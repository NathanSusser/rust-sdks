#!/usr/bin/env bash
# S1: reproduce the 10 Sep spike with the queue located.
#
# Unpinned publisher (as on 10 Sep), the original uplink probe fired at epoch+120, and
# every discriminator on one clock: 5 Hz echo to the SFU media node (10.1.20.16) and
# the ingress (10.1.20.21), 5 Hz TTL-limited UDP to hops 2/3 (they ignore echo), the
# hop recorder with the modem DLF, and a RoomService participant list at epoch+60 as
# the same-SFU proof. Everything self-stops; DONE is written when all have exited.
#
# Usage: s1-arm.sh <label> <epoch> <duration_s>
set -uo pipefail
[ $# -ge 3 ] || { echo "usage: $0 <label> <epoch> <duration_s>" >&2; exit 2; }
label=$1 epoch=$2 dur=$3
DIAG_DIR=$(cd "$(dirname "$0")" && pwd); REPO=$(cd "$DIAG_DIR/.." && pwd)
SFU_HOST=livekit-release-livekit-server-figure-ai-h265.apps.oai01.stc.edgeai.t-mobile.com
out="$REPO/results/23-s1/$label"; mkdir -p "$out"; rm -f "$out/DONE"
now=$(date -u +%s)
[ "$epoch" -gt $((now + 45)) ] || { echo "epoch must be >= now+45" >&2; exit 2; }
span=$(( epoch - now + dur + 40 ))
echo "s1-arm $(date -u +%FT%TZ) label=$label epoch=$epoch dur=$dur probe_at=$((epoch+120)) UNPINNED(LK_PIN_BITRATE_TO_MAX=0,LK_MAX_START_BITRATE_KBPS=0)" > "$out/arm.log"

for ip in 10.1.20.16 10.1.20.21; do
  ping -D -i 0.2 -I wwan0 -w "$span" "$ip" > "$out/ping-$ip.txt" 2>&1 &
done
python3 "$DIAG_DIR/hop-ttl-probe.py" 10.1.20.16 "$span" "$out/hop-ttl.csv" 5 2,3 > "$out/hop-ttl.err" 2>&1 &

( sleep $(( epoch + 60 - $(date -u +%s) ))
  cd "$REPO" && set -a && . .livekit-demo/.env && set +a
  python3 teleop-test-matrix/scripts/lk_rooms.py --url "wss://$SFU_HOST" --room "$label" > "$out/rooms-epoch+60.txt" 2>&1 ) &

( cd "$REPO" && teleop-test-matrix/scripts/at-epoch.sh $((epoch + 120)) \
    env PAY="$DIAG_DIR/up4m.bin" N=4 "$DIAG_DIR/probe-timed.sh" "$out/probe.csv" ) > "$out/probe.err" 2>&1 &

( cd "$REPO" && OUT="$out" LK_PIN_BITRATE_TO_MAX=0 LK_MAX_START_BITRATE_KBPS=0 DIAG=1 \
    "$DIAG_DIR/long-run.sh" "$label" "$epoch" "$dur" 2000 h264 ) > "$out/long-run.out" 2>&1
echo "long-run rc=$? $(date -u +%FT%TZ)" >> "$out/arm.log"
wait
echo "done $(date -u +%FT%TZ)" >> "$out/arm.log"
touch "$out/DONE"
