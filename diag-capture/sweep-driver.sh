#!/usr/bin/env bash
# Anchored capacity-breakpoint sweep: find the bitrate where this link starts having issues.
#
# Operator's purpose, verbatim: "I want to pin it so I know when the network has issues at
# what bitrate. then I can configure based on that bitrate." So this LOCATES A KNEE. It is
# not a latency comparison, and the cell list is shaped by that.
#
# Design: teleop-test-matrix/docs/RERUN-DESIGN-2026-09-18.md (Host B, 8c5c191).
#
# WHY THE PIN STAYS ON. An unpinned sender backs off before it stresses the link, so it can
# never show where the link breaks. LK_PIN_BITRATE_TO_MAX raises the floor to meet
# --max-bitrate, and --degradation locked stops the encoder paying the forced rate in
# resolution or frame rate instead of bitrate. The harness source says it plainly: it is a
# measurement mode, not a control law, and it deliberately disables the mechanism that keeps
# a sender inside capacity. Cell 13 is the one unforced control.
#
# WHY THE SAME 2000k CELL RUNS FOUR TIMES. The ladder takes ~90 minutes and this uplink has
# documented sub-2 Mbps episodes on four separate dates, so capacity here is a SAMPLE, NOT A
# LEVEL. Without a yardstick, "4000k came back bad" and "the link got worse since we started"
# are indistinguishable. The anchors are that yardstick: if all four agree the sweep is valid;
# if one degrades, the ladder steps around it are VOID and get re-run rather than published as
# a breakpoint that is really a clock reading. This is not theoretical -- it is exactly the
# mistake made on 17 Sep, when h264 ran first and passed, AV1 ran later and failed, and the
# conclusion "AV1 is broken" was wrong twice over.
#   => Drop ladder steps to save time. NEVER drop an anchor.
#
# A COLLAPSED CELL IS A RESULT. On 17 Sep four cells collapsed and left almost nothing to
# analyse. Every cell here runs its full window with pcap armed before the epoch and stopped
# after it at BOTH ends, so the collapse itself is captured: the queue building, the
# retransmission burst, and the signalling timeout. NO EARLY ABORT.
#
# DIAG ON EVERY CELL, FULL MASK. This supersedes the line in the design doc that says not to.
# The ladder's validity rests on the four anchors agreeing, and when an anchor does NOT agree
# we need RSRP/RSRQ/SNR and RRC events to separate "radio conditions changed at 18:20" from
# "something in our stack changed". Without DIAG on the anchors a drifting anchor just voids
# cells and teaches us nothing. MASK=full; MASK=nr5g is sparse and cannot answer anything.
#
# REDUCE THEN DELETE. Full mask is ~8 MB/s, so ~2.4 GB per side per 300 s cell and ~31 GB per
# side for the sweep. After each cell: dlf-rates.py -> per-second CSV (~2 MB, crosses the
# cable fine), then delete the raw .dlf -- EXCEPT for a cell that COLLAPSED, where we may want
# to re-examine it. 35 GB of undecodable DLF already sits on Host B from earlier campaigns.
#
# ONE INSTRUMENT IS NOT MEASURABLE IN THIS TOPOLOGY AND MUST NEVER BE QUOTED.
# Control-path RTT and one-way figures require a teleop-harness peer at BOTH ends. Our
# topology pairs a harness publisher with the local_video `subscriber` binary, and
# examples/local_video/Cargo.toml has no teleop-test-matrix dependency, so that binary
# cannot speak the probe protocol no matter what flags are passed. The protocol itself is
# complete on both sides -- responder at run.rs:1026 / transport.rs:89, completer at
# run.rs:694 -- so this is a topology limitation with a known fix, NOT a bug and NOT
# dead code.
#
# What it looked like instead: probes_lost 93-99% in EVERY cell of 17 Sep including the
# flawless one, probes_completed 0, rtt_us_interval empty, and distinct_seq_received 0
# against seq_published 119,781. Those are not five symptoms; they are one absent peer
# counted five ways. probe.rs is a four-timestamp ROUND TRIP, so with nothing echoing
# every probe ages out at DEFAULT_PROBE_LIFETIME_US=2 s and is counted lost.
#
# So these figures are ABSENT BY DESIGN, not lost, and must not appear in any report
# from this sweep or from 17 Sep. The codebase already knows how to say this properly:
# ClockSyncConfidence::None suppresses the one-way and glass-to-glass columns while
# leaving the run valid. This metric said "99.7% lost" instead, which reads like a
# finding. Absent beats plausible-looking.
#
# The sweep does not depend on it: every threshold comes from retx, e2e p99, media onset
# and qdisc backlog, all validated. Measuring the control path would be a deliberate
# extra cell with teleop-harness at both ends, accepting the loss of Host B's per-frame
# render log for that cell. Not part of this campaign.
#
# Usage: [START_AT=n] [CELL=300] [DRY=1] sweep-driver.sh [run-label]
set -uo pipefail

