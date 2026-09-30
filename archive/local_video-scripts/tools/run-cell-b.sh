#!/usr/bin/env bash
# SFU hostname lives outside the repo (public): ~/.config/teleop/sfu.env sets TELEOP_SFU_HOST.
[ -f "$HOME/.config/teleop/sfu.env" ] && . "$HOME/.config/teleop/sfu.env"
# Host B: run one instrumented cell and produce the paired PDF, in one command.
#
#   run-cell-b.sh <room> [duration_s]
#
# Arms Host B's packet capture and full-mask modem DIAG, runs the subscriber in the
# FOREGROUND so the operator can watch the video and Ctrl-C it, then after the cell
# collects everything and renders the report into ~/teleop/cells/<room>/.
#
# Host A is not started from here. Start it separately (or let the operator), publishing
# into the SAME room on the SAME deployment. If A's artefacts are reachable over the PTP
# cable when the cell ends they are pulled in and the report is paired; if not, a B-only
# report is produced rather than nothing.
#
# Every guard below exists because its absence cost a cell on 2026-09-17/18.
set -uo pipefail

ROOM=${1:?usage: run-cell-b.sh <room> [duration_s]}
DUR=${2:-300}
# Lives at archive/local_video-scripts/tools/ since the teleop/ restructure.
SCRIPTS=$(cd "$(dirname "$0")/.." && pwd)
REPO=$(cd "$SCRIPTS/../.." && pwd)
CELL=~/teleop/cells/$ROOM
A_HOST=${A_HOST:-nsusser@192.168.99.1}
# NOT "\$HOME/..." -- that goes over the wire literally inside the quoted ssh string and
# scp looks for a directory whose name starts with a dollar sign. Resolved remotely instead.
A_RESULTS=${A_RESULTS:-}
# Deployment. NOT defaulted to $LIVEKIT_URL: joining a different deployment with the same
# room name succeeds SILENTLY and receives nothing, which is indistinguishable from a real
# failure. Switching is an explicit act -- and Host A must be switched to match.
URL=${LK_URL:-wss://${TELEOP_SFU_HOST:?TELEOP_SFU_HOST unset -- put it in ~/.config/teleop/sfu.env}}

# Capture windows must OUTLAST the cell. Sized from DUR rather than fixed, because a cell
# that outruns its DIAG window leaves the last minute with no modem log and the report
# looks complete anyway.
# LEAD is the allowance for the operator-driven handshake between B arming and A
# publishing. Observed: 81 s on 2026-09-19. A capture that outlasts the cell measured
# from OUR start must carry the handshake too, or the tail is lost.
# DIAG is stopped TAIL seconds after the subscriber exits, so its window is only a cap.
# pcap cannot be stopped (see pcap.sh), so its window is the whole budget: a publisher
# that starts more than LEAD s after arming loses the pcap tail. Raise LEAD for slow handshakes.
LEAD=${LEAD:-60}
TAIL=${TAIL:-15}
PCAP_S=$(( DUR + LEAD + 30 ))
DIAG_S=$(( DUR + LEAD + 90 ))

mkdir -p "$CELL"
say() { echo "[$(date -u +%H:%M:%S)] $*" | tee -a "$CELL/timeline.txt"; }

# ---- pre-flight ------------------------------------------------------------------
# PTP servo must be LOCKED. phc2sys STEPS CLOCK_REALTIME by seconds while acquiring
# (-7.96 s observed at the 2026-09-18 reboot); a step mid-cell corrupts every
# cross-host timestamp join silently.
servo=$(journalctl --since "-60 sec" --no-pager 2>/dev/null |
        grep -oE "phc2sys.*s[0-9]" | tail -1 | grep -oE "s[0-9]$")
if [ "$servo" != "s2" ]; then
  echo "PTP servo is '${servo:-unknown}', not s2 -- still acquiring and may step the clock." >&2
  echo "Wait for s2 before arming, or set FORCE=1 to override." >&2
  [ "${FORCE:-0}" = 1 ] || exit 1
fi
# phc2sys s2 is NOT proof of cross-host lock. phc2sys only syncs CLOCK_REALTIME to B's
# own PHC; if ptp4l has lost Host A that PHC is free-running and phc2sys still reports
# s2 with nanosecond offsets. On 2026-09-26 the PTP cable was down 00:30-01:24Z, this
# check printed "PTP servo s2" at 01:11:45, and vbv1-test1-8mbps ran with B's clock
# untethered. Require what proves B is disciplined to A: carrier on the PTP port, and
# ptp4l emitting servo (rms) lines, which it only does while it has a master.
PTP_IF=${PTP_IF:-eno2}
carrier=$(cat /sys/class/net/$PTP_IF/carrier 2>/dev/null || echo 0)
p4l=$(journalctl --since "-30 sec" --no-pager 2>/dev/null | grep -cE "ptp4l\[[0-9]+\]: .* rms ")
if [ "$carrier" != 1 ] || [ "${p4l:-0}" -lt 5 ]; then
  echo "PTP NOT locked to Host A: $PTP_IF carrier=$carrier, ptp4l servo lines in 30 s=${p4l:-0}." >&2
  echo "phc2sys may still say s2 -- it is tracking a free-running PHC. Check the PTP cable." >&2
  echo "Set FORCE=1 to run anyway (cross-host latency will be untrustworthy)." >&2
  [ "${FORCE:-0}" = 1 ] || exit 1
fi
say "pre-flight: PTP servo $servo, $PTP_IF carrier up, ptp4l locked to A ($p4l servo lines/30 s)"

# 5G must be up BEFORE arming. On 2026-09-29 a cell was launched mid modem re-registration:
# tcpdump refused ("wwan0: That device is not up"), DIAG started anyway, and the abort left
# DIAG holding the port lock for its full window, blocking the rerun.
if ! ip -4 -o addr show dev wwan0 2>/dev/null | grep -q inet || ! ip route show default dev wwan0 2>/dev/null | grep -q .; then
  echo "5G NOT UP: wwan0 has no IPv4 address or default route (modem registering?). Wait and retry." >&2
  exit 1
fi

# Wait for the capture locks to clear. NOTE: `flock -n 9 9>f` only TESTS the lock --
# the redirection is scoped to the flock command, so the lock is released the instant
# it returns. This loop therefore cannot reserve anything, and on 2026-09-21 a previous
# run's captures were still alive when it stopped waiting: the new pcap.sh and capture.sh
# both refused to start, and the whole 300 s cell recorded nothing. So: wait longer, and
# REFUSE rather than proceed blind.
locks_free() {
  flock -n 9 9>~/diag-capture/.pcap-wwan0.lock && flock -n 8 8>~/diag-capture/.ttyUSB0.lock
}
lock_ok=0
for _ in $(seq 1 60); do
  if locks_free; then lock_ok=1; break; fi
  say "waiting for capture locks to clear..."; sleep 5
done
if [ "$lock_ok" != 1 ]; then
  echo "Capture locks still held after 5 minutes -- a previous cell is still capturing." >&2
  echo "Wait for it to finish, or set FORCE=1 to run a cell with NO captures." >&2
  [ "${FORCE:-0}" = 1 ] || exit 1
fi

# Clock offset MEASURED, never estimated. An estimated -12.3 against a real -14.758
# misaligned a whole modem timeline by 2.5 s; the counts were fine and the alignment
# was not, which is invisible on the rendered page.
OFFSET=$(python3 - <<'PY'
import socket, struct, time, statistics
v = []
for h in ("time.google.com", "pool.ntp.org", "time.cloudflare.com"):
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.settimeout(3)
        t0 = time.time(); s.sendto(b'\x1b' + 47 * b'\0', (h, 123))
        d, _ = s.recvfrom(1024); t1 = time.time()
        u = struct.unpack("!12I", d)
        v.append(((t0 + t1) / 2) - (u[10] - 2208988800 + u[11] / 2**32))
    except Exception:
        pass
print(f"{statistics.median(v):.3f}" if v else "")
PY
)
# No answer from any server used to print 0, and the captures then stamped the host clock
# as true UTC. Unknown is not zero.
OFFSET_MEASURED=
if [ -z "$OFFSET" ]; then
  echo "Clock offset UNMEASURED: no NTP server answered over 5G." >&2
  [ "${FORCE:-0}" = 1 ] || exit 1
  OFFSET=0
  say "clock offset UNMEASURED -- capture stamps end in h, modem alignment assumes 0"
else
  OFFSET_MEASURED=1
  say "clock offset measured: ${OFFSET}s local-UTC"
fi
# Hand it to the capture wrappers. Without this they stamp filenames from the HOST clock
# and label them `Z`, which this box free-runs ~15.8 s behind -- so the name claims a UTC
# it does not have and any cross-host join by filename inherits the error. With it set
# they stamp true UTC; without it they end the stamp in `h` rather than lie.
[ -n "$OFFSET_MEASURED" ] && export HOST_MINUS_UTC="$OFFSET"

# ---- arm -------------------------------------------------------------------------
# ARM_T gates every freshness check below. A capture file older than this belongs to a
# previous run and must never be mistaken for ours.
ARM_T=$(date +%s)
say "arming pcap ${PCAP_S}s and DIAG ${DIAG_S}s for room $ROOM"
setsid nohup ~/diag-capture/pcap.sh "$PCAP_S" "$ROOM" wwan0 > "$CELL/pcap.out" 2>&1 </dev/null &
PCAP_PID=$!
sleep 4
setsid nohup ~/diag-capture/capture.sh "$DIAG_S" "$ROOM" > "$CELL/capture.out" 2>&1 </dev/null &
DIAG_PID=$!
# TERM, not INT: a background job of a non-interactive shell starts with SIGINT ignored, and
# bash cannot trap a signal ignored at entry -- so INT was silently dropped and DIAG ran its
# full window (2026-09-29). capture.sh traps TERM the same way: one SIGINT to QCSuper, then
# diag-log-off.
stop_diag() { kill -TERM "$DIAG_PID" 2>/dev/null; }
# The pcap LOCK is held by the pcap.sh wrapper (it closes fd 9 for tcpdump), so stopping the
# wrapper frees the lock. tcpdump itself cannot be signalled and runs on to its -G deadline,
# still writing THIS room's file -- which may then pick up the next cell's first packets.
release_pcap_lock() { kill -TERM "$PCAP_PID" 2>/dev/null; }
# Ctrl-C or a closed terminal used to orphan every capture: setsid detaches them, so they ran
# their full windows and held both locks, blocking the next cell for up to 7 min (2026-09-29).
on_interrupt() {
  trap '' INT TERM HUP
  say "interrupted -- stopping DIAG, hops-b and the pcap wrapper (tcpdump runs to its deadline)"
  stop_diag; release_pcap_lock; kill -TERM "${HOP_PID:-}" 2>/dev/null
  sleep 6   # let DIAG close its file before it is shipped
  ship
  exit 130
}
trap on_interrupt INT TERM HUP

# Operator policy since 2026-09-29: Host A stores everything, B keeps nothing after a run.
# Hands this cell's directory and this run's capture files to ship-to-a.sh, detached: it
# waits for tcpdump to close the pcap, copies to A, verifies every sha256, then deletes
# B's copy. On any failure B's copy stays and ~/teleop/ship.log says FAILED.
ship() {
  # All results live under ~/teleop-runs on A since the restructure (not results/ in the repo).
  local dest f t extra=()
  dest=$(timeout 20 ssh -o BatchMode=yes -o ConnectTimeout=8 "$A_HOST" \
           "echo \$HOME/teleop-runs/manual/$ROOM" 2>/dev/null)
  if [ -z "$dest" ]; then
    say "SHIP FAILED: Host A unreachable -- data stays on B in $CELL"; return
  fi
  for f in "$HOME"/diag-logs/"$ROOM"-* "$HOME"/pcap-logs/"$ROOM"-*; do
    [ -f "$f" ] || continue
    # Birth time, same clock as ARM_T (the name stamps are true UTC now, not host time).
    t=$(stat -c %W "$f" 2>/dev/null); [ "${t:-0}" -ge "$ARM_T" ] && extra+=("$f")
  done
  say "shipping to $A_HOST:$dest/hostb -- B's copy is deleted once every file verifies (log: ~/teleop/ship.log)"
  setsid nohup "$SCRIPTS/tools/ship-to-a.sh" "$ROOM" "$CELL" "$A_HOST" "$dest/hostb" "${extra[@]}" \
    >/dev/null 2>&1 </dev/null &
}
# Host B's receive-side recorder: qdisc, driver counters, PER-SOCKET UDP drops, and the radio
# (rsrp/rsrq/snr via mmcli, unprivileged). Until 2026-09-22 Host B recorded NO radio metrics at
# all, so cell5m-a's +8 dB step on Host A could not be checked against this host -- a one-host
# step is equally consistent with a beam change local to A and a cell change nobody instrumented
# here. NOT hop-recorder.sh: that one needs sudo for its QMI columns, which this host does not
# have, and would return them silently empty.
setsid nohup ~/diag-capture/hop-recorder-b.sh "$ROOM" "$(( DUR + LEAD + 60 ))" "$CELL" \
  > "$CELL/hops-b.out" 2>&1 </dev/null &
HOP_PID=$!
sleep 8

# Verify by POSITIVE OBSERVATION, not by exit code. An ssh/launch exit status says
# nothing about whether the capture is writing; only the process and a growing file do.
# Match on /proc/PID/comm, never `pgrep -f` -- that matches the shell carrying the
# pattern in its own argv and has twice killed the wrong process here.
running() {
  local want=$1 pat=$2 p c cl
  for p in /proc/[0-9]*; do
    c=$(cat "$p/comm" 2>/dev/null) || continue
    [ "$c" = "$want" ] || continue
    cl=$(tr '\0' ' ' < "$p/cmdline" 2>/dev/null)
    case "$cl" in *$pat*) return 0;; esac
  done
  return 1
}
# `running <comm> <room>` is NOT sufficient on its own: on 2026-09-21 it matched the
# PREVIOUS run's tcpdump, which carried the same room name in its argv and was still
# alive, and reported "pcap: RUNNING" while our own capture had refused to start. The
# only honest evidence is a capture FILE created after we armed, whose size is growing.
# MTIME IS THE WRONG CLOCK HERE. A previous run's capture that is still writing has a
# mtime of "just now" -- on 2026-09-21 the stale DLF's mtime was 8 minutes after its own
# start. What we need is when the file was CREATED. Birth time (%W) gives it where the
# filesystem records it; the capture scripts also stamp the creation instant into the
# name as <room>-YYYYMMDDTHHMMSSZ, written by `date -u` on this same clock, so that is
# the authority and works everywhere.
born() {                       # born <path> -> creation epoch, or 0
  local f=$1 stamp b
  # Z = stamped in true UTC (capture.sh had HOST_MINUS_UTC); h = host clock, offset unknown.
  stamp=$(printf '%s' "${f##*/}" | grep -oE '[0-9]{8}T[0-9]{6}[Zh]' | tail -1)
  if [ -n "$stamp" ]; then
    b=$(date -u -d "${stamp:0:4}-${stamp:4:2}-${stamp:6:2} ${stamp:9:2}:${stamp:11:2}:${stamp:13:2}" +%s 2>/dev/null)
    [ -n "$b" ] && { printf '%s' "$b"; return; }
  fi
  b=$(stat -c %W "$f" 2>/dev/null)
  case "$b" in ''|0|-) b=0;; esac
  printf '%s' "$b"
}
fresh() {                      # fresh <glob> -> newest file CREATED at or after ARM_T
  local newest="" f t
  for f in $1; do
    [ -f "$f" ] || continue
    t=$(born "$f"); [ "${t:-0}" -ge "$ARM_T" ] || continue
    newest=$f
  done
  printf '%s' "$newest"
}
growing() {                    # growing <file> -> 0 if it gained bytes over 3 s
  local a b
  a=$(stat -c %s "$1" 2>/dev/null || echo 0); sleep 3
  b=$(stat -c %s "$1" 2>/dev/null || echo 0)
  [ "${b:-0}" -gt "${a:-0}" ]
}
PCAP_F=$(fresh "$HOME/pcap-logs/$ROOM-*.pcap")
DIAG_F=$(fresh "$HOME/diag-logs/$ROOM-*.dlf")
armed=1
if [ -n "$PCAP_F" ] && running tcpdump "$ROOM"; then
  say "pcap: RUNNING -> $(basename "$PCAP_F")"
