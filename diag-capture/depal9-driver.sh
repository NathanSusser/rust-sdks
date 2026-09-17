#!/usr/bin/env bash
# The nine-cell codec x bitrate matrix on the new high-motion depal clip.
#
# 2 / 5 / 8 Mbps  x  H.264 / AV1 / H.265, nvenc, published A -> B, DIAG on BOTH hosts.
# Operator-requested 2026-09-17. One cell at a time, never overlapping, ~2 hours total.
#
# ORDER IS DELIBERATE: H.264 row, then AV1, then H.265 LAST, and the HEVC row is now
# KNOWN to be publisher-only. Measured by a 30 s probe at 2026-09-17T17:07:57Z rather
# than assumed:
#   A  negotiated h265 and encoded it on nvenc for real -- requested_codec=h265,
#      negotiated_codec=h265, codec_mime_type=video/H265, encoder_implementation=
#      "NVIDIA H265 Encoder", tier nvenc, across all 31 snapshots. No substitution, so
#      the CodecFallback guard correctly stayed silent.
#   B  SUBSCRIBED to the H265 track cleanly (sid TR_VCmZV8us6RQ6B9, 1600x1300, full 30 s)
#      and DECODED NOTHING. subscriber.csv header only, zero rows, and no "Decode health"
#      line anywhere in the log where an H.264 cell emits one per second. Media did arrive
#      -- 1,408,441 B / 4,252 pkt against a 3.0 kB/s idle baseline -- so this is a DECODER
#      GAP ON B, not an SFU or SDK refusal.
#   The 0.38 Mbps B received against 2 Mbps published is NOT a capacity result: with no
#   decoder consuming frames there is no PLI or keyframe feedback driving the sender.
# This confirms empirically what overnight-driver2.sh asserted in a comment ("B has no
# H.265 decoder (no NVIDIA GPU, no software path)") and never tested -- which is why
# 142 archived subscriber logs contain zero HEVC: B was only ever armed for h264 cells.
#
# So an HEVC cell yields a real A-side encoder comparison (encode time, bytes/frame, QP,
# pin behaviour) and a real modem capture, with an EMPTY subscriber half. That is a
# legitimate thing to want and a waste of 600 s + 6 GB if you do not. MAX_CELLS=6 runs
# the H.264 and AV1 rows only; the default runs all nine.
#
# WHAT PROTECTS THE CODEC LABEL. publish-cell.sh runs the harness with the negotiated-codec
# guard compiled in (run.rs:596, RunError::CodecFallback): if the SFU substitutes H.264 for
# a requested H.265, the cell FAILS rather than producing a report labelled with a codec it
# did not run. Verified present in the built binary, not just in source.
#
# TIMING CONSTRAINTS, both from Host B and both real:
#   LEAD >= 75 s   receive-around-cell.sh refuses to arm closer than CAP_BEFORE+15, because
#                  B's capture must be live and proven writing before the cell starts.
#                  120 s here.
#   >= 40 s between one capture ending and the next epoch, because capture.sh holds a flock
#                  on the DIAG port and cell N+1 gets no modem log if N still owns it. The
#                  serial structure below plus LEAD satisfies this with margin.
#
# NO CAPACITY PROBE BY DEFAULT. overnight-driver2.sh ran one in each gap; here PROBE=0.
# Our own uplink speed test is what caused every "grant collapse" on 2026-09-10, and nine
# cells is a lot of opportunity to repeat that. PROBE=1 restores the gap probe.
#
# Usage: [START_AT=n] [CELL=600] [DIAG=1] [COUNTERS=0] [PROBE=0] depal9-driver.sh [run-label]
set -uo pipefail

RUN=${1:-depal9-$(date -u +%Y-%m-%d)}
CELL=${CELL:-600}                 # seconds of video per cell
LEAD=${LEAD:-120}                 # seconds between arming and the epoch
DIAG=${DIAG:-1}                   # modem capture, both hosts
COUNTERS=${COUNTERS:-0}           # 5 ms driver-counter sampler on A (operator's call)
PROBE=${PROBE:-0}                 # capacity probe in the gap
START_AT=${START_AT:-1}           # resume at cell n after an interruption
MAX_CELLS=${MAX_CELLS:-0}         # 0 = run the whole matrix; 6 = H.264 and AV1 rows only
MIN_FREE_GB=${MIN_FREE_GB:-80}
MAX_FAILS=${MAX_FAILS:-3}
B=${B_HOST:-192.168.99.2}

CLIP=${CLIP:-/home/nsusser/teleop-media/depal-face-lower-20260904/depal-face-lower-src-30s.mp4}
export CLIP                       # publish-cell.sh reads this; long-run.sh only forwards the env

