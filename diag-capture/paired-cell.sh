#!/usr/bin/env bash
# SFU hostname lives outside the repo (public): ~/.config/teleop/sfu.env sets TELEOP_SFU_HOST.
[ -f "$HOME/.config/teleop/sfu.env" ] && . "$HOME/.config/teleop/sfu.env"
# ONE COMMAND: run a paired A/B cell with every instrument on both hosts, pull Host B's data
# back, and verify the two sides describe the same cell.
#
#   ./diag-capture/paired-cell.sh <label> <cap_kbps> <codec> [duration_s] [lead_s]
#   e.g. ./diag-capture/paired-cell.sh cell-5m 5000 h264 300
#
# Everything below is a rule bought with a lost or wrong measurement. Read before editing.
#
# COLLISION GUARD, FIRST. On 2026-09-18 two publishers ended up in one room twice, because a
# cell was launched without checking whether one was already running. The subscriber binds to
# ONE track, so the second stream is invisible on screen while it doubles the uplink load and
# corrupts the first 60 s of the capture. This script REFUSES to start if a publisher,
# subscriber or DIAG capture is live on either host. Stop them, or use a different room.
#
# THE EPOCH IS COMPUTED ONCE. A candidate epoch written during planning and then abandoned at
# relaunch left a stale EPOCH file that anchored a whole modem reduction 48 s late; the report
# looked perfect and the error surfaced only when the other host compared probe_start values.
# Here the epoch is computed once, written from that same variable, and CHECKED afterwards
# against what at-epoch actually printed.
#
# DURATION MUST BE EXPORTED. publish-cell.sh takes the cell length from $DURATION and silently
# falls back to 150 s when it is unset. A cell requested as 300 s once recorded for 380 s and
# published for 150.
#
# NEVER TRUST ssh's EXIT CODE as evidence that a capture started on B. `setsid nohup ... &`
# returns 0 whether or not the thing runs. Every B-side instrument is verified POSITIVELY
# below -- by its own output appearing and growing.
#
# B'S PCAP LIVES OUTSIDE THE RSYNC TREE. pcap.sh writes to ~/pcap-logs/, not the run dir, so a
# plain rsync of the run dir silently returns no packet capture. It is pulled explicitly.
#
# NO CREDENTIALS CROSS THE LINK. Host B sources its OWN .livekit-demo/.env and its own corp CA.
# This script never sends a key or secret to B, and must never be changed to.
#
# A LOCKED WAYLAND SESSION SILENTLY RUINS THE SUBSCRIBER: it withholds frame callbacks, so the
# subscriber decodes at full rate and renders almost nothing, and decode health reads perfectly
# the whole time. The lock state is checked before arming and the run refuses if it is locked.
set -uo pipefail

label=${1:?usage: paired-cell.sh <label> <cap_kbps> <codec> [duration_s] [lead_s]}
cap=${2:?cap_kbps required}
codec=${3:?codec required}
DUR=${4:-300}
LEAD=${5:-180}      # 75 was not enough: arming measured 117 s on 2026-09-23

B=${B_HOST:-192.168.99.2}
DIAG=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$DIAG/.." && pwd)
D="$REPO/results/$label"
URL="${LK_URL:-wss://${TELEOP_SFU_HOST:?TELEOP_SFU_HOST unset -- put it in ~/.config/teleop/sfu.env}}"
CLIP="${CLIP:-/home/nsusser/teleop-media/depal-face-lower-20260904/depal-face-lower-src-30s.mp4}"
B_REPO=${B_REPO:-'~/code/rust-sdks'}
span=$(( LEAD + DUR + 25 ))
mkdir -p "$D/hostb"

ssh_b() { timeout "${2:-30}" ssh -n -o BatchMode=yes -o ConnectTimeout=10 "$B" "$1"; }
say() { printf '%s\n' "$*"; }

