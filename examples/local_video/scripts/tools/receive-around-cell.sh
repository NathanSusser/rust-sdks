#!/usr/bin/env bash
# Host B half of a joint modem-DIAG test: capture Host B's modem log AROUND a
# subscriber that receives Host A's cell, so both modem logs and both media logs
# cover the same wall-clock window and can be joined afterwards.
#
# Pairs with Host A's diag-capture/capture-around-cell.sh, which captures Host A's
# modem and publishes the cell via publish-cell.sh. Both take the same <epoch>.
#
# WHY A WRAPPER. capture.sh runs in the foreground for a fixed duration. Starting it
# and a subscriber by hand in two terminals around a timed cell means the capture
# starts late or ends early, and nobody notices until the logs are read. This
# starts the capture before the cell, proves it is writing, joins the room, proves
# media arrived, and holds the capture until the cell is over.
#
# WHY NOT run_subscriber_test.sh. Its pre-flight network probe puts its own traffic
# through Host B's modem inside the capture window, which would appear in the DIAG
# log as scheduler activity the cell did not cause. The bare subscriber adds none.
#
# SERVER. Must match publish-cell.sh, which hardcodes the -h265 deployment. The URL
# in .livekit-demo/.env is the newer server; joining it puts us in a different room
# with the same name and we receive nothing -- which is how the 2026-09-11 uplink
# DIAG test got zero media.
#
# PORT. capture.sh (owned by the rust-sdks-d9 session) takes a flock on the DIAG
# port and refuses to start if another capture holds it. Never kill capture.sh
# itself: its diag-log-off step runs after QCSuper exits, and killing the shell
# skips it, leaving the modem streaming logs and loading the baseband.
#
# CLOCK. DLF modem timestamps are not a usable clock on their own: a file can hold
# garbage records (a 2.4e11 s min-max span was seen) and valid ones sit ~10 s off wall
# with seconds of slop. timeline.txt records two independent wall anchors -- capture
# live (ms) and media onset (first-packet µs from the CSV) -- so the DLF can be aligned
# by a median(wall - modem) fit over records near each, outliers dropped, rather than
# by trusting any single record.
#
# UTC. The rig clock is common to both hosts (PTP) but NOT UTC: Host A, the reference,
# is not NTP-disciplined, and on 2026-09-15 both ran ~4.35 s behind UTC. Host-to-host
# alignment is unaffected; joining against UTC-keyed logs (the SFU's) is not. One SNTP
# offset is logged before the capture window opens and one after it closes -- ~100 ms
# of traffic each, outside the DIAG window -- because the offset drifts.
#
# Usage: receive-around-cell.sh <label> <epoch> <room> [outdir]
set -uo pipefail