else
  say "pcap: NOT RUNNING -- no capture file newer than arming, or tcpdump is not alive"; armed=0
fi
if [ -n "$DIAG_F" ] && running qcsuper-noroot "$ROOM" && growing "$DIAG_F"; then
  say "DIAG: RUNNING -> $(basename "$DIAG_F")"
else
  say "DIAG: NOT RUNNING -- no capture file newer than arming, or it is not growing"; armed=0
fi
HOPS_F="$CELL/$ROOM.hops-b.csv"
if [ -s "$HOPS_F" ]; then
  # An empty radio column is the documented failure here: mmcli returns nothing unless signal
  # polling is armed. Say so rather than shipping a file that looks complete.
  if awk -F, 'NR>1 && $16!="" {found=1} END{exit !found}' "$HOPS_F" 2>/dev/null; then
    say "hops-b: RUNNING -> $(basename "$HOPS_F") (radio columns populated)"
  else
    say "hops-b: running but RADIO COLUMNS EMPTY -- mmcli signal polling not armed; qdisc and UDP counters still valid"
  fi
else
  say "hops-b: NOT RUNNING -- no receive-side counters or radio metrics this cell"
fi

if [ "$armed" != 1 ]; then
  say "ABORTING before the subscriber starts -- a cell with no captures is 5 wasted minutes"
  echo "See $CELL/pcap.out and $CELL/capture.out. Set FORCE=1 to run anyway." >&2
  if [ "${FORCE:-0}" != 1 ]; then
    trap - INT TERM HUP
    stop_diag; release_pcap_lock; kill -TERM "$HOP_PID" 2>/dev/null
    sleep 6
    ship
    exit 1
  fi
