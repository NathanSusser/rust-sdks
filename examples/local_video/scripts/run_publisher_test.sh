#!/usr/bin/env bash
# Publisher side of a two-machine latency test.
#
# Run this on the SENDING machine, a few seconds AFTER the subscriber is up.
# Writes publisher.csv; copy it to the subscriber machine and run
# generate_frame_report.py there to get the PDF.
#
# Usage:
#   ./run_publisher_test.sh <ws-url> <room> [seconds] [outdir]
set -euo pipefail

URL="${1:?usage: $0 <ws-url> <room> [seconds] [outdir]}"
ROOM="${2:?usage: $0 <ws-url> <room> [seconds] [outdir]}"
SECONDS_TO_RUN="${3:-120}"
OUTDIR="${4:-./results}"

FPS=30
# Frames 0-59 are encoder ramp-up and bitrate probing; starting at 60 keeps that
# transient out of the statistics. The end bound is what makes the run stop by
# itself instead of needing a timer or Ctrl-C.
START_FRAME=60
END_FRAME=$(( START_FRAME + FPS * SECONDS_TO_RUN ))

: "${LIVEKIT_API_KEY:?set LIVEKIT_API_KEY}"
: "${LIVEKIT_API_SECRET:?set LIVEKIT_API_SECRET}"

mkdir -p "$OUTDIR"
BIN="$(dirname "$0")/../../../target/release/publisher"
[ -x "$BIN" ] || { echo "build first: cargo build --release -p local_video --features desktop --bin publisher --bin subscriber" >&2; exit 1; }

echo "publisher -> $ROOM on $URL"
echo "  frames $START_FRAME..$END_FRAME (~${SECONDS_TO_RUN}s at ${FPS}fps), exits on its own"
echo "  csv: $OUTDIR/publisher.csv"

# --test-pattern 1 rather than a camera: identical pixels on every host and every
# run, so a latency difference between two runs is the network, not the scene.
# --display-video opens the local preview. Deliberately no --display-timing: the
# extra diagnostics window is GPU work on the same machine that is encoding, and
# on a laptop that contention has been enough to stall the stream outright.
"$BIN" \
  --url "$URL" \
  --room-name "$ROOM" \
  --identity cam-1 \
  --test-pattern 1 \
  --width 1280 --height 720 --fps "$FPS" \
  --codec h264 \
  --burn-timestamp \
  --display-video \
  --log-csv "$OUTDIR/publisher.csv" \
  --log-start-frame-id "$START_FRAME" \
  --log-end-frame-id "$END_FRAME"

echo
echo "done: $OUTDIR/publisher.csv ($(wc -l < "$OUTDIR/publisher.csv") lines)"
echo "copy it to the subscriber machine, then run:"
echo "  ./run_report.sh $OUTDIR"
