#!/usr/bin/env bash
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
LEAD=${5:-75}

B=${B_HOST:-192.168.99.2}
DIAG=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$DIAG/.." && pwd)
D="$REPO/results/$label"
URL="${LK_URL:-wss://livekit-figure-ai.apps.oai01.stc.edgeai.t-mobile.com}"
CLIP="${CLIP:-/home/nsusser/teleop-media/depal-face-lower-20260904/depal-face-lower-src-30s.mp4}"
B_REPO=${B_REPO:-'~/code/rust-sdks'}
span=$(( LEAD + DUR + 25 ))
mkdir -p "$D/hostb"

ssh_b() { timeout "${2:-30}" ssh -n -o BatchMode=yes -o ConnectTimeout=10 "$B" "$1"; }
say() { printf '%s\n' "$*"; }

say "=== paired cell: $label  ${cap}k  $codec  ${DUR}s  (lead ${LEAD}s, recorders ${span}s) ==="
say "    sfu  : $URL"
say "    clip : $CLIP"

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
ssh_b "cd ~/diag-capture && setsid nohup ./capture.sh $span $label > ~/teleop-runs/$label/capture.out 2>&1 < /dev/null &" 25
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
  [ "${d:-0}" = 1 ] && [ "${s:-0}" = 1 ] && [ "${p:-0}" = 1 ] && [ "${h:-0}" = 1 ] && { ok=0; break; }
  sleep 3
done
say "    B diag=$d subscriber=$s tcpdump=$p hops=$h"
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

DURATION="$DUR" "$REPO/teleop-test-matrix/scripts/publish-cell.sh" "$epoch" "$label" "$cap" "$codec" "$D" \
  || { echo "cell did not go LIVE" >&2; kill -TERM "$rec" 2>/dev/null; exit 1; }

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
