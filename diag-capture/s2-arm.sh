#!/usr/bin/env bash
# S2: which classifier before hop 3 lets ICMP skip the uplink queue that holds UDP?
#
# Pinned 2 Mbps H.264 (no backoff: the queue must grow or drop), a bigger dose (N parallel
# uploads at epoch+120, default 12), and paired 5 Hz probes to the SFU media node:
#   ICMP echo DSCP 0 | ICMP echo EF | UDP TTL=3 DSCP 0 | UDP TTL=3 EF | TCP SYN->RST (closed 3478) DSCP 0
# Readout: EF UDP escapes -> DSCP-aware classifier; only ICMP escapes regardless of DSCP ->
# protocol classifier; TCP tells whether it is "not UDP" or specifically ICMP.
# Plus 10 Hz qdisc (requeue burst), the hop recorder (DIAG per env), RoomService at +60.
#
# Usage: [N=12] [DIAG=1] s2-arm.sh <label> <epoch> <duration_s>
set -uo pipefail
[ $# -ge 3 ] || { echo "usage: $0 <label> <epoch> <duration_s>" >&2; exit 2; }
label=$1 epoch=$2 dur=$3; N=${N:-12}
DIAG_DIR=$(cd "$(dirname "$0")" && pwd); REPO=$(cd "$DIAG_DIR/.." && pwd)
SFU_HOST=livekit-release-livekit-server-figure-ai-h265.apps.oai01.stc.edgeai.t-mobile.com
MEDIA=10.1.20.16
out="$REPO/results/25-s2/$label"; mkdir -p "$out"; rm -f "$out/DONE"
now=$(date -u +%s)
[ "$epoch" -gt $((now + 45)) ] || { echo "epoch must be >= now+45" >&2; exit 2; }
span=$(( epoch - now + dur + 40 ))
echo "s2-arm $(date -u +%FT%TZ) label=$label epoch=$epoch dur=$dur N=$N probe_at=$((epoch+120)) PINNED DIAG=${DIAG:-1}" > "$out/arm.log"

ping -D -i 0.2 -I wwan0 -w "$span"          "$MEDIA" > "$out/ping-icmp-dscp0.txt" 2>&1 &
ping -D -i 0.2 -I wwan0 -w "$span" -Q 0xb8  "$MEDIA" > "$out/ping-icmp-ef.txt"    2>&1 &
python3 "$DIAG_DIR/hop-ttl-probe.py" "$MEDIA" "$span" "$out/udp-ttl3-dscp0.csv" 5 3 0x00 > "$out/udp-ttl3-dscp0.err" 2>&1 &
python3 "$DIAG_DIR/hop-ttl-probe.py" "$MEDIA" "$span" "$out/udp-ttl3-ef.csv"    5 3 0xb8 > "$out/udp-ttl3-ef.err"    2>&1 &
python3 "$DIAG_DIR/tcp-rtt-probe.py" "$MEDIA" 3478 "$span" "$out/tcp-rst-dscp0.csv" 5 0x00 > "$out/tcp-rst-dscp0.err" 2>&1 &
"$DIAG_DIR/qdisc-10hz.sh" "$span" "$out/qdisc-10hz.csv" > /dev/null 2>&1 &

( sleep $(( epoch + 60 - $(date -u +%s) ))
  cd "$REPO" && set -a && . .livekit-demo/.env && set +a
  python3 teleop-test-matrix/scripts/lk_rooms.py --url "wss://$SFU_HOST" --room "$label" > "$out/rooms-epoch+60.txt" 2>&1 ) &

( cd "$REPO" && teleop-test-matrix/scripts/at-epoch.sh $((epoch + 120)) \
    env PAY="$DIAG_DIR/up4m.bin" N="$N" "$DIAG_DIR/probe-timed.sh" "$out/probe.csv" ) > "$out/probe.err" 2>&1 &

( cd "$REPO" && OUT="$out" DIAG="${DIAG:-1}" "$DIAG_DIR/long-run.sh" "$label" "$epoch" "$dur" 2000 h264 ) > "$out/long-run.out" 2>&1
echo "long-run rc=$? $(date -u +%FT%TZ)" >> "$out/arm.log"
wait
echo "done $(date -u +%FT%TZ)" >> "$out/arm.log"
touch "$out/DONE"