RUN=${1:-sweep-$(date -u +%Y-%m-%d)}
CELL=${CELL:-300}                 # seconds of video per cell
LEAD=${LEAD:-120}                 # arming lead; Host B refuses under 75 s
GAP=${GAP:-120}                   # minimum between cells so the DIAG port lock clears
START_AT=${START_AT:-1}
# Stop cleanly after N cells. Used to run the pre-flight cell alone: the instruments must
# be read against one known-good cell before the remaining 80 minutes are committed. Never
# interrupt a running cell instead -- killing capture.sh skips diag-log-off and leaves the
# modem streaming into nothing, which also makes the NEXT capture start dirty.
MAX_CELLS=${MAX_CELLS:-0}         # 0 = whole ladder
DRY=${DRY:-0}
MIN_FREE_GB=${MIN_FREE_GB:-60}
B=${B_HOST:-192.168.99.2}
CLIP=${CLIP:-/home/nsusser/teleop-media/depal-face-lower-20260904/depal-face-lower-src-30s.mp4}
export CLIP

DIAG_DIR=$(cd "$(dirname "$0")" && pwd); REPO=$(cd "$DIAG_DIR/.." && pwd)
# SFU deployment and the media/ingress node IPs the path probes target. All overridable,
# none defaulted to $LIVEKIT_URL -- see publish-cell.sh for why that default would be unsafe.
# THE NODE IPs MATTER AS MUCH AS THE URL: 10.1.20.16/.21 are -h265 nodes, so on another
# deployment the ping and TTL rows would measure a host that is NOT in the media path and
# report a perfectly healthy RTT while the actual SFU struggled. Set them with the URL or
# treat those rows as void.
SFU_HOST=${LK_SFU_HOST:-livekit-release-livekit-server-figure-ai-h265.apps.oai01.stc.edgeai.t-mobile.com}
SFU_MEDIA_IP=${LK_SFU_MEDIA_IP:-10.1.20.16}
SFU_INGRESS_IP=${LK_SFU_INGRESS_IP:-10.1.20.21}
ROOT="$REPO/results/$RUN"; mkdir -p "$ROOT"
SUMMARY="$ROOT/summary.csv"
[ -f "$SUMMARY" ] || echo "n,label,codec,cap_kbps,forced,epoch,a_rc,b_done,a_pcap,b_pcap,pulled,pushed,retx_pct,dlf_records,dlf_kept,host_minus_utc_ms,verdict,note" > "$SUMMARY"
log() { echo "$(date -u +%FT%TZ) $*" | tee -a "$ROOT/driver.log"; }
ssh_b() { timeout "${2:-30}" ssh -n -o BatchMode=yes -o ConnectTimeout=8 "$B" "$1"; }

# label  codec  cap_kbps  forced(1/0)  role
CELLS=(
  "h264-2000k-anchor-a  h264 2000 1 anchor"
  "h264-2500k           h264 2500 1 ladder"
  "h264-3000k           h264 3000 1 ladder"
  "h264-2000k-anchor-b  h264 2000 1 anchor"
  "h264-3500k           h264 3500 1 ladder"
  "h264-4000k           h264 4000 1 ladder"
  "h264-2000k-anchor-c  h264 2000 1 anchor"
  "h264-4500k           h264 4500 1 ladder"
  "h264-5000k           h264 5000 1 ladder"
  "h264-2000k-anchor-d  h264 2000 1 anchor"
  "av1-2000k            av1  2000 1 av1"
  "av1-3000k            av1  3000 1 av1"
  "h264-2000k-unpinned  h264 2000 0 control"
)

[ -r "$CLIP" ] || { log "FATAL clip unreadable: $CLIP"; exit 2; }
[ -x "$REPO/target/release/teleop-harness" ] || { log "FATAL harness not built"; exit 2; }
ssh_b "test -x ~/diag-capture/pcap.sh" 15 || log "WARN B has no pcap.sh -- B-side packet capture will be MISSING"
ssh_b "getcap /usr/bin/tcpdump | grep -q cap_net_raw" 15 || log "WARN B tcpdump lacks cap_net_raw -- B-side pcap will fail"