[ $# -ge 3 ] || { echo "usage: $0 <label> <epoch> <room> [outdir]" >&2; exit 2; }
label=$1 epoch=$2 room=$3

REPO=$(cd "$(dirname "$0")/../../../.." && pwd)
outdir=${4:-$REPO/examples/local_video/scripts/results/diag-$room}
URL="wss://livekit-release-livekit-server-figure-ai-h265.apps.oai01.stc.edgeai.t-mobile.com"
CAPTURE="$HOME/diag-capture/capture.sh"
SUB="$REPO/target/release/subscriber"

# Host A's cell: 5 s warmup + 150 s run from the epoch, then ~3 s of flush.
CAP_BEFORE=60      # capture starts this long before the epoch (idle baseline)
CAP_AFTER=35       # and runs this long past the cell's end
CELL_S=155
SUB_BEFORE=20      # subscriber joins this long before the epoch
# Show frame timing (latency) in the subscriber's diagnostics window. Costs a little
# local render work on Host B's integrated GPU, which can nudge local latency; it has
# no effect on the modem or the network. SHOW_TIMING=0 omits it, as in the runbook.
TIMING_FLAG=(--display-timestamp)
[ "${SHOW_TIMING:-1}" = "0" ] && TIMING_FLAG=()
cap_dur=$((CAP_BEFORE + CELL_S + CAP_AFTER))

say() { echo "[$(date -u +%H:%M:%S)] $*" | tee -a "$outdir/timeline.txt"; }
ms()  { date +%s%3N; }
# One SNTP query; prints local-minus-UTC. Never fails the run.
sntp_offset() {
  python3 - <<'NTP'
import socket, struct, time
E = 2208988800
def query(host):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.settimeout(2)
    pkt = bytearray(48); pkt[0] = 0x23                      # NTPv4, client mode
    t1 = time.time()
    struct.pack_into("!II", pkt, 40, int(t1) + E, int((t1 % 1) * 2**32))
    s.sendto(pkt, (host, 123)); data, _ = s.recvfrom(512); t4 = time.time()
    ts = lambda o: (lambda a, b: a - E + b / 2**32)(*struct.unpack_from("!II", data, o))
    t2, t3 = ts(32), ts(40)
    theta = ((t2 - t1) + (t3 - t4)) / 2                     # server minus local
    delay = (t4 - t1) - (t3 - t2)
    return -theta, delay
for h in ("time.google.com", "time.cloudflare.com", "pool.ntp.org"):
    try:
        lmu, d = query(h)
        print(f"local-UTC {lmu*1000:+.1f} ms ({'behind' if lmu < 0 else 'ahead of'} UTC) via {h}, delay {d*1000:.0f} ms")
        break
    except Exception:
        continue
else:
    print("SNTP unavailable (no server answered; run continues)")
NTP
}

# ---- pre-flight: fail BEFORE the epoch, not after it --------------------------
now=$(date +%s)
[ "$epoch" -gt $((now + CAP_BEFORE + 15)) ] || {
  echo "epoch $epoch is too soon: need > $((CAP_BEFORE + 15)) s of lead for the capture to go live first" >&2; exit 1; }
[ -x "$CAPTURE" ] || { echo "capture script missing: $CAPTURE" >&2; exit 1; }
[ -x "$SUB" ]     || { echo "subscriber binary missing: $SUB" >&2; exit 1; }
[ -c /dev/ttyUSB0 ] || { echo "DIAG port /dev/ttyUSB0 absent" >&2; exit 1; }
# Match the interpreter, not the word. `pgrep -f qcsuper` also matches any shell whose
# command text merely contains "qcsuper" -- including Claude tool shells and monitors
# on this host -- and would refuse, losing the run. capture.sh uses the same pattern.
QC_PROC='^[^ ]*python[0-9.]* [^ ]*qcsuper'
if pgrep -f "$QC_PROC" >/dev/null; then
  echo "a qcsuper process already holds a DIAG port; not starting:" >&2; pgrep -af "$QC_PROC" >&2; exit 1
fi
[ -n "${DISPLAY:-}" ] || { echo "DISPLAY unset: run this at Host B's console, not over plain ssh" >&2; exit 1; }
cd "$REPO" || exit 1
set -a && . .livekit-demo/.env && set +a
: "${LIVEKIT_API_KEY:?not set after sourcing .livekit-demo/.env}"
: "${LIVEKIT_API_SECRET:?not set after sourcing .livekit-demo/.env}"
export SSL_CERT_FILE="${SSL_CERT_FILE:-$REPO/.livekit-demo/corp-ca.pem}"
mkdir -p "$outdir" "$HOME/diag-logs"
: > "$outdir/timeline.txt"
say "label=$label epoch=$epoch room=$room capture=${cap_dur}s server=-h265"
say "clock pre-run:  $(sntp_offset)"

# ---- 1. modem capture, live before the cell -----------------------------------
python3 -c "import time;d=$epoch-$CAP_BEFORE-time.time()
if d>0: time.sleep(d)"
before=$(ls -1 "$HOME"/diag-logs/"$label"-*.dlf 2>/dev/null | wc -l)
"$CAPTURE" "$cap_dur" "$label" > "$outdir/capture.out" 2>&1 &
cpid=$!
dlf=""
for _ in $(seq 1 200); do
  kill -0 "$cpid" 2>/dev/null || { say "CAPTURE DID NOT START:"; cat "$outdir/capture.out" >&2; exit 1; }
  if [ "$(ls -1 "$HOME"/diag-logs/"$label"-*.dlf 2>/dev/null | wc -l)" -gt "$before" ]; then
    dlf=$(ls -1t "$HOME"/diag-logs/"$label"-*.dlf | head -1)
    [ -s "$dlf" ] && break
  fi
  sleep 0.1
done
[ -n "$dlf" ] && [ -s "$dlf" ] || { say "CAPTURE ALIVE BUT WROTE NOTHING in 20 s (letting it finish so diag-log-off runs)"; wait "$cpid"; exit 1; }
say "capture LIVE -> $dlf  (wall ms $(ms))"

# ---- 2. subscriber, in the room before the publisher --------------------------
python3 -c "import time;d=$epoch-$SUB_BEFORE-time.time()
if d>0: time.sleep(d)"
env -u WAYLAND_DISPLAY RUST_LOG=info timeout $((SUB_BEFORE + CELL_S + 25)) \
  "$SUB" --url "$URL" --room-name "$room" --identity "host-b-$label" --low-latency "${TIMING_FLAG[@]}" \
  --log-csv "$outdir/subscriber.csv" > "$outdir/subscriber.log" 2>&1 &
spid=$!
for _ in $(seq 1 30); do
  grep -q 'Connected:' "$outdir/subscriber.log" 2>/dev/null && break
  kill -0 "$spid" 2>/dev/null || break
  sleep 0.5
done
if grep -q 'Connected:' "$outdir/subscriber.log"; then
  say "subscriber CONNECTED to $room"
else
  say "SUBSCRIBER FAILED TO CONNECT (capture continues; log below)"; tail -8 "$outdir/subscriber.log" >&2
fi

# ---- 3. did media actually arrive? The one fact the DIAG capture needs --------
deadline=$((epoch + 40))
arrived=0
while [ "$(date +%s)" -lt "$deadline" ] && kill -0 "$spid" 2>/dev/null; do
  if grep -qE 'Decode health: received=[1-9]' "$outdir/subscriber.log" 2>/dev/null; then arrived=1; break; fi
  sleep 1
done
if [ "$arrived" = 1 ]; then
  say "MEDIA ARRIVED  (poll saw decode by wall ms $(ms); precise onset from CSV at the end)"
else
  say "NO MEDIA by epoch+40 s -- the downlink was idle; this DIAG window does not cover a loaded link"
fi

# ---- 4. hold for the cell, then let the capture end on its own ----------------
wait "$spid"; src=$?
say "subscriber exited rc=$src  (4 = publisher unpublished cleanly, 124 = timeout)"
say "waiting for capture to finish and turn modem logging off..."
wait "$cpid"; crc=$?
say "capture exited rc=$crc"
say "clock post-run: $(sntp_offset)"

# ---- 5. what landed ----------------------------------------------------------
echo
cat "$outdir/capture.out"
echo
grep -E 'Decode health' "$outdir/subscriber.log" | tail -1
python3 - "$outdir/subscriber.csv" "$epoch" "$outdir/timeline.txt" <<'PY'
import csv, sys
rows = list(csv.DictReader(open(sys.argv[1])))
if not rows:
    print("subscriber.csv: no rows (nothing rendered -- check Decode health above for arrival)")
    raise SystemExit
epoch_us = int(sys.argv[2]) * 1_000_000
# Media onset: first-packet wire-arrival time, PTP-disciplined µs. The downlink MAC
# records in the DLF jump at the same moment, so this is a second, independent anchor.
arr = []
for r in rows:
    try:
        v = int(r["webrtc_receive_timestamp_us"])
    except (ValueError, KeyError):
        continue
    if v > 0:
        arr.append(v)
if arr:
    onset = min(arr)
    line = (f"media onset (first-packet wire time, PTP): {onset} us "
            f"= epoch{(onset - epoch_us) / 1e6:+.3f} s")
    print(line)
    with open(sys.argv[3], "a") as t:
        t.write(line + "\n")
bins = {}
for r in rows:
    try:
        t = (int(r["webrtc_receive_timestamp_us"]) - epoch_us) / 1e6
        b = float(r["receive_bitrate_mbps"])
    except (ValueError, KeyError):
        continue
    bins.setdefault(int(t // 10) * 10, []).append(b)
print(f"subscriber.csv: {len(rows)} rows, packets_lost {rows[-1].get('packets_lost')}, "
      f"resolution {rows[-1].get('frame_width')}x{rows[-1].get('frame_height')}")
print("receive bitrate by 10 s window, seconds from epoch (a grant collapse shows here):")
for k in sorted(bins):
    v = sorted(bins[k])
    print(f"  t+{k:>4}s  p50 {v[len(v)//2]:5.2f} Mbps   n={len(v)}")
PY
say "done. outdir=$outdir dlf=$dlf"