say "=== paired cell: $label  ${cap}k  $codec  ${DUR}s  (lead ${LEAD}s, recorders ${span}s) ==="
say "    sfu  : $URL"
say "    clip : $CLIP"

# ---------------------------------------------------------------- CPU governor, BOTH hosts
# ASSERT THE GOVERNOR BEFORE THE RUN, NOT AFTER. On 2026-09-24 Host B's subscriber printed a
# powersave warning, the operator fixed B, and Host A was left in powersave -- because the
# warning came from B's wrapper and A's publisher prints no equivalent. A runs the ENCODER, so
# a half-corrected pair is arguably worse than neither: it looks corrected.
#
# Why it matters at a pinned rate: encode timing sits inside what we are measuring. A throttled
# encoder and a queueing link produce similar-looking latency in the paired split, and if A is
# slow we cannot tell them apart -- which is the decomposition this tooling exists to provide.
# Host A's cores idle at 800 MHz under powersave; forking tools have measured 4-5x too slow.
#
# cpufrequtils persists the GOVERNOR across reboots but NOT EPP -- that is an intel_pstate knob
# outside its scope -- so both need re-checking after any reboot. Warn rather than refuse: a
# throttled cell is still a cell, and the operator may be deliberately measuring the throttled
# case. Set GOV_STRICT=1 to make it fatal.
say
say "--- CPU governor ---"
gov_bad=0
a_gov=$(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor 2>/dev/null || echo unknown)
a_epp=$(cat /sys/devices/system/cpu/cpu0/cpufreq/energy_performance_preference 2>/dev/null || echo unknown)
b_ge=$(ssh_b 'echo "$(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor 2>/dev/null || echo unknown) $(cat /sys/devices/system/cpu/cpu0/cpufreq/energy_performance_preference 2>/dev/null || echo unknown)"' 20)
b_gov=${b_ge%% *}; b_epp=${b_ge##* }
say "    A: governor=$a_gov epp=$a_epp        (publisher -- ENCODE timing)"
say "    B: governor=${b_gov:-?} epp=${b_epp:-?}        (subscriber -- DECODE/RENDER timing)"
for pair in "A:$a_gov:$a_epp" "B:${b_gov:-unknown}:${b_epp:-unknown}"; do
  h=${pair%%:*}; rest=${pair#*:}; g=${rest%%:*}; e=${rest##*:}
  if [ "$g" != performance ] || [ "$e" != performance ]; then
    say "    WARNING: Host $h is not at performance/performance."
    say "             Its timings will be INFLATED and are not comparable to performance runs."
    say "             Fix on Host $h:  echo performance | sudo tee /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor"
    say "                              echo performance | sudo tee /sys/devices/system/cpu/cpu*/cpufreq/energy_performance_preference"
    gov_bad=1
  fi
done
[ "$gov_bad" = 0 ] && say "    both hosts at performance/performance"
if [ "$gov_bad" = 1 ] && [ "${GOV_STRICT:-0}" = 1 ]; then
  echo "REFUSING: GOV_STRICT=1 and a host is not at performance." >&2; exit 1
fi

# ---------------------------------------------------------------- collision guard
say
say "--- collision guard ---"
a_busy=""
pgrep -x teleop-harness >/dev/null && a_busy="$a_busy publisher(pid $(pgrep -x teleop-harness | tr '\n' ' '))"
fuser /dev/ttyUSB0 >/dev/null 2>&1 && a_busy="$a_busy diag-capture"
[ -n "$a_busy" ] && { echo "REFUSING: Host A already busy:$a_busy" >&2
                      echo "Stop it first -- two publishers in one room corrupt both." >&2; exit 1; }
say "    A: clear"

b_busy=$(ssh_b 'b=""; pgrep -x subscriber >/dev/null && b="$b subscriber"; pgrep -x tcpdump >/dev/null && b="$b tcpdump"; fuser /dev/ttyUSB0 >/dev/null 2>&1 && b="$b diag"; echo "$b"' 20)
[ -n "${b_busy// /}" ] && { echo "REFUSING: Host B already busy:$b_busy" >&2; exit 1; }
say "    B: clear"

lock=$(ssh_b 'loginctl list-sessions --no-legend | awk "{print \$1}" | while read s; do
        [ "$(loginctl show-session $s -p Type --value)" = wayland ] && loginctl show-session $s -p LockedHint --value; done | head -1' 20)
[ "$lock" = "yes" ] && { echo "REFUSING: Host B's Wayland session is LOCKED. It withholds frame callbacks," >&2
                         echo "so the subscriber would decode fine and render nothing, and the CSV would" >&2
                         echo "look healthy while being empty. Unlock B's screen and re-run." >&2; exit 1; }
say "    B session unlocked (render data will be valid)"

# ---------------------------------------------------------------- the one epoch
epoch=$(( $(date -u +%s) + LEAD ))
printf '%s\n' "$epoch" > "$D/EPOCH"
say
say "--- epoch $epoch = $(date -u -d @"$epoch" +%H:%M:%SZ) (written once, to $D/EPOCH) ---"

# ---------------------------------------------------------------- arm Host B
say
say "--- arming Host B ---"
ssh_b "mkdir -p ~/teleop-runs/$label" 20
# B_DIAG=0 runs the cell with NO modem log on Host B. Host B has ~90 GB against this host's
# ~560 GB and DIAG costs ~30 GB/hour per host, so B's disk -- not the night -- is what ends a
# long campaign. Turning B's DIAG off is the only way to extend one WITHOUT DELETING ANYTHING,
# which is the operator's standing instruction.
#
# What that costs: nothing for the event hunt. The signature lives in A's modem log, and
# 2026-09-22's work established that nothing on B's radio layer moved at the event -- not a
# level, not a validity flag. B still contributes the thing the trigger actually needs, which
# is packets_lost from subscriber.csv, plus its pcap and hop counters. Those are ~60 MB/cell.
# Set B_DIAG=1 to restore lockstep capture when B has the room for it.
B_DIAG=${B_DIAG:-1}
if [ "$B_DIAG" = 1 ]; then
  ssh_b "cd ~/diag-capture && setsid nohup ./capture.sh $span $label > ~/teleop-runs/$label/capture.out 2>&1 < /dev/null &" 25
else
  say "    B_DIAG=0: no modem log on Host B this cell (disk budget; A still captures)"
fi
ssh_b "cd ~/diag-capture && setsid nohup ./pcap.sh $span $label > ~/teleop-runs/$label/pcap.out 2>&1 < /dev/null &" 25
ssh_b "cd ~/diag-capture && setsid nohup ./hop-recorder-b.sh $label $span ~/teleop-runs/$label > ~/teleop-runs/$label/hops.out 2>&1 < /dev/null &" 25
# B sources its OWN credentials and CA. Nothing secret travels over this connection.
ssh_b "cd $B_REPO && setsid nohup env XDG_RUNTIME_DIR=/run/user/1000 WAYLAND_DISPLAY=wayland-0 \
        bash -c 'set -a; . .livekit-demo/.env; set +a; exec examples/local_video/scripts/run_subscriber_test.sh \
        \"$URL\" \"$label\" $(( DUR + 40 )) ~/teleop-runs/$label' \
        > ~/teleop-runs/$label/subscriber.out 2>&1 < /dev/null &" 30

# VERIFY POSITIVELY -- output appearing, not an ssh exit code.
ok=1
for i in $(seq 12); do
  st=$(ssh_b "d=0; s=0; p=0; h=0
    ls ~/diag-logs/${label}-*.dlf >/dev/null 2>&1 && [ -s \"\$(ls -t ~/diag-logs/${label}-*.dlf | head -1)\" ] && d=1
    pgrep -x subscriber >/dev/null && s=1
    pgrep -x tcpdump >/dev/null && p=1
    [ -s ~/teleop-runs/$label/$label.hops-b.csv ] && h=1
    echo \"\$d \$s \$p \$h\"" 20)
  read -r d s p h <<<"$st"
  # Do not require the flag for something we deliberately did not start: with B_DIAG=0 the
  # DIAG check would never pass and every cell would refuse. Require it only when armed.
  want_d=$B_DIAG
  [ "${d:-0}" = "$want_d" ] && [ "${s:-0}" = 1 ] && [ "${p:-0}" = 1 ] && [ "${h:-0}" = 1 ] && { ok=0; break; }
  sleep 3
done
say "    B diag=$d (wanted $B_DIAG) subscriber=$s tcpdump=$p hops=$h"
[ "$ok" -eq 0 ] || { echo "REFUSING: Host B did not come up fully (see flags above)." >&2
                     ssh_b "tail -5 ~/teleop-runs/$label/subscriber.out 2>/dev/null" 20 >&2; exit 1; }
say "    B armed and verified"

# ---------------------------------------------------------------- Host A
say
say "--- arming Host A ---"
DIAG=1 "$DIAG/hop-recorder.sh" "$label" "$span" "$D" > "$D/recorder.out" 2>&1 &
rec=$!
sleep 8
kill -0 "$rec" 2>/dev/null || { echo "A recorder died:" >&2; cat "$D/recorder.out" >&2; exit 1; }
say "    A recorder live"

# DO NOT WALK INTO at-epoch's REFUSAL. On 2026-09-23 arming both hosts took 117 s against a
# 75 s lead, so the epoch was 42 s past and the publisher refused -- correctly, but only
# after both hosts had armed and B had written 1.4 GB. Check it here, where the failure is
# still cheap, and print the MEASURED arming time so the next run uses a number instead of
# another guess.
now=$(date -u +%s)
remain=$(( epoch - now ))
armed_for=$(( LEAD - remain ))
say "    arming took ${armed_for}s of the ${LEAD}s lead; ${remain}s left before epoch"
if [ "$remain" -lt 15 ]; then
  say "ABORT: only ${remain}s before the epoch -- at-epoch would refuse to start late."
  say "       Arming took ${armed_for}s. Re-run with a lead of at least $(( armed_for + 60 )):"
  say "         ./diag-capture/paired-cell.sh $label $cap $codec $DUR $(( armed_for + 60 ))"
  kill -TERM "$rec" 2>/dev/null
  exit 1
fi

# PASS THE SFU AND THE CLIP EXPLICITLY, THEN CHECK WHAT WAS ACTUALLY USED.
# 2026-09-23: this line passed ONLY DURATION. URL and CLIP above are plain shell vars, so
# publish-cell.sh never saw them and fell back to its own defaults -- the OLD h265
# deployment and the 30-minute robot clip. TWO separate faults: CLIP had the right name but
# was unexported, and URL is the WRONG NAME (publish-cell.sh reads LK_URL), so exporting it
# would not have helped either. Host B was on livekit-figure-ai while the publisher would
# have gone to livekit-release-...-h265 -- same room name, different server, so they could
# never meet. Only at-epoch's refusal stopped it becoming data compared against cell5m-a.
# The header above already says "DURATION MUST BE EXPORTED": the lesson was learned for one
# variable and never generalised to the other two.
#
# Then VERIFY THE PRODUCT instead of trusting the assignment -- the rule the capture-file
# selection in this codebase already follows, because every heuristic has failed once.
pub_out="$D/publish-cell.out"
DURATION="$DUR" LK_URL="$URL" CLIP="$CLIP" \
  "$REPO/teleop-test-matrix/scripts/publish-cell.sh" "$epoch" "$label" "$cap" "$codec" "$D" 2>&1 \
  | tee "$pub_out"
[ "${PIPESTATUS[0]}" = 0 ] \
  || { echo "cell did not go LIVE" >&2; kill -TERM "$rec" 2>/dev/null; exit 1; }

# LOOK IN THE RIGHT FILE, AND FAIL IF THE VALUE IS ABSENT.
# publish-cell.sh writes only its LIVE line to stdout; the "source:" and "sfu:" lines go to the
# CELL LOG. The first version of this check read stdout, found nothing, and -- because the
# comparison was guarded by [ -n "$used_url" ] -- passed silently, printing "<not printed>".
# A guard that skips itself when its input is missing is a guard that can only pass. Search
# both files, and treat a missing value as a FAILURE rather than a pass.
for src in "$D/$label.log" "$pub_out"; do
  [ -s "$src" ] || continue
  [ -n "${used_url:-}" ]  || used_url=$(sed -n 's/^sfu: *url=\([^ ]*\).*/\1/p' "$src" | head -1)
  [ -n "${used_clip:-}" ] || used_clip=$(sed -n 's/^source: *clip=\([^ ]*\).*/\1/p' "$src" | head -1)
done
vpid=$(sed -n 's/^LIVE .* pid=\([0-9]*\) .*/\1/p' "$pub_out" | head -1)
bad=0
if [ -z "${used_url:-}" ] || [ -z "${used_clip:-}" ]; then
  say "CANNOT VERIFY: publisher did not report its sfu/clip where this check looks."
  say "               url='${used_url:-<absent>}'  clip='${used_clip:-<absent>}'"
  say "               Refusing rather than assuming they were right -- an unverifiable cell is"
  say "               not a verified one, and this check silently passed on absent output once."
  bad=1
fi
if [ -n "${used_url:-}" ] && [ "$used_url" != "$URL" ]; then
  say "WRONG SFU : asked $URL"; say "            used  $used_url"; bad=1
fi
if [ -n "${used_clip:-}" ] && [ "$used_clip" != "$CLIP" ]; then
  say "WRONG CLIP: asked $CLIP"; say "            used  $used_clip"; bad=1
fi
if [ "$bad" = 1 ]; then
  say "ABORTING: this cell would not be comparable to the others."
  [ -n "$vpid" ] && kill -TERM "$vpid" 2>/dev/null
  kill -TERM "$rec" 2>/dev/null
  exit 1
fi
say "    verified sfu : ${used_url:-<not printed>}"
say "    verified clip: $(basename "${used_clip:-<not printed>}")"

say
say "--- cell running; waiting out the ${span}s recorder span ---"
wait "$rec"
sleep 5

# ---------------------------------------------------------------- collect
say
say "--- pulling Host B ---"
timeout 600 rsync -a -e "ssh -o BatchMode=yes" "$B:~/teleop-runs/$label/" "$D/hostb/" 2>&1 | tail -2
# pcap.sh writes OUTSIDE the run dir; the rsync above cannot see it.
bp=$(ssh_b "ls -t ~/pcap-logs/${label}-*.pcap 2>/dev/null | head -1" 20)
[ -n "$bp" ] && timeout 600 rsync -a -e "ssh -o BatchMode=yes" "$B:$bp" "$D/hostb/" && say "    pulled $(basename "$bp")"
[ -n "$bp" ] || say "    WARNING: no pcap found on B in ~/pcap-logs"

# ---------------------------------------------------------------- reduce both modems
# Both offsets are MEASURED NOW, on their own hosts. They are never carried over from an
# earlier cell: the offset moved 0.19 s in ninety minutes on 2026-09-18 and 2.2 s in an hour
# on 2026-09-17, and a reduction anchored with a remembered offset came out 2,458 ms out
# against the video while every record count looked perfect.
say
say "--- reducing modem logs (both hosts, offsets measured now) ---"
a_off=$(python3 "$DIAG/clock-offset.py" 2>/dev/null)
b_off=$(ssh_b 'python3 ~/diag-capture/clock-offset.py' 25)
say "    A host_minus_utc_s=${a_off:-UNKNOWN}   B host_minus_utc_s=${b_off:-UNKNOWN}"

a_dlf="$D/$label.dlf"
if [ -s "$a_dlf" ] && [ -n "$a_off" ]; then
  timeout 1800 python3 "$DIAG/dlf-rates.py" "$a_dlf" "$epoch" "$(( epoch + DUR ))" "$a_off" "$D/dlf-rates.csv" 60 \
    2>&1 | tail -1 | sed 's/^/    A: /'
else
  say "    A: SKIPPED (dlf missing or offset unknown) -- modem page will be absent"
fi

# B reduces on B and sends back the ~1 MB CSV, not the multi-GB raw capture.
if [ -n "$b_off" ]; then
  ssh_b "d=\$(ls -t ~/diag-logs/${label}-*.dlf 2>/dev/null | head -1); [ -n \"\$d\" ] && \
         timeout 1800 python3 ~/diag-capture/dlf-rates.py \"\$d\" $epoch $(( epoch + DUR )) $b_off \
           ~/teleop-runs/$label/dlf-rates.csv 60 2>&1 | tail -1" 1900 | sed 's/^/    B: /'
  timeout 300 rsync -a -e "ssh -o BatchMode=yes" "$B:~/teleop-runs/$label/dlf-rates.csv" "$D/hostb/" 2>/dev/null \
    && say "    B: reduction pulled" || say "    B: reduction NOT pulled"
else
  say "    B: SKIPPED (offset unknown)"
fi

# ---------------------------------------------------------------- verify
say
say "=== verification ==="
fired=$(sed -n 's/.*waiting [0-9]*s until \([0-9:]*\) UTC.*/\1/p' "$D/$label.log" | head -1)
want=$(date -u -d @"$epoch" +%H:%M:%S)
if [ -n "$fired" ] && [ "$fired" != "$want" ]; then
  echo "  ANCHOR MISMATCH: EPOCH says $want, at-epoch fired $fired -- modem reductions will be wrong" >&2
else
  say "  anchor ok: $epoch = $want = firing time"
fi
for f in "$D/$label.pub.csv" "$D/$label.jsonl" "$D/$label.wwan0.pcap" "$D/$label.dlf" "$D/$label.hops.csv"; do
  [ -s "$f" ] && say "  A  $(basename "$f")  $(du -h "$f" | cut -f1)" || say "  A  $(basename "$f")  MISSING/EMPTY"
done
for f in "$D"/hostb/subscriber.csv "$D"/hostb/"$label".hops-b.csv "$D"/hostb/*.pcap; do
  [ -s "$f" ] && say "  B  $(basename "$f")  $(du -h "$f" | cut -f1)" || say "  B  $(basename "$f")  MISSING/EMPTY"
done
a_fr=$(( $(wc -l < "$D/$label.pub.csv" 2>/dev/null || echo 1) - 1 ))
b_fr=$(( $(wc -l < "$D/hostb/subscriber.csv" 2>/dev/null || echo 1) - 1 ))
say "  frames: A captured $a_fr   B rendered $b_fr   shortfall $(( a_fr - b_fr ))"
for who in "" hostb/; do
  f="$D/${who}dlf-rates.csv"
  [ -s "$f" ] && say "  anchor $( [ -z "$who" ] && echo A || echo B ): $(head -1 "$f" | grep -oE 'probe_start=[0-9.]+|probe_start_ms=[0-9]+' | head -1)"
done
[ "$b_fr" -lt 10 ] && say "  WARNING: B rendered almost nothing -- check the Wayland lock note at the top of this script"
say
say "outputs: $D   (raw .dlf kept on both hosts; deleting them is the operator's call)"
say "next   : python3 diag-capture/paired-report.py $D $D/$label-paired.pdf"