log "start $RUN cells=${#CELLS[@]} cell=${CELL}s lead=${LEAD}s gap=${GAP}s diag=full-mask-every-cell"
log "sfu=$SFU_HOST media=$SFU_MEDIA_IP ingress=$SFU_INGRESS_IP"   # read back, never asserted
log "anchors at cells 1,4,7,10 -- if one degrades the steps around it are VOID, not a breakpoint"

# SIM advances only under DRY so the preview shows a realistic schedule; 0 in a real run.
n=0; SIM=0
for spec in "${CELLS[@]}"; do
  n=$((n + 1))
  [ "$MAX_CELLS" -eq 0 ] || [ "$n" -le "$MAX_CELLS" ] || { log "stopping cleanly at MAX_CELLS=$MAX_CELLS"; break; }
  read -r label codec cap forced role <<<"$spec"
  [ "$n" -ge "$START_AT" ] || { log "cell $n $label skipped (START_AT=$START_AT)"; continue; }

  free_gb=$(df -BG --output=avail "$REPO" | tail -1 | tr -dc 0-9)
  [ "$free_gb" -ge "$MIN_FREE_GB" ] || { log "only ${free_gb} GB free; stopping"; break; }
  if fuser /dev/ttyUSB0 >/dev/null 2>&1; then
    log "  DIAG port still held; waiting up to 90 s for the lock to clear"
    for _ in $(seq 18); do sleep 5; fuser /dev/ttyUSB0 >/dev/null 2>&1 || break; done
  fi

  full="$RUN-$label"
  out="$ROOT/$full"; mkdir -p "$out"
  # SIM is 0 in a real run: each cell blocks ~CELL seconds inside long-run.sh before the
  # next epoch is computed. It only advances under DRY, so the preview shows the schedule
  # a human can actually sanity-check rather than thirteen identical start times.
  epoch=$(( $(date -u +%s) + LEAD + SIM ))
  span=$(( CELL + 150 ))
  b_out="examples/local_video/scripts/results/$RUN/$full"

  log "cell $n/${#CELLS[@]}  $full  [$role]  cap=${cap}k forced=$forced  epoch=$(date -u -d @"$epoch" +%H:%M:%SZ)"
  if [ "$DRY" = 1 ]; then
    log "  DRY: would arm B pcap + subscriber, run ${CELL}s forced=$forced, pull, reduce"
    SIM=$(( SIM + LEAD + CELL + GAP + 60 ))
    continue
  fi

  # Host B: packet capture first (its own lock), then the subscriber+DIAG wrapper.
  # ARM, THEN VERIFY -- never trust the ssh exit code here. `setsid nohup ... &` detaches on
  # B and keeps running, but ssh can hold the channel open past our timeout (pcap.sh's flock
  # fd survives into the child), so `timeout` kills the CLIENT and returns 124 while the
  # remote capture is running perfectly. Cell 1 of 2026-09-17 logged exactly that false
  # WARN while B's tcpdump was already writing a 445 KB file. A wrong warning is worse than
  # none: it teaches you to ignore the warning that is eventually real. So the arm is
  # fire-and-forget and the CHECK is a positive observation of the remote process.
  ssh_b "cd ~ && setsid nohup diag-capture/pcap.sh $span $full wwan0 >/dev/null 2>&1 </dev/null &" 20 >/dev/null 2>&1
  b_pcap=no
  for _ in 1 2 3 4 5 6; do
    if ssh_b "pgrep -f 'tcpdump.*$full' >/dev/null" 10; then b_pcap=yes; break; fi
    sleep 2
  done
  [ "$b_pcap" = yes ] && log "  B pcap verified running" \
                      || log "  WARN B pcap NOT observed after arming -- this cell will be single-ended"
  ssh_b "cd ~/code/rust-sdks; DURATION=$CELL DIAG=1 setsid nohup examples/local_video/scripts/tools/receive-around-cell.sh $full $epoch $full $b_out >/dev/null 2>&1 </dev/null &" 20 \
    || log "  WARN B arming failed (rc=$?)"

  for ip in "$SFU_MEDIA_IP" "$SFU_INGRESS_IP"; do ping -D -i 0.2 -I wwan0 -w "$span" "$ip" > "$out/ping-$ip.txt" 2>&1 & done
  python3 "$DIAG_DIR/hop-ttl-probe.py" "$SFU_MEDIA_IP" "$span" "$out/hop-ttl.csv" 5 3 > "$out/hop-ttl.err" 2>&1 &
  "$DIAG_DIR/qdisc-10hz.sh" "$span" "$out/qdisc-10hz.csv" > /dev/null 2>&1 &
  "$DIAG_DIR/driver-counters-5ms.sh" "$span" "$out/driver-counters.csv" wwan0 4 > "$out/driver-counters.out" 2>&1 &
  ( sleep $(( epoch + 60 - $(date -u +%s) )); cd "$REPO" && set -a && . .livekit-demo/.env && set +a
    python3 teleop-test-matrix/scripts/lk_rooms.py --url "wss://$SFU_HOST" --room "$full" > "$out/rooms-epoch+60.txt" 2>&1 ) &

  # forced=1 -> pin the floor up to the cap; forced=0 -> the unforced control cell.
  ( cd "$REPO" && OUT="$out" DIAG=1 LK_PIN_BITRATE_TO_MAX="$forced" \
      "$DIAG_DIR/long-run.sh" "$full" "$epoch" "$CELL" "$cap" "$codec" ) > "$out/long-run.out" 2>&1
  a_rc=$?
  wait
  log "  A done rc=$a_rc"

  # VERIFY A'S OWN CAPTURE. The B-side check above watches the far end; nothing watched this
  # one, which is an asymmetry a paired campaign cannot afford: a run that captures at one end
  # only LOOKS complete and the paired RTP join -- the whole point of the design -- silently
  # has nothing to join. hop-recorder reports its capability decision into long-run.out, and
  # the file itself is the proof, so check both.
  a_pcap=no
  if [ -s "$out/$full.wwan0.pcap" ]; then
    apsz=$(stat -c%s "$out/$full.wwan0.pcap")
    [ "$apsz" -gt 100000 ] && a_pcap=yes
    log "  A pcap $( [ "$a_pcap" = yes ] && echo verified || echo "SUSPICIOUS" ): $apsz bytes"
  else
    log "  ERROR A pcap MISSING -- this cell is SINGLE-ENDED and cannot be joined with B"
    grep -m1 'recording:' "$out/long-run.out" 2>/dev/null | sed 's/^/    /' | tee -a "$ROOT/driver.log"
  fi
  if [ "$a_pcap" != yes ] || [ "$b_pcap" != yes ]; then
    log "  WARNING cell $n is not paired (A pcap=$a_pcap, B pcap=$b_pcap) -- attribution unavailable"
  fi

  b_done=no; pulled=na
  for _ in $(seq 60); do ssh_b "test -f ~/code/rust-sdks/$b_out/DONE" 10 && { b_done=yes; break; }; sleep 5; done
  if [ "$b_done" = yes ]; then
    mkdir -p "$out/hostb"
    timeout 600 rsync -a -e "ssh -o BatchMode=yes" --exclude frames --exclude '*.dlf' \
      "$B:code/rust-sdks/$b_out/" "$out/hostb/" && pulled=yes || pulled=fail
    # B's pcap.sh writes to ~/pcap-logs/, OUTSIDE the cell tree the rsync above walks, so the
    # pull silently lands no capture and the paired RTP join -- the whole point of the design --
    # has nothing to join. Found by hand after cell 1 on 2026-09-17, fixed by hand for that cell
    # only, and it then recurred on the very next cell. Pull it explicitly.
    timeout 300 rsync -a -e "ssh -o BatchMode=yes" \
      "$B:pcap-logs/${full}-*.pcap" "$out/hostb/" 2>/dev/null \
      && log "  B pcap pulled into hostb/" \
      || log "  WARN B pcap NOT pulled -- paired join unavailable for this cell"
  else
    log "  WARN B never wrote DONE"
  fi

  pushed=no
  if [ -s "$out/$full.pub.csv" ]; then
    ssh_b "mkdir -p ~/code/rust-sdks/$b_out/hosta" 15 \
      && timeout 300 rsync -a -e "ssh -o BatchMode=yes" \
           "$out/$full.pub.csv" "$out/$full.jsonl" "$out/$full.log" \
           "$B:code/rust-sdks/$b_out/hosta/" && pushed=yes || pushed=fail
  fi

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

  # Collapse signature: a signalling ping timeout or resume anywhere in the publisher log.
  collapsed=0
  grep -qiE 'ping timeout|resuming connection|publisher reconnecting' "$out/$full.log" 2>/dev/null && collapsed=1

  dlf_n=; dlf_kept=no
  if [ -s "$out/$full.dlf" ] && [ -n "$off_ms" ]; then
    python3 "$DIAG_DIR/dlf-rates.py" "$out/$full.dlf" "$epoch" "$((epoch + CELL))" \
      "$(awk -v m="$off_ms" 'BEGIN{printf "%.3f", m/1000}')" "$out/dlf-rates.csv" 60 > "$out/dlf-rates.out" 2>&1
    dlf_n=$(sed -n 's/.*records_total \([0-9]*\).*/\1/p' "$out/dlf-rates.out")
    # KEEP THE RAW CAPTURE BY DEFAULT. The earlier rule deleted it unless the cell had a
    # signalling ping timeout, and on 2026-09-17 that destroyed the raw DIAG for a cell
    # scored FAILING at 46% retransmission with B receiving nothing -- precisely the cell
    # whose radio behaviour we then wanted to examine. Per-second dlf-rates counts survive
    # deletion, but the SUB-SECOND scheduling cadence (0xB883 inter-arrival) needs raw
    # records and cannot be recovered. A cell that failed is the one worth keeping.
    # Set REDUCE_DELETE=1 to reclaim disk, and even then only a CLEAN cell is deleted.
    if [ "$collapsed" = 1 ]; then
      dlf_kept=yes; log "  COLLAPSED -- keeping raw DLF for re-examination"
    elif [ "${REDUCE_DELETE:-0}" != 1 ]; then
      dlf_kept=yes; log "  keeping raw DLF ($dlf_n records); set REDUCE_DELETE=1 to reclaim disk"
    elif [ -n "$dlf_n" ] && [ "$b_done" = yes ] && [ "$a_pcap" = yes ] && [ "$b_pcap" = yes ]; then
      rm -f "$out/$full.dlf"; log "  reduced to dlf-rates.csv ($dlf_n records), raw DLF deleted"
    else
      dlf_kept=yes; log "  cell not fully paired -- keeping raw DLF"
    fi
  fi

  retx=$(python3 - "$out/$full.jsonl" <<'EOF' 2>/dev/null
import sys, json
sent=retr=0
for line in open(sys.argv[1]):
    if '"retransmitted' not in line: continue
    try: d=json.loads(line)
    except Exception: continue
    def dig(o):
        if isinstance(o,dict):
            if 'packets_sent' in o and 'retransmitted_packets_sent' in o: return o
            for v in o.values():
                r=dig(v)
                if r: return r
        elif isinstance(o,list):
            for v in o:
                r=dig(v)
                if r: return r
    b=dig(d)
    if b:
        sent=b.get('packets_sent') or sent
        retr=b.get('retransmitted_packets_sent') or retr
print(f"{100.0*retr/sent:.2f}" if sent else "")
EOF
)

  # Verdict per the design's thresholds. QUEUEING is the number the sweep exists to find.
  verdict=UNKNOWN
  if [ "$collapsed" = 1 ]; then verdict=COLLAPSED
  elif [ -n "$retx" ]; then
    awk -v r="$retx" 'BEGIN{exit !(r>=10)}' && verdict=FAILING
    [ "$verdict" = UNKNOWN ] && { awk -v r="$retx" 'BEGIN{exit !(r>=0.5)}' && verdict=QUEUEING; }
    [ "$verdict" = UNKNOWN ] && verdict=CLEAN
  fi

  note=; [ "$a_rc" -eq 0 ] || note="A rc=$a_rc"
  [ "$b_done" = yes ] || note="${note:+$note; }B no DONE"
  echo "$n,$full,$codec,$cap,$forced,$epoch,$a_rc,$b_done,$a_pcap,$b_pcap,$pulled,$pushed,${retx},${dlf_n},${dlf_kept},${off_ms},${verdict},${note}" >> "$SUMMARY"
  log "  $full: $verdict  retx=${retx:-?}%  dlf=${dlf_n:-none} kept=$dlf_kept  host-UTC=${off_ms:-?}ms  ${note}"
  [ "$role" = anchor ] && log "  ANCHOR $full -> $verdict (compare against the other anchors before trusting any ladder step)"

  sleep "$GAP"
done
log "sweep exit after $n cells"
log "NEXT: compare the four anchors. If they disagree, the ladder measured the hour, not the bitrate."
