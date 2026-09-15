#!/usr/bin/env bash
# Host B half of a joint modem-DIAG test: capture Host B's modem log AROUND a
# subscriber that receives Host A's cell, so both modem logs and both media logs
# cover the same wall-clock window and can be joined afterwards.
#
# Pairs with Host A's diag-capture/capture-around-cell.sh / long-run.sh, which capture
# Host A's modem and publish the cell via publish-cell.sh. Both take the same <epoch>.
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
# The same goes for any bulk upload: on 2026-09-10 Host A's own uplink speed test
# caused every "grant collapse". This script warns if curl/rsync/scp are running.
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
# UNATTENDED. Host A's driver can launch this over the cable, one call per cycle:
#   ssh -n nsusser@192.168.99.2 "cd ~/code/rust-sdks; DURATION=600 DIAG=1 setsid nohup \
#     examples/local_video/scripts/tools/receive-around-cell.sh <label> <epoch> <room> <outdir> \
#     >/dev/null 2>&1 </dev/null &"
# `;` not `&&` before the background job: the `&&` form keeps ssh open until the run ends.
# Over ssh there is no DISPLAY/XAUTHORITY; this finds the console's Xwayland auth file.
# <outdir>/DONE is written when every child has stopped; pull only after it exists.
#
# Knobs (environment):
#   DURATION=150  seconds the publisher streams after its 5 s warmup
#   DIAG=1        modem DLF via capture.sh (0: skip; lets cycles alternate the DIAG load)
#   HOPS=1        1 Hz host counters: wwan0 rx/tx, qdisc, UDP socket-buffer drops, signal
#   PING=1        5 Hz ICMP to the SFU (RTT and loss on the path, independent of media)
#   SHOW_TIMING=1 frame timing in the subscriber's diagnostics window
#   SUBSCRIBE=1   0: no subscriber or room join (recorders only), for no-video test arms
#
# Usage: receive-around-cell.sh <label> <epoch> <room> [outdir]
set -uo pipefail
# A non-login ssh or systemd environment may omit sbin, where tc lives.
export PATH="$PATH:/usr/sbin:/sbin"