DIAG_DIR=$(cd "$(dirname "$0")" && pwd); REPO=$(cd "$DIAG_DIR/.." && pwd)
SFU_HOST=livekit-release-livekit-server-figure-ai-h265.apps.oai01.stc.edgeai.t-mobile.com
ROOT="$REPO/results/$RUN"; mkdir -p "$ROOT"
SUMMARY="$ROOT/summary.csv"
[ -f "$SUMMARY" ] || echo "n,label,codec,cap_kbps,diag,epoch,a_rc,b_done,pulled,pushed,dlf_records,host_minus_utc_ms,note" > "$SUMMARY"
log() { echo "$(date -u +%FT%TZ) $*" | tee -a "$ROOT/driver.log"; }
ssh_b() { timeout "${2:-30}" ssh -n -o BatchMode=yes -o ConnectTimeout=8 "$B" "$1"; }

[ -r "$CLIP" ] || { log "FATAL clip not readable: $CLIP"; exit 2; }
[ -x "$REPO/target/release/teleop-harness" ] || { log "FATAL harness not built"; exit 2; }

# The matrix, in the order argued for above.
MATRIX=(
  "h264 2000" "h264 5000" "h264 8000"
  "av1 2000"  "av1 5000"  "av1 8000"
  "h265 2000" "h265 5000" "h265 8000"
)

log "start $RUN cells=${#MATRIX[@]} cell=${CELL}s lead=${LEAD}s diag=$DIAG counters=$COUNTERS probe=$PROBE"
log "clip=$CLIP"
log "order: h264 row, av1 row, h265 row last (B has never decoded HEVC)"

