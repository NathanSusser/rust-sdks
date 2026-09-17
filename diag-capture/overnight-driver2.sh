#!/usr/bin/env bash
# Overnight bottleneck loop: repeated cells until END, every hop recorded on both hosts,
# attributed per cycle, so the morning has an answer to "where is the bottleneck".
#
# Contract with Host B (rust-sdks-29): B's side is ONE wrapper B owns, launched here with a
# single ssh call per cycle; B writes <outdir>/DONE when everything has stopped; data is
# pulled over the cable only after DONE, never during a cell.
#
# Cycle k:
#   codec    h264 cells go to B (full two-host attribution). Every H265_EVERY-th cycle is h265,
#            published from A only: B has no H.265 decoder (no NVIDIA GPU, no software path).
#   diag     lockstep on both hosts: DLF on every DIAG_EVERY-th cycle (default 3, agreed with
#            rust-sdks-29 for Host B's disk), always an h264 cycle so both hosts capture together,
#            never an A-only h265 cycle. Off-cycles separate modem-log load from network effects.
#   A        pinned publisher (all three RUN-DISCIPLINE section 12 overrides, via publish-cell.sh)
#            + hop-recorder (qdisc/sysfs/QMI/signal 1 Hz, wwan0 pcap, optional DLF) + 5 Hz echo
#            to the SFU media node and ingress + 5 Hz TTL=3 hop probe + RoomService check at +60 s.
#   gap      after both sides are DONE: pull B, attribute, then a capped capacity probe on A
#            and B's gap-probe, never overlapping a cell.
# Stops at END_EPOCH, after MAX_FAILS consecutive failed cycles, or when disk runs low.
#
# DIAG SCHEDULE (driver2, agreed with rust-sdks-29 for the morning busy hour): the DLF budget
# on Host B is fixed, so capture is placed where the answer is:
#   epoch <  DIAG_OFF_FROM  : DIAG on cycles with k % DIAG_EVERY == 1 (h264 only), as before
#   DIAG_OFF_FROM <= epoch < DIAG_DENSE_FROM : DIAG off
#   epoch >= DIAG_DENSE_FROM : DIAG on EVERY h264 cycle
# START_K continues cycle numbering after a restart so labels never collide.
#
# Usage: [START_K=n] overnight-driver2.sh <night-label> <end_unix_s> [cell_s=600] [cap_kbps=2000]
set -uo pipefail
night=$1; END_EPOCH=$2; CELL=${3:-600}; CAP=${4:-2000}
H265_EVERY=${H265_EVERY:-3}; DIAG_EVERY=${DIAG_EVERY:-3}; MAX_FAILS=${MAX_FAILS:-3}; MIN_FREE_GB=${MIN_FREE_GB:-40}
DIAG_OFF_FROM=${DIAG_OFF_FROM:-1789473600}     # 12:00:00Z
DIAG_DENSE_FROM=${DIAG_DENSE_FROM:-1789482600} # 14:30:00Z
B=${B_HOST:-192.168.99.2}
DIAG_DIR=$(cd "$(dirname "$0")" && pwd); REPO=$(cd "$DIAG_DIR/.." && pwd)
SFU_HOST=livekit-release-livekit-server-figure-ai-h265.apps.oai01.stc.edgeai.t-mobile.com
ROOT="$REPO/results/24-overnight/$night"; mkdir -p "$ROOT"
SUMMARY="$ROOT/summary.csv"
[ -f "$SUMMARY" ] || echo "cycle,label,codec,diag,epoch,a_rc,b_done,pulled,collapse_s,b_frames_missing,b_pkts_lost,first_hops,a_probe_parallel_mbps,a_probe_single_mbps,b_probe_mbps,host_minus_utc_ms,qdisc_backlog_max,qdisc_backlog_s,dlf_records,note" > "$SUMMARY"
log() { echo "$(date -u +%FT%TZ) $*" | tee -a "$ROOT/driver.log"; }
ssh_b() { timeout "${2:-30}" ssh -n -o BatchMode=yes -o ConnectTimeout=8 "$B" "$1"; }