[ $# -ge 3 ] || { echo "usage: $0 <label> <epoch> <room> [outdir]" >&2; exit 2; }
label=$1 epoch=$2 room=$3

REPO=$(cd "$(dirname "$0")/../../../.." && pwd)
outdir=${4:-$REPO/examples/local_video/scripts/results/diag-$room}
URL="wss://livekit-release-livekit-server-figure-ai-h265.apps.oai01.stc.edgeai.t-mobile.com"
SFU_IP=10.1.20.21
CAPTURE="$HOME/diag-capture/capture.sh"
SUB="$REPO/target/release/subscriber"
IF=wwan0
DIAG=${DIAG:-1} HOPS=${HOPS:-1} PING=${PING:-1}

# Host A's cell: 5 s warmup + DURATION s run from the epoch, then ~3 s of flush.
CAP_BEFORE=60      # capture starts this long before the epoch (idle baseline)
CAP_AFTER=35       # and runs this long past the cell's end
CELL_S=$(( ${DURATION:-150} + 5 ))
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

# 1 Hz host-side counters. On Host B the media is DOWNLINK, so the drops that matter
# are receive-side: the driver (rx_dropped/rx_missed) and the UDP socket buffer
# (RcvbufErrors) -- a packet the modem delivered but this host discarded. The qdisc is
# egress only; it is logged because RTCP and ping leave through it.
hops_sampler() {
  local csv=$1 dur=$2 s=/sys/class/net/$IF/statistics end=$(( $(date +%s) + $2 ))
  mmcli -m 0 --signal-setup=5 >/dev/null 2>&1
  echo "unix_ms,rx_packets,rx_bytes,rx_dropped,rx_errors,rx_missed,tx_packets,tx_dropped,qdisc_dropped,qdisc_backlog_pkts,qdisc_requeues,udp_in_errors,udp_rcvbuf_errors,nr_rsrp_dbm,nr_snr_db,access_tech" > "$csv"
  local n=0 sig=",," t q
  while [ "$(date +%s)" -lt "$end" ]; do
    t=$(ms)
    q=$(tc -s qdisc show dev "$IF" 2>/dev/null)
    local qd qb qr
    qd=$(sed -n 's/.*(dropped \([0-9]*\),.*/\1/p' <<<"$q" | head -1)
    qb=$(sed -n 's/.*backlog [0-9]*b \([0-9]*\)p.*/\1/p' <<<"$q" | head -1)
    qr=$(sed -n 's/.*requeues \([0-9]*\)).*/\1/p' <<<"$q" | head -1)
    # /proc/net/snmp "Udp:" value line: InErrors is field 4, RcvbufErrors field 6.
    local udp; udp=$(awk '/^Udp: [0-9]/{print $4","$6; exit}' /proc/net/snmp)
    if [ $((n % 5)) = 0 ]; then
      local g; g=$(mmcli -m 0 --signal-get 2>/dev/null; mmcli -m 0 2>/dev/null | grep -m1 'access tech')
      sig="$(sed -n '/5G/,$ s/.*rsrp: *\(-*[0-9.]*\).*/\1/p' <<<"$g" | head -1),$(sed -n '/5G/,$ s/.*s\/n: *\(-*[0-9.]*\).*/\1/p' <<<"$g" | head -1),$(sed -n 's/.*access tech: *\(.*\)$/\1/p' <<<"$g" | head -1 | tr -d ' \033' | sed 's/\[[0-9;]*m//g')"
    fi
    echo "$t,$(cat $s/rx_packets),$(cat $s/rx_bytes),$(cat $s/rx_dropped),$(cat $s/rx_errors),$(cat $s/rx_missed_errors),$(cat $s/tx_packets),$(cat $s/tx_dropped),${qd},${qb},${qr},${udp},${sig}" >> "$csv"
    n=$((n + 1))
    sleep "$(awk -v t="$t" -v now="$(ms)" 'BEGIN{d=1-(now-t)/1000; print (d>0?d:0)}')"
  done
}

cpid="" spid="" hpid="" ppid_="" dlf="" src="" crc=""
finish() {
  local rc=$?
  # Children with their own timers stop by themselves; these two are only ours to stop.
  [ -n "$hpid" ] && kill "$hpid" 2>/dev/null
  [ -n "$ppid_" ] && kill $ppid_ 2>/dev/null
  # Never kill capture.sh (see PORT); wait for it so DONE means the modem is quiet.
  [ -n "$cpid" ] && wait "$cpid" 2>/dev/null
  [ -d "$outdir" ] && printf 'rc=%s subscriber_rc=%s capture_rc=%s dlf=%s finished_ms=%s\n' \
    "$rc" "${src:-}" "${crc:-}" "${dlf:-none}" "$(ms)" > "$outdir/DONE"
}

# ---- pre-flight: fail BEFORE the epoch, not after it --------------------------
now=$(date +%s)
[ "$epoch" -gt $((now + CAP_BEFORE + 15)) ] || {
  echo "epoch $epoch is too soon: need > $((CAP_BEFORE + 15)) s of lead for the capture to go live first" >&2; exit 1; }
[ -x "$SUB" ]     || { echo "subscriber binary missing: $SUB" >&2; exit 1; }
if [ "$DIAG" = 1 ]; then
  # Fast-reader DLFs run ~8-13 MB/s. capture.sh refuses below 10 MB/s x window + 5 GB, and a
  # refused capture would abort the whole cycle here; drop only the modem log instead, so the
  # media, ping and counter data for the cell are still collected. MIN_FREE_GB adds headroom.
  need_gb=$(( (10 * cap_dur + 1023) / 1024 + 5 + ${MIN_FREE_GB:-20} ))
  free_gb=$(df -BG --output=avail "$HOME/diag-logs" 2>/dev/null | tail -1 | tr -dc 0-9)
  if [ -n "$free_gb" ] && [ "$free_gb" -lt "$need_gb" ]; then
    echo "only ${free_gb} GB free, ${need_gb} GB needed for a ${cap_dur}s DLF: running this cell with DIAG=0" >&2
    DIAG=0 DIAG_SKIPPED_DISK="free ${free_gb} GB < ${need_gb} GB"
  fi
fi
if [ "$DIAG" = 1 ]; then
  [ -x "$CAPTURE" ] || { echo "capture script missing: $CAPTURE" >&2; exit 1; }
  [ -c /dev/ttyUSB0 ] || { echo "DIAG port /dev/ttyUSB0 absent" >&2; exit 1; }
  # Match the interpreter, not the word. `pgrep -f qcsuper` also matches any shell whose
  # command text merely contains "qcsuper" -- including Claude tool shells and monitors
  # on this host -- and would refuse, losing the run. capture.sh uses the same pattern.
  QC_PROC='^[^ ]*python[0-9.]* [^ ]*qcsuper'
  if pgrep -f "$QC_PROC" >/dev/null; then
    echo "a qcsuper process already holds a DIAG port; not starting:" >&2; pgrep -af "$QC_PROC" >&2; exit 1
  fi
fi
# Over ssh: render on the console's display. GNOME's Xwayland writes a per-login auth
# file; without it the subscriber cannot open a window and logs no CSV rows.
export DISPLAY=${DISPLAY:-:0}
if ! xdpyinfo >/dev/null 2>&1; then
  XAUTHORITY=$(ls -t /run/user/"$(id -u)"/.mutter-Xwaylandauth.* 2>/dev/null | head -1)
  export XAUTHORITY
  xdpyinfo >/dev/null 2>&1 || { echo "cannot open display $DISPLAY (nobody logged in at Host B's console?)" >&2; exit 1; }
fi
cd "$REPO" || exit 1
set -a && . .livekit-demo/.env && set +a
: "${LIVEKIT_API_KEY:?not set after sourcing .livekit-demo/.env}"
: "${LIVEKIT_API_SECRET:?not set after sourcing .livekit-demo/.env}"
export SSL_CERT_FILE="${SSL_CERT_FILE:-$REPO/.livekit-demo/corp-ca.pem}"
mkdir -p "$outdir" "$HOME/diag-logs"
rm -f "$outdir/DONE"
: > "$outdir/timeline.txt"
trap finish EXIT
trap 'exit 130' INT TERM
say "label=$label epoch=$epoch room=$room duration=$((CELL_S - 5))s window=${cap_dur}s diag=$DIAG hops=$HOPS ping=$PING server=-h265"
[ -n "${DIAG_SKIPPED_DISK:-}" ] && say "DIAG SKIPPED for disk space: ${DIAG_SKIPPED_DISK}"
sid=$(loginctl list-sessions --no-legend 2>/dev/null | awk -v u="$(id -un)" '$3==u && /seat/ {print $1; exit}')
say "console session ${sid:-none}: $(loginctl show-session "${sid:-0}" -p Type -p LockedHint 2>/dev/null | paste -sd' ')"
[ "$(loginctl show-session "${sid:-0}" -p LockedHint --value 2>/dev/null)" = yes ] && \
  say "WARNING: console is LOCKED; rendering may stall and the CSV may be empty"
busy=$(pgrep -a -x 'curl|rsync|scp' 2>/dev/null)
[ -n "$busy" ] && say "WARNING: bulk-transfer processes running (contaminates the modem path): $(tr '\n' ';' <<<"$busy")"
say "clock pre-run:  $(sntp_offset)"

# ---- 1. modem capture and path recorders, live before the cell ---------------
python3 -c "import time;d=$epoch-$CAP_BEFORE-time.time()
if d>0: time.sleep(d)"
if [ "$HOPS" = 1 ]; then
  hops_sampler "$outdir/hops.csv" "$cap_dur" 2>"$outdir/hops.err" &
  hpid=$!
  say "hops sampler LIVE -> $outdir/hops.csv (1 Hz)"
fi
if [ "$PING" = 1 ]; then
  # 10.1.20.21 is the cluster ingress (signalling); media flows to the SFU node, which
  # Host A's wwan0 pcap showed as 10.1.20.16 on 2026-09-15. Ping both.
  for ip in ${PING_TARGETS:-10.1.20.16 $SFU_IP}; do
    ping -D -n -i 0.2 -W 1 -w "$cap_dur" "$ip" > "$outdir/ping-$ip.txt" 2>&1 &
    ppid_="$ppid_ $!"
  done
  say "ping LIVE -> $outdir/ping-<ip>.txt (${PING_TARGETS:-10.1.20.16 $SFU_IP}, 5 Hz)"
fi
if [ "$DIAG" = 1 ]; then
  before=$(ls -1 "$HOME"/diag-logs/"$label"-*.dlf 2>/dev/null | wc -l)
  "$CAPTURE" "$cap_dur" "$label" > "$outdir/capture.out" 2>&1 &
  cpid=$!
  for _ in $(seq 1 200); do
    kill -0 "$cpid" 2>/dev/null || { say "CAPTURE DID NOT START:"; cat "$outdir/capture.out" >&2; cpid=""; exit 1; }
    if [ "$(ls -1 "$HOME"/diag-logs/"$label"-*.dlf 2>/dev/null | wc -l)" -gt "$before" ]; then
      dlf=$(ls -1t "$HOME"/diag-logs/"$label"-*.dlf | head -1)
      [ -s "$dlf" ] && break
    fi
    sleep 0.1
  done
  [ -n "$dlf" ] && [ -s "$dlf" ] || { say "CAPTURE ALIVE BUT WROTE NOTHING in 20 s (letting it finish so diag-log-off runs)"; exit 1; }
  say "capture LIVE -> $dlf  (wall ms $(ms))"
  # A non-empty DLF is not proof: QCSuper can write a few records and exit at startup
  # (opcode-158 race, 2026-09-15). A good start logs "Enabled logging for" within ~1 s.
  for _ in $(seq 1 20); do grep -q 'Enabled logging for' "${dlf%.dlf}.log" 2>/dev/null && break; sleep 0.5; done
  grep -q 'Enabled logging for' "${dlf%.dlf}.log" 2>/dev/null \
    && say "capture logging ENABLED (QCSuper set the log mask)" \
    || say "WARNING: no 'Enabled logging for' in ${dlf%.dlf}.log after 10 s; capture.sh may be retrying (see capture_rc in DONE)"
else
  say "DIAG off for this cycle (no modem capture)"
fi

# ---- 2. subscriber, in the room before the publisher --------------------------
python3 -c "import time;d=$epoch-$SUB_BEFORE-time.time()
if d>0: time.sleep(d)"
if [ "${SUBSCRIBE:-1}" = 0 ]; then
  # No-video arm (e.g. S3 a/c): record the modem, counters and pings over the same window
  # with no room join, so nothing on this host's link comes from the media path.
  say "SUBSCRIBE=0: no subscriber this cell; recorders hold until the window ends"
  python3 -c "import time;d=$epoch+$CELL_S+25-time.time()
if d>0: time.sleep(d)"
  src="skipped"
else
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
  # A room lives on one SFU node: Host A's log must show this same SID.
  say "subscriber CONNECTED to $room  room_sid=$(grep -m1 -o 'Connected: .* - RM_[A-Za-z0-9]*' "$outdir/subscriber.log" | grep -o 'RM_[A-Za-z0-9]*')"
else
  say "SUBSCRIBER FAILED TO CONNECT (recorders continue; log below)"; tail -8 "$outdir/subscriber.log" >&2
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
  # Same-SFU proof, from the SFU itself: both hosts' identities under one room SID.
  timeout 20 python3 "$REPO/examples/local_video/scripts/tools/room_check.py" "$room" 2>&1 \
    | sed 's/^/  sfu: /' | tee -a "$outdir/timeline.txt"
else
  say "NO MEDIA by epoch+40 s -- the downlink was idle; this window does not cover a loaded link"
fi

fi  # end of the SUB=0 / subscriber branch opened in section 2

# ---- 4. hold for the cell, then let the recorders end on their own ------------
if [ -n "$spid" ]; then
  wait "$spid"; src=$?
  say "subscriber exited rc=$src  (4 = publisher unpublished cleanly, 124 = timeout)"
fi
if [ -n "$cpid" ]; then
  say "waiting for capture to finish and turn modem logging off..."
  wait "$cpid"; crc=$?; cpid=""
  say "capture exited rc=$crc"
fi
[ -n "$hpid" ] && { wait "$hpid"; hpid=""; }
[ -n "$ppid_" ] && { wait $ppid_; ppid_=""; }
post_clock=$(sntp_offset)
say "clock post-run: $post_clock"

# Per-second per-code modem-log summary for the whole window. Raw DLFs are 3-9 GB and never
# cross the PTP cable; this small CSV does. Seconds are relative to the epoch; the modem clock
# (network time) is put on the host clock with this run's measured host-minus-UTC.
if [ -n "$dlf" ] && [ -s "$dlf" ]; then
  hmu=$(sed -n 's/^local-UTC \([-+][0-9.]*\) ms.*/\1/p' <<<"$post_clock")
  hmu_s=$(awk -v m="${hmu:--4600}" 'BEGIN{printf "%.3f", m/1000}')
  if timeout 600 python3 "$REPO/examples/local_video/scripts/tools/dlf_rates.py" "$dlf" \
       --probe-start-ms $((epoch * 1000)) --probe-end-ms $(( (epoch + CELL_S) * 1000 )) \
       --host-minus-utc "$hmu_s" --before "$CAP_BEFORE" --after "$CAP_AFTER" \
       -o "$outdir/dlf-rates-hostb.csv" > "$outdir/dlf-rates.out" 2>&1; then
    say "dlf-rates-hostb.csv written (host-minus-UTC ${hmu_s} s; seconds relative to epoch)"
  else
    say "WARNING: dlf-rates summary failed; see dlf-rates.out"
  fi
fi

# ---- 5. what landed ----------------------------------------------------------
echo
[ -f "$outdir/capture.out" ] && cat "$outdir/capture.out" && echo
grep -E 'Decode health' "$outdir/subscriber.log" | tail -1
[ -f "$outdir/ping-sfu.txt" ] && tail -2 "$outdir/ping-sfu.txt" | tee -a "$outdir/timeline.txt"
python3 - "$outdir/subscriber.csv" "$epoch" "$outdir/timeline.txt" <<'PY'
import csv, os, sys
if not os.path.exists(sys.argv[1]):
    print("subscriber.csv: not written")
    raise SystemExit
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
    bins.setdefault(int(t // 60) * 60, []).append(b)
print(f"subscriber.csv: {len(rows)} rows, packets_lost {rows[-1].get('packets_lost')}, "
      f"resolution {rows[-1].get('frame_width')}x{rows[-1].get('frame_height')}")
print("receive bitrate by 60 s window, seconds from epoch (a grant collapse shows here):")
for k in sorted(bins):
    v = sorted(bins[k])
    print(f"  t+{k:>4}s  p50 {v[len(v)//2]:5.2f} Mbps   n={len(v)}")
PY
say "done. outdir=$outdir dlf=${dlf:-none}"