fi

# ---- the cell --------------------------------------------------------------------
say "subscriber joining $ROOM (foreground -- Ctrl-C to stop)"
set -a; . "$REPO/.livekit-demo/.env"; set +a
SSL_CERT_FILE="$REPO/.livekit-demo/corp-ca.pem" \
env -u WAYLAND_DISPLAY DISPLAY="${DISPLAY:-:0}" RUST_LOG=info \
  "$REPO/target/release/subscriber" \
  --url "$URL" --room-name "$ROOM" --identity "host-b-$ROOM" \
  --low-latency --display-timestamp \
  --log-csv "$CELL/subscriber.csv" 2>&1 | tee "$CELL/subscriber.log"
say "subscriber exited"

# ---- collect ---------------------------------------------------------------------
say "stopping DIAG in ${TAIL}s (pcap closes itself ${PCAP_S}s after arming; nothing below reads it)"
sleep "$TAIL"
trap - INT TERM HUP
stop_diag
release_pcap_lock
for _ in $(seq 1 60); do
  running qcsuper-noroot "$ROOM" || break
  sleep 2
done

# Host A's artefacts, if the cable answers. Absence is reported, not fatal.
mkdir -p "$CELL/hosta"
# Resolve the remote directory ON HOST A, so $HOME expands in A's shell and not in this
# string. Then pull ONE FILE PER scp: a single scp with several sources fails as a whole
# when any one source does not match, so a missing dlf-rates.csv silently pulled NOTHING
# and the report rendered Host B only without saying so (2026-09-18, cell5m-a).
if [ -z "$A_RESULTS" ]; then
  A_RESULTS=$(timeout 20 ssh -o BatchMode=yes -o ConnectTimeout=8 "$A_HOST" \
                "echo \$HOME/code/rust-sdks/results/$ROOM" 2>/dev/null)