fails=0; n=0
for spec in "${MATRIX[@]}"; do
  n=$((n + 1))
  [ "$MAX_CELLS" -eq 0 ] || [ "$n" -le "$MAX_CELLS" ] || { log "stopping at MAX_CELLS=$MAX_CELLS"; break; }
  [ "$n" -ge "$START_AT" ] || { log "cell $n skipped (START_AT=$START_AT)"; continue; }
  read -r codec cap <<<"$spec"

  free_gb=$(df -BG --output=avail "$REPO" | tail -1 | tr -dc 0-9)
  [ "$free_gb" -ge "$MIN_FREE_GB" ] || { log "only ${free_gb} GB free; stopping"; break; }
  [ "$fails" -lt "$MAX_FAILS" ] || { log "$fails consecutive failures; stopping"; break; }
  if fuser /dev/ttyUSB0 >/dev/null 2>&1; then
    log "  diag port still held; waiting up to 60 s"
    for _ in $(seq 12); do sleep 5; fuser /dev/ttyUSB0 >/dev/null 2>&1 || break; done
  fi

  label="$RUN-$codec-${cap}k"
  out="$ROOT/$label"; mkdir -p "$out"
  epoch=$(( $(date -u +%s) + LEAD ))
  span=$(( CELL + 150 ))
  b_out="examples/local_video/scripts/results/$RUN/$label"

  log "cell $n/${#MATRIX[@]}  $label  epoch=$(date -u -d @"$epoch" +%H:%M:%SZ)  cap=${cap}k"

  # Arm B for every cell, including H.265: if B cannot decode it we want that recorded
  # against this campaign rather than inferred, and B's modem capture is useful either way.
  ssh_b "cd ~/code/rust-sdks; DURATION=$CELL DIAG=$DIAG setsid nohup examples/local_video/scripts/tools/receive-around-cell.sh $label $epoch $label $b_out >/dev/null 2>&1 </dev/null &" 20 \
    || log "  WARN B arming ssh failed (rc=$?)"

  # A-side context recorders, all bounded by span so none outlives the cell.
  for ip in 10.1.20.16 10.1.20.21; do ping -D -i 0.2 -I wwan0 -w "$span" "$ip" > "$out/ping-$ip.txt" 2>&1 & done
  python3 "$DIAG_DIR/hop-ttl-probe.py" 10.1.20.16 "$span" "$out/hop-ttl.csv" 5 3 > "$out/hop-ttl.err" 2>&1 &
  "$DIAG_DIR/qdisc-10hz.sh" "$span" "$out/qdisc-10hz.csv" > /dev/null 2>&1 &
  [ "$COUNTERS" = 1 ] && "$DIAG_DIR/driver-counters-5ms.sh" "$span" "$out/driver-counters.csv" wwan0 4 > "$out/driver-counters.out" 2>&1 &
  ( sleep $(( epoch + 60 - $(date -u +%s) )); cd "$REPO" && set -a && . .livekit-demo/.env && set +a
    python3 teleop-test-matrix/scripts/lk_rooms.py --url "wss://$SFU_HOST" --room "$label" > "$out/rooms-epoch+60.txt" 2>&1 ) &

  ( cd "$REPO" && OUT="$out" DIAG=$DIAG "$DIAG_DIR/long-run.sh" "$label" "$epoch" "$CELL" "$cap" "$codec" ) > "$out/long-run.out" 2>&1
  a_rc=$?
  wait
  log "  A done rc=$a_rc"

  # B: wait for DONE (it means B's modem is quiet and the flock is released), then pull.
  b_done=no; pulled=na
  for _ in $(seq 72); do ssh_b "test -f ~/code/rust-sdks/$b_out/DONE" 10 && { b_done=yes; break; }; sleep 5; done
  if [ "$b_done" = yes ]; then
    mkdir -p "$out/hostb"
    # DLFs never cross the cable: multi-GB copies disturb PTP sync. CSVs and logs only.
    timeout 600 rsync -a -e "ssh -o BatchMode=yes" --exclude frames --exclude '*.dlf' \
      "$B:code/rust-sdks/$b_out/" "$out/hostb/" && pulled=yes || pulled=fail
  else
    log "  WARN B never wrote DONE"
  fi

  # Push A's media CSVs to B per cell so B can generate and sanity-check each report while
  # the next cell runs -- a problem then surfaces at cell 2, not after all nine.
  pushed=no
  if [ -s "$out/$label.pub.csv" ]; then
    ssh_b "mkdir -p ~/code/rust-sdks/$b_out/hosta" 15 \
      && timeout 300 rsync -a -e "ssh -o BatchMode=yes" \
           "$out/$label.pub.csv" "$out/$label.jsonl" "$out/$label.log" \
           "$B:code/rust-sdks/$b_out/hosta/" && pushed=yes || pushed=fail
  fi

  # Clock: A is a free-running PTP grandmaster, so host-minus-UTC is measured per cell
  # rather than assumed. dlf-rates needs it to put modem records on host time.
  off_ms=$(python3 - <<'PYO' 2>/dev/null
import socket, struct, time
E = 2208988800; res = []
for h in ('time.cloudflare.com', 'time.google.com', 'pool.ntp.org'):
    try:
        a = socket.getaddrinfo(h, 123, socket.AF_INET, socket.SOCK_DGRAM)[0][4]
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.settimeout(3)
        p = bytearray(48); p[0] = 0x23; t1 = time.time(); p[40:48] = struct.pack('!II', int(t1 + E), int((t1 % 1) * 2**32))
        s.sendto(p, a); d, _ = s.recvfrom(512); t4 = time.time()
        ts = lambda o: struct.unpack('!I', d[o:o+4])[0] - E + struct.unpack('!I', d[o+4:o+8])[0] / 2**32
        res.append(((ts(32) - t1) + (ts(40) - t4)) / 2)
    except Exception: pass
if res: res.sort(); print(f"{-res[len(res)//2]*1000:.1f}")
PYO
)
  dlf_n=
  if [ -s "$out/$label.dlf" ] && [ -n "$off_ms" ]; then
    python3 "$DIAG_DIR/dlf-rates.py" "$out/$label.dlf" "$epoch" "$((epoch + CELL))" \
      "$(awk -v m="$off_ms" 'BEGIN{printf "%.3f", m/1000}')" "$out/dlf-rates.csv" 60 > "$out/dlf-rates.out" 2>&1
    dlf_n=$(sed -n 's/.*records_total \([0-9]*\).*/\1/p' "$out/dlf-rates.out")
  fi

  [ "$PROBE" = 1 ] && PAY="$DIAG_DIR/up4m.bin" N=4 "$DIAG_DIR/probe-timed.sh" "$out/gap-probe-a.csv" > /dev/null 2>&1

  ok=1; note=
  [ "$a_rc" -eq 0 ] || { ok=0; note="A rc=$a_rc"; }
  if [ "$b_done" != yes ]; then
    if [ "$codec" = h265 ]; then note="${note:+$note; }B no DONE (HEVC receive unproven)"
    else ok=0; note="${note:+$note; }B no DONE"; fi
  fi
  [ -n "$dlf_n" ] || note="${note:+$note; }no dlf"
  [ $ok = 1 ] && fails=0 || fails=$((fails + 1))

  echo "$n,$label,$codec,$cap,$DIAG,$epoch,$a_rc,$b_done,$pulled,$pushed,${dlf_n},${off_ms},${note}" >> "$SUMMARY"
  log "  $label: a_rc=$a_rc b=$b_done pull=$pulled push=$pushed dlf=${dlf_n:-none} host-UTC=${off_ms:-?}ms ${note}"
  sleep 15
done
log "driver exit after $n cells"
