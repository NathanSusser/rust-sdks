#!/usr/bin/env bash
# S3: is the uplink coupling between A and B a shared-cell scheduler effect?
#
# Three arms, DIAG on both hosts, same A-side instrumentation in each:
#   a  A uploads N parallel at epoch+60, NO video on either host  -> does B's modem still step?
#   b  A streams pinned 2 Mbps H.264 to B; B uploads N at epoch+60 -> does A's delay/backlog rise?
#                                                                     (another UE's load cutting A's uplink)
#   c  B uploads N at epoch+60, NO video, A only records           -> mirror of (a): does A's modem step?
# Every arm: 5 Hz ICMP / UDP TTL=3 / TCP SYN->RST to the SFU media node, 10 Hz qdisc, the hop
# recorder with the modem DLF, and A's serving cell (QMI) read before and after the arm.
#
# Usage: [N=12] s3-arm.sh <a|b|c> <label> <epoch> [duration_s=120]
set -uo pipefail
[ $# -ge 3 ] || { echo "usage: $0 <a|b|c> <label> <epoch> [duration_s]" >&2; exit 2; }
arm=$1 label=$2 epoch=$3 dur=${4:-120}; N=${N:-12}
case "$arm" in a|b|c) ;; *) echo "arm must be a, b or c" >&2; exit 2;; esac
DIAG_DIR=$(cd "$(dirname "$0")" && pwd); REPO=$(cd "$DIAG_DIR/.." && pwd)
MEDIA=10.1.20.16; QMI=${QMI:-/dev/cdc-wdm8}
out="$REPO/results/26-s3/$label"; mkdir -p "$out"; rm -f "$out/DONE"
now=$(date -u +%s)
[ "$epoch" -gt $((now + 45)) ] || { echo "epoch must be >= now+45" >&2; exit 2; }
span=$(( epoch - now + dur + 40 ))
echo "s3-arm $(date -u +%FT%TZ) arm=$arm label=$label epoch=$epoch dur=$dur N=$N build=$(readlink "$DIAG_DIR/qcsuper-noroot")" > "$out/arm.log"

cell() { { date -u +%FT%TZ; sudo -n qmicli -d "$QMI" -p --nas-get-cell-location-info 2>&1 | grep -iE '5GNR ARFCN|Physical Cell ID|Global Cell ID|Tracking Area|PLMN|RSRP' ; } > "$out/$1"; }
cell cell-before.txt

ping -D -i 0.2 -I wwan0 -w "$span" "$MEDIA" > "$out/ping-icmp-dscp0.txt" 2>&1 &
python3 "$DIAG_DIR/hop-ttl-probe.py" "$MEDIA" "$span" "$out/udp-ttl3-dscp0.csv" 5 3 0x00 > "$out/udp-ttl3-dscp0.err" 2>&1 &
python3 "$DIAG_DIR/tcp-rtt-probe.py" "$MEDIA" 3478 "$span" "$out/tcp-rst-dscp0.csv" 5 0x00 > "$out/tcp-rst-dscp0.err" 2>&1 &
"$DIAG_DIR/qdisc-10hz.sh" "$span" "$out/qdisc-10hz.csv" > /dev/null 2>&1 &

if [ "$arm" = a ]; then
  ( cd "$REPO" && teleop-test-matrix/scripts/at-epoch.sh $((epoch + 60)) \
      env PAY="$DIAG_DIR/up4m.bin" N="$N" "$DIAG_DIR/probe-timed.sh" "$out/probe.csv" ) > "$out/probe.err" 2>&1 &
fi

if [ "$arm" = b ]; then
  ( cd "$REPO" && OUT="$out" DIAG=1 "$DIAG_DIR/long-run.sh" "$label" "$epoch" "$dur" 2000 h264 ) > "$out/long-run.out" 2>&1
  echo "long-run rc=$? $(date -u +%FT%TZ)" >> "$out/arm.log"
else
  # No publisher: the recorder alone covers the arm (DIAG on), stopping itself at the span end.
  DIAG=1 "$DIAG_DIR/hop-recorder.sh" "$label" "$span" "$out" > "$out/recorder.out" 2>&1
  echo "recorder rc=$? $(date -u +%FT%TZ)" >> "$out/arm.log"
fi
wait
cell cell-after.txt
echo "done $(date -u +%FT%TZ)" >> "$out/arm.log"
touch "$out/DONE"
