#!/usr/bin/env bash
# Host B side of one S3 arm (shared-cell coupling test). One call per arm, launched by
# epoch-75 s at the latest; blocks until the arm is done.
#
#   a  Host A uploads, no video anywhere:  B records modem + counters + pings, no subscriber
#   b  Host A streams to B, B uploads:      B subscribes AND runs the upload at epoch+60 s
#   c  B uploads, no video anywhere:        B records, no subscriber, uploads at epoch+60 s
#
# The upload is upload-at.sh: 12 x 4,000,000-byte uploads then one single, --max-time 25,
# phases logged in unix ms to <outdir>/probe.csv (Host A's probe.csv format).
#
# Usage: s3-hostb-arm.sh <a|b|c> <epoch> [duration_s=120]
set -uo pipefail
[ $# -ge 2 ] || { echo "usage: $0 <a|b|c> <epoch> [duration_s]" >&2; exit 2; }
arm=$1 epoch=$2 dur=${3:-120}
TOOLS=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$TOOLS/../../../.." && pwd)
# Labels match Host A's s3-arm.sh; arm b's room is its label, so the subscriber joins A's room.
case "$arm" in
  a) sub=0; upload=0; label="s3a-upload-a-novideo" ;;
  b) sub=1; upload=1; label="s3b-stream-a-upload-b" ;;
  c) sub=0; upload=1; label="s3c-upload-b-novideo" ;;
  *) echo "arm must be a, b or c" >&2; exit 2 ;;
esac
outdir="$REPO/examples/local_video/scripts/results/overnight-cycles/${label}-${epoch}"
mkdir -p "$outdir"

SUBSCRIBE=$sub DURATION=$dur DIAG=1 "$TOOLS/receive-around-cell.sh" "$label" "$epoch" "$label" "$outdir" \
  > "$outdir/wrapper.out" 2>&1 &
wpid=$!
upid=""
if [ "$upload" = 1 ]; then
  "$TOOLS/upload-at.sh" $(( (epoch + 60) * 1000 )) "$outdir" 12 25 > "$outdir/upload.out" 2>&1 &
  upid=$!
fi
wait "$wpid"; wrc=$?
urc=""
[ -n "$upid" ] && { wait "$upid"; urc=$?; }
echo "arm $arm epoch $epoch: wrapper rc=$wrc upload rc=${urc:-n/a} outdir=$outdir"