fi
if [ -n "$A_RESULTS" ] &&
   timeout 20 ssh -o BatchMode=yes -o ConnectTimeout=8 "$A_HOST" "test -d '$A_RESULTS'" 2>/dev/null; then
  say "pulling Host A artefacts from $A_RESULTS"
  for pat in '*.pub.csv' '*.jsonl' 'dlf-rates.csv' 'dlf-rates-hosta.csv'; do
    if timeout 180 scp -q -o BatchMode=yes -o ConnectTimeout=8 \
         "$A_HOST:$A_RESULTS/$pat" "$CELL/hosta/" 2>/dev/null; then
      say "  pulled $pat"
    else
      say "  MISSING on Host A: $pat"
    fi
  done
  ls "$CELL/hosta" | sed 's/^/  hosta: /' | tee -a "$CELL/timeline.txt"
else
  say "Host A artefacts NOT reachable -- report will be Host B only"
fi

# ---- reduce the modem log, anchored on the MEDIA, not on our own arming instant -----
# The arming instant is not the media instant: the operator starts Host A by hand, and on
# 2026-09-19 the handshake was 81 s. A window of DUR+60 measured from arming therefore
# ended 21 s BEFORE the media did, and the report drew a strip that stopped early without
# saying so. capture_timestamp_us in A's publisher CSV is the only instant that means the
# same thing on both hosts, so prefer it and fall back loudly.
# Must be OUR dlf. `ls -t | head -1` picked a previous run's file on 2026-09-21 and
# reduced a window that did not overlap the media at all, writing zero rows.
DLF=$(fresh "$HOME/diag-logs/$ROOM-*.dlf")
if [ -z "$DLF" ]; then
  say "NO modem log from this run -- not reducing a stale one (report will have no Host B strip)"