fails=0; k=$(( ${START_K:-1} - 1 ))
log "start(driver2) night=$night start_k=${START_K:-1} end=$(date -u -d @"$END_EPOCH" +%FT%TZ) cell=${CELL}s cap=${CAP}k h265_every=$H265_EVERY diag: k%$DIAG_EVERY==1 until $(date -u -d @"$DIAG_OFF_FROM" +%TZ), off until $(date -u -d @"$DIAG_DENSE_FROM" +%TZ), every h264 after; build=$(readlink "$DIAG_DIR/qcsuper-noroot")"
while :; do
  now=$(date -u +%s)
  [ $((now + CELL + 300)) -lt "$END_EPOCH" ] || { log "end window reached; stopping"; break; }
  free_gb=$(df -BG --output=avail "$REPO" | tail -1 | tr -dc 0-9)
  [ "$free_gb" -ge "$MIN_FREE_GB" ] || { log "only ${free_gb} GB free; stopping"; break; }
  [ "$fails" -lt "$MAX_FAILS" ] || { log "$fails consecutive failed cycles; stopping"; break; }

  k=$((k + 1))
  codec=h264; [ $((k % H265_EVERY)) -eq 0 ] && codec=h265
  epoch=$((now + 90))
  diag=0
  if [ "$codec" = h264 ]; then
    if   [ "$epoch" -ge "$DIAG_DENSE_FROM" ]; then diag=1
    elif [ "$epoch" -lt "$DIAG_OFF_FROM" ] && [ $(( k % DIAG_EVERY )) -eq 1 ]; then diag=1
    fi
  fi
  label="${night}-c$(printf %03d $k)-${codec}-d${diag}"
  out="$ROOT/$label"; mkdir -p "$out"
  span=$((CELL + 150))
  log "cycle $k $label epoch=$(date -u -d @"$epoch" +%TZ)"

  b_out="examples/local_video/scripts/results/overnight-cycles/$label"
  if [ "$codec" = h264 ]; then
    ssh_b "cd ~/code/rust-sdks; DURATION=$CELL DIAG=$diag setsid nohup examples/local_video/scripts/tools/receive-around-cell.sh $label $epoch $label $b_out >/dev/null 2>&1 </dev/null &" 20 \
      || log "  B launch ssh failed (rc=$?)"
  fi

  for ip in 10.1.20.16 10.1.20.21; do ping -D -i 0.2 -I wwan0 -w "$span" "$ip" > "$out/ping-$ip.txt" 2>&1 & done
  python3 "$DIAG_DIR/hop-ttl-probe.py" 10.1.20.16 "$span" "$out/hop-ttl.csv" 5 3 > "$out/hop-ttl.err" 2>&1 &
  "$DIAG_DIR/qdisc-10hz.sh" "$span" "$out/qdisc-10hz.csv" > /dev/null 2>&1 &
  ( sleep $((epoch + 60 - $(date -u +%s))); cd "$REPO" && set -a && . .livekit-demo/.env && set +a
    python3 teleop-test-matrix/scripts/lk_rooms.py --url "wss://$SFU_HOST" --room "$label" > "$out/rooms-epoch+60.txt" 2>&1 ) &

  ( cd "$REPO" && OUT="$out" DIAG=$diag "$DIAG_DIR/long-run.sh" "$label" "$epoch" "$CELL" "$CAP" "$codec" ) > "$out/long-run.out" 2>&1
  a_rc=$?
  wait
  log "  A done rc=$a_rc"

  b_done=na; pulled=na
  if [ "$codec" = h264 ]; then
    b_done=no
    for _ in $(seq 60); do ssh_b "test -f ~/code/rust-sdks/$b_out/DONE" 10 && { b_done=yes; break; }; sleep 5; done
    if [ "$b_done" = yes ]; then
      mkdir -p "$out/hostb"
      # DLFs stay on their host: multi-GB copies over the PTP cable disturb sync (agreed with
      # rust-sdks-29). Only CSVs/logs and B's per-second dlf-rates cross.
      timeout 300 rsync -a -e "ssh -o BatchMode=yes" --exclude frames --exclude '*.dlf' "$B:code/rust-sdks/$b_out/" "$out/hostb/" && pulled=yes || pulled=fail
    fi
  fi

  # Clock: A is a free-running PTP grandmaster; DLF alignment needs host-minus-UTC per cycle.
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
    python3 "$DIAG_DIR/dlf-rates.py" "$out/$label.dlf" "$epoch" "$((epoch + CELL))" "$(awk -v m="$off_ms" 'BEGIN{printf "%.3f", m/1000}')" "$out/dlf-rates.csv" 60 > "$out/dlf-rates.out" 2>&1
    dlf_n=$(sed -n 's/.*records_total \([0-9]*\).*/\1/p' "$out/dlf-rates.out")
  fi
  bl_max=; bl_s=
  if [ -s "$out/qdisc-10hz.csv" ]; then
    read -r bl_max bl_s < <(awk -F, -v e="$epoch" -v c="$CELL" 'NR>1 && $1/1000>=e && $1/1000<e+c { if ($7+0>m) m=$7+0; if ($7+0>10) { s[int($1/1000)]=1 } } END { n=0; for (k in s) n++; print m+0, n }' "$out/qdisc-10hz.csv")
  fi

  collapse=; missing=; lost=; hopsum=
  if [ -s "$out/$label.jsonl" ]; then
    args=(--cap-kbps "$CAP" --jsonl "$out/$label.jsonl" --pubcsv "$out/$label.pub.csv" --hops "$out/$label.hops.csv" --out "$out/attribution.csv")
    [ -s "$out/$label.wwan0.pcap" ] && args+=(--pcap "$out/$label.wwan0.pcap")
    [ -s "$out/$label.dlf" ] && args+=(--dlf "$out/$label.dlf")
    [ -s "$out/hostb/subscriber.csv" ] && args+=(--b-subcsv "$out/hostb/subscriber.csv")
    python3 "$REPO/teleop-test-matrix/scripts/hop_attribution.py" "${args[@]}" > "$out/attribution.txt" 2>&1
    collapse=$(sed -n 's/^collapse seconds.*: //p' "$out/attribution.txt")
    missing=$(sed -n 's/^frames missing at B: \([0-9]*\).*/\1/p' "$out/attribution.txt")
    lost=$(sed -n 's/.*packets lost at B: \([0-9]*\).*/\1/p' "$out/attribution.txt")
    hopsum=$(grep -E '^  [0-9]' "$out/attribution.txt" | sed -E 's/^  [^:]+: //' | sort | uniq -c | sort -rn | head -3 | sed -E 's/^ +//' | tr '\n' ';' | tr ',' ' ')
  fi

  # Gap: capacity on A, then on B -- never overlapping a cell.
  a_mbps=; b_mbps=
  PAY="$DIAG_DIR/up4m.bin" N=4 "$DIAG_DIR/probe-timed.sh" "$out/gap-probe-a.csv" > /dev/null 2>&1
  # Aggregate = sum of the PARALLEL streams only (the single stream is a separate lower bound;
  # adding it double-counted: the dry run reported 72.3 against 42.8 parallel + 29.5 single).
  a_mbps=$(awk -F, '/^parallel_end/ && $3 ~ /Mbps/ {gsub(/ Mbps/,"",$3); s+=$3; n++} END{if (n) printf "%.1f", s}' "$out/gap-probe-a.csv")
  a_single=$(awk -F, '/^single_end/ && $3 ~ /Mbps/ {gsub(/ Mbps/,"",$3); printf "%.1f", $3}' "$out/gap-probe-a.csv")
  if [ "$codec" = h264 ] && [ "$b_done" = yes ]; then
    ssh_b "cd ~/code/rust-sdks; examples/local_video/scripts/tools/gap-probe.sh $label $b_out" 60 > "$out/gap-probe-b.out" 2>&1
    b_mbps=$(grep -oE '[0-9]+(\.[0-9]+)? Mbps' "$out/gap-probe-b.out" | head -1 | cut -d' ' -f1)
  fi

  ok=1; note=
  [ "$a_rc" -eq 0 ] || { ok=0; note="A long-run rc=$a_rc"; }
  [ "$codec" = h265 ] || [ "$b_done" = yes ] || { ok=0; note="${note:+$note; }B not DONE"; }
  [ $ok = 1 ] && fails=0 || fails=$((fails + 1))
  echo "$k,$label,$codec,$diag,$epoch,$a_rc,$b_done,$pulled,${collapse},${missing},${lost},${hopsum},${a_mbps},${a_single},${b_mbps},${off_ms},${bl_max},${bl_s},${dlf_n},${note}" >> "$SUMMARY"
  log "  cycle $k summary: collapse=${collapse:-?}s missing=${missing:-?} lost=${lost:-?} backlog_max=${bl_max:-?} (${bl_s:-?} s>10) A=${a_mbps:-?}Mbps(par)/${a_single:-?}(single) B=${b_mbps:-?}Mbps host-UTC=${off_ms:-?}ms dlf=${dlf_n:-none} ${note}"
  sleep 20
done
log "driver exit"
