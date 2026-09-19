#!/usr/bin/env bash
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
REPO=$(cd "$(dirname "$0")/../../../.." && pwd)
CELL=~/teleop/cells/$ROOM
A_HOST=${A_HOST:-nsusser@192.168.99.1}
# NOT "\$HOME/..." -- that goes over the wire literally inside the quoted ssh string and
# scp looks for a directory whose name starts with a dollar sign. Resolved remotely instead.
A_RESULTS=${A_RESULTS:-}
# Deployment. NOT defaulted to $LIVEKIT_URL: joining a different deployment with the same
# room name succeeds SILENTLY and receives nothing, which is indistinguishable from a real
# failure. Switching is an explicit act -- and Host A must be switched to match.
URL=${LK_URL:-wss://livekit-figure-ai.apps.oai01.stc.edgeai.t-mobile.com}

# Capture windows must OUTLAST the cell. Sized from DUR rather than fixed, because a cell
# that outruns its DIAG window leaves the last minute with no modem log and the report
# looks complete anyway.
# LEAD is the allowance for the operator-driven handshake between B arming and A
# publishing. Observed: 81 s on 2026-09-19. A capture that outlasts the cell measured
# from OUR start must carry the handshake too, or the tail is lost.
LEAD=${LEAD:-120}
PCAP_S=$(( DUR + LEAD + 120 ))
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
say "pre-flight: PTP servo $servo"

# Wait for the capture locks rather than failing. A previous cell's captures
# self-terminate; colliding with them just loses this cell too.
for _ in $(seq 1 60); do
  if flock -n 9 9>~/diag-capture/.pcap-wwan0.lock &&
     flock -n 8 8>~/diag-capture/.ttyUSB0.lock; then break; fi
  say "waiting for capture locks to clear..."; sleep 5
done

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
print(f"{statistics.median(v):.3f}" if v else "0")
PY
)
say "clock offset measured: ${OFFSET}s local-UTC"

# ---- arm -------------------------------------------------------------------------
say "arming pcap ${PCAP_S}s and DIAG ${DIAG_S}s for room $ROOM"
setsid nohup ~/diag-capture/pcap.sh "$PCAP_S" "$ROOM" wwan0 > "$CELL/pcap.out" 2>&1 </dev/null &
sleep 4
setsid nohup ~/diag-capture/capture.sh "$DIAG_S" "$ROOM" > "$CELL/capture.out" 2>&1 </dev/null &
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
running tcpdump "$ROOM"        && say "pcap: RUNNING"  || say "pcap: NOT RUNNING -- cell will be single-ended"
running qcsuper-noroot "$ROOM" && say "DIAG: RUNNING"  || say "DIAG: NOT RUNNING -- no modem log this cell"

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
say "waiting for captures to close (diag-log-off must run)"
for _ in $(seq 1 60); do
  running tcpdump "$ROOM" || running qcsuper-noroot "$ROOM" || break
  sleep 5
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
DLF=$(ls -t ~/diag-logs/"$ROOM"-*.dlf 2>/dev/null | head -1)
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
  python3 "$REPO/examples/local_video/scripts/tools/dlf_rates.py" "$DLF" \
    --probe-start-ms $((EP * 1000)) --probe-end-ms $((EPEND * 1000)) \
    --host-minus-utc "$OFFSET" --before 60 --after 60 \
    -o "$CELL/dlf-rates-hostb.csv" >> "$CELL/timeline.txt" 2>&1
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
[ -n "$missing" ] && TITLE="$ROOM  [INCOMPLETE:$missing ]"
[ -n "$missing" ] && say "REPORT IS INCOMPLETE --$missing"

args=(--subscriber "$CELL/subscriber.csv" --subscriber-log "$CELL/subscriber.log"
      --title "$TITLE" -o "$PDF")
[ -s "${PUB:-}" ]   && args+=(--publisher "$PUB")
[ -s "${JSONL:-}" ] && args+=(--publisher-stats "$JSONL")
[ -s "$A_MODEM" ]   && args+=(--modem-rates-a "$A_MODEM")
[ -s "$CELL/dlf-rates-hostb.csv" ] && args+=(--modem-rates-b "$CELL/dlf-rates-hostb.csv")

say "generating report"
python3 "$REPO/examples/local_video/scripts/generate_frame_report.py" "${args[@]}" \
  2>&1 | tee -a "$CELL/timeline.txt"

# Verify the PDF actually rendered. A blank modem page has shipped from here before.
if [ -s "$PDF" ]; then
  pages=$(python3 -c "
import re,sys
d=open('$PDF','rb').read()
print(len(re.findall(rb'/Type\s*/Page[^s]', d)))" 2>/dev/null)
  rows=$(( $(wc -l < "$CELL/subscriber.csv" 2>/dev/null || echo 1) - 1 ))
  say "REPORT: $PDF ($(stat -c %s "$PDF") bytes, ${pages} pages, ${rows} frames)"
  ln -sfn "$CELL" ~/teleop/latest-cell
  echo
  echo "  ==> $PDF"
  echo "  ==> also at ~/teleop/latest-cell/"
else
  say "REPORT FAILED -- artefacts are in $CELL"
fi