fi
if [ -n "$DLF" ]; then
  PUB0=$(ls "$CELL"/hosta/*.pub.csv "$CELL"/*.pub.csv 2>/dev/null | head -1)
  MEDIA=$(python3 - "${PUB0:-}" <<'PYMEDIA'
import csv, sys
p = sys.argv[1] if len(sys.argv) > 1 else ""
if p:
    ts = [float(r["capture_timestamp_us"]) / 1e6
          for r in csv.DictReader(open(p)) if r.get("capture_timestamp_us")]
    if ts:
        print(f"{int(min(ts))} {int(max(ts)) + 1}")
PYMEDIA
)
  if [ -n "$MEDIA" ]; then
    set -- $MEDIA; EP=$1; EPEND=$2
    say "modem window from A capture timestamps: ${EP}..${EPEND} ($((EPEND-EP))s of media)"
  else
    EP=$(date -u -d "$(grep -m1 -oE '^\[[0-9:]+' "$CELL/timeline.txt" | tr -d '[')" +%s 2>/dev/null || date -u +%s)
    EPEND=$(( EP + DUR + 180 ))
    say "NO publisher CSV -- modem window falls back to arming+${DUR}s+180s slack; strip may not match the media"
  fi
  say "reducing $(basename "$DLF") at offset ${OFFSET}s"
  python3 "$SCRIPTS/tools/dlf_rates.py" "$DLF" \
    --probe-start-ms $((EP * 1000)) --probe-end-ms $((EPEND * 1000)) \
    --host-minus-utc "$OFFSET" --before 60 --after 60 \
    -o "$CELL/dlf-rates-hostb.csv" >> "$CELL/timeline.txt" 2>&1
  # GUARD THE PRODUCT, NOT THE SELECTION (Host A's framing, 2026-09-21, and it is the
  # better one). No rule for picking the right capture file can succeed when the right
  # file does not exist -- on cell5m-a2 only a stale capture existed at all. So check
  # what came out: does the reduction actually cover the media? This one test catches a
  # stale capture, a non-overlapping window and a wrong clock offset alike.
  #
  # Coverage is PER-SECOND PRESENCE, not the span from first record to last. The span
  # form is the trap: records at both window edges with a hole between score 100%.
  # There is deliberately NO midpoint test. It existed as a cheap proxy for "the window
  # really is inside the data", which only matters under the span form; under density it
  # is redundant AND harmful -- a reduction 99% covered whose one missing second happens
  # to be the midpoint was being rejected outright. Do not re-add it.
  COV=$(python3 - "$CELL/dlf-rates-hostb.csv" "$EP" "$EPEND" <<'PYCOV'
import csv, sys
path, ep, epend = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
span = max(1, epend - ep)
try:
    secs = {int(r["second_rel_probe"]) for r in
            csv.DictReader(l for l in open(path) if not l.startswith("#"))}
except Exception:
    print("0 0 0"); raise SystemExit
have = sum(1 for s in range(0, span) if s in secs)
print(f"{len(secs)} {100 * have // span}")
PYCOV
)
  set -- $COV; NSEC=${1:-0}; PCT=${2:-0}
  say "modem coverage: ${PCT}% of the ${DUR}s media window, ${NSEC} seconds in file"
  if [ "${PCT:-0}" -lt 50 ]; then
    say "MODEM REDUCTION DOES NOT COVER THE MEDIA -- discarding it rather than drawing it."
    say "  capture: $(basename "$DLF")   media window: ${EP}..${EPEND}"
    say "  This cell has NO usable Host B modem data. Do not read a quiet modem page as quiet."
    mv -f "$CELL/dlf-rates-hostb.csv" "$CELL/dlf-rates-hostb.REJECTED.csv" 2>/dev/null
  fi
fi

# The two modem files are anchored to each host's own probe_start. If those differ the
# report draws both strips as if simultaneous and nothing on the page looks wrong.
# Look in the cell directory too, not only hosta/: when the pull breaks, Host A pushes its
# reduction straight into $CELL rather than waiting for a pull that is the broken part.
A_MODEM=""
for c in "$CELL/hosta/dlf-rates.csv" "$CELL/hosta/dlf-rates-hosta.csv" \
         "$CELL/dlf-rates-hosta.csv" "$CELL/dlf-rates-a.csv"; do
  [ -s "$c" ] && { A_MODEM="$c"; break; }
done
if [ -s "$A_MODEM" ] && [ -s "$CELL/dlf-rates-hostb.csv" ]; then
  python3 - "$A_MODEM" "$CELL/dlf-rates-hostb.csv" "$CELL/hosta/dlf-rates-aligned.csv" <<'PY' | tee -a "$CELL/timeline.txt"
import re, sys, csv
a, b, out = sys.argv[1], sys.argv[2], sys.argv[3]
def start(p):
    t = "".join(l for l in open(p) if l.startswith("#"))
    m = re.search(r"probe_start_ms=(\d+)", t) or re.search(r"probe_start=([0-9.]+)", t)
    if not m: return None
    v = float(m.group(1)); return int(v if v > 1e11 else v * 1000)
sa, sb = start(a), start(b)
if sa is None or sb is None or sa == sb:
    print("  modem strips share an origin (or one lacks probe_start); no shift"); sys.exit(1)
shift = (sa - sb) // 1000
hdr = [l for l in open(a) if l.startswith("#")]
rows = list(csv.DictReader(l for l in open(a) if not l.startswith("#")))
with open(out, "w") as f:
    for h in hdr:
        f.write(re.sub(r"probe_start(_ms)?=[0-9.]+", f"probe_start_ms={sb}", h))
    f.write("second_rel_probe,code,count\n")
    for r in rows:
        f.write(f"{int(r['second_rel_probe'])+shift},{r['code']},{r['count']}\n")
print(f"  Host A modem shifted {shift:+d}s onto Host B's origin ({len(rows)} rows)")
PY
  [ -s "$CELL/hosta/dlf-rates-aligned.csv" ] && A_MODEM="$CELL/hosta/dlf-rates-aligned.csv"
fi

# ---- report ----------------------------------------------------------------------
PDF="$CELL/$ROOM-paired.pdf"
PUB=$(ls "$CELL"/hosta/*.pub.csv "$CELL"/*.pub.csv 2>/dev/null | head -1)
JSONL=$(ls "$CELL"/hosta/*.jsonl "$CELL"/*.jsonl 2>/dev/null | head -1)

# Say it in the TITLE when an input is missing. A transfer bug will recur; a report that
# cannot hide a missing input is what catches it. The 2026-09-18 B-only report was
# indistinguishable from a paired one at a glance, which is how it shipped.
TITLE="$ROOM"
missing=""
[ -s "${PUB:-}" ]                  || missing="$missing no-publisher"
[ -s "$A_MODEM" ]                  || missing="$missing no-modem-A"
[ -s "$CELL/dlf-rates-hostb.csv" ] || missing="$missing no-modem-B"
# Accept/reject is not enough. A reduction covering 53% of the media passes the 50%
# floor with its midpoint just inside, and is then DRAWN as though complete -- the same
# silent-partial failure as the 21 s tail lost on cloud5m-a, only larger. So anything
# short of near-total coverage is named in the title rather than quietly rendered.
if [ -s "$CELL/dlf-rates-hostb.csv" ] && [ "${PCT:-100}" -lt 95 ]; then
  missing="$missing modem-B-only-${PCT}%"
fi
# capture.sh retries QCSuper if it exits early, appending to the same DLF. That
# recovery is the problem: it turns a loud failure into a slightly-short capture
# nobody examines, and capture.sh is launched with setsid/& so its exit 3 is never
# read. Host A declined to mirror the retry for exactly this reason. Keep it, but
# make a fired retry a VISIBLE event -- a DLF assembled across a gap is not the
# same artefact as one written straight through, whatever its size says.
# Read the marker off the ARTEFACT, not only off the run log. Host A's point: a glob
# consumer SURVIVES a "-PARTIAL" suffix, which means it happily reduces the partial
# capture as though it were whole -- the marker helps only if something downstream
# refuses it. "$ROOM-*.dlf" matches "-PARTIAL" files, so this is that refusal. It also
# catches a DLF reduced outside this script, where capture.out is not present at all.
case "${DLF:-}" in
  *-PARTIAL.dlf) missing="$missing modem-B-capture-PARTIAL" ;;
esac
if [ -s "$CELL/capture.out" ] && grep -q 'QCSuper exited after' "$CELL/capture.out" 2>/dev/null; then
  n=$(grep -c 'QCSuper exited after' "$CELL/capture.out"); n=${n:-0}
  missing="$missing modem-B-capture-restarted-x${n}"
fi
[ -n "$missing" ] && TITLE="$ROOM  [INCOMPLETE:$missing ]"
[ -n "$missing" ] && say "REPORT IS INCOMPLETE --$missing"

args=(--subscriber "$CELL/subscriber.csv" --subscriber-log "$CELL/subscriber.log"
      --title "$TITLE" -o "$PDF")
[ -s "${PUB:-}" ]   && args+=(--publisher "$PUB")
[ -s "${JSONL:-}" ] && args+=(--publisher-stats "$JSONL")
[ -s "$A_MODEM" ]   && args+=(--modem-rates-a "$A_MODEM")
[ -s "$CELL/dlf-rates-hostb.csv" ] && args+=(--modem-rates-b "$CELL/dlf-rates-hostb.csv")

say "generating report"
python3 "$SCRIPTS/generate_frame_report.py" "${args[@]}" \
  2>&1 | tee -a "$CELL/timeline.txt"

# Verify the PDF actually rendered. A blank modem page has shipped from here before.
if [ -s "$PDF" ]; then
  pages=$(python3 -c "
import re,sys
d=open('$PDF','rb').read()
print(len(re.findall(rb'/Type\s*/Page[^s]', d)))" 2>/dev/null)
  rows=$(( $(wc -l < "$CELL/subscriber.csv" 2>/dev/null || echo 1) - 1 ))
  say "REPORT: $PDF ($(stat -c %s "$PDF") bytes, ${pages} pages, ${rows} frames)"
else
  say "REPORT FAILED"
fi
ship
