#!/usr/bin/env bash
# Subscriber side of a two-machine latency test.
#
# Run this on the RECEIVING machine FIRST, then start the publisher.
#
# Needs a display. The subscriber writes a CSV row when a frame completes on the
# GPU, so with no window it renders nothing and the CSV stays empty apart from its
# header. Over SSH, export DISPLAY=:0 (and `xhost +si:localuser:$USER` on the
# console) so the window opens on the machine's own screen.
#
# Usage:
#   ./run_subscriber_test.sh <ws-url> <room> [seconds] [outdir]
set -euo pipefail

URL="${1:?usage: $0 <ws-url> <room> [seconds] [outdir]}"
ROOM="${2:?usage: $0 <ws-url> <room> [seconds] [outdir]}"
SECONDS_TO_RUN="${3:-120}"
OUTDIR="${4:-./results}"

FPS=30
START_FRAME=60
END_FRAME=$(( START_FRAME + FPS * SECONDS_TO_RUN ))

: "${LIVEKIT_API_KEY:?set LIVEKIT_API_KEY}"
: "${LIVEKIT_API_SECRET:?set LIVEKIT_API_SECRET}"

if [ -z "${DISPLAY:-}" ] && [ -z "${WAYLAND_DISPLAY:-}" ]; then
  echo "ERROR: no DISPLAY set. The subscriber logs on GPU render completion, so a" >&2
  echo "headless run produces an empty CSV. Try: DISPLAY=:0 $0 $*" >&2
  exit 1
fi

mkdir -p "$OUTDIR"
BIN="$(dirname "$0")/../../../target/release/subscriber"
[ -x "$BIN" ] || { echo "build first: cargo build --release -p local_video --features desktop --bin publisher --bin subscriber" >&2; exit 1; }

echo "subscriber -> $ROOM on $URL"
echo "  frames $START_FRAME..$END_FRAME (~${SECONDS_TO_RUN}s at ${FPS}fps), exits on its own"
echo "  csv: $OUTDIR/subscriber.csv"
echo "  start the publisher now (or within ~30s)"

# The video window is always shown. --display-timestamp adds the diagnostics
# window with the live per-stage timing readout, which is what makes the pipeline
# visible during a demo. It is a second window doing GPU work on the machine being
# measured: fine on a desktop GPU, but if frames stall or the CSV stops growing
# mid-run, drop this flag first -- on a laptop it has been enough to kill the
# stream. Set SHOW_TIMING=0 to omit it.
TIMING_FLAG=()
[ "${SHOW_TIMING:-1}" = "1" ] && TIMING_FLAG=(--display-timestamp)

"$BIN" \
  --url "$URL" \
  --room-name "$ROOM" \
  --identity viewer-1 \
  --participant cam-1 \
  "${TIMING_FLAG[@]}" \
  --log-csv "$OUTDIR/subscriber.csv" \
  --log-start-frame-id "$START_FRAME" \
  --log-end-frame-id "$END_FRAME"

ROWS=$(( $(wc -l < "$OUTDIR/subscriber.csv") - 1 ))
echo
echo "done: $OUTDIR/subscriber.csv ($ROWS frames)"
if [ "$ROWS" -le 0 ]; then
  echo "WARNING: no frames logged. Either no video arrived, or nothing rendered" >&2
  echo "(check DISPLAY). A report needs rows on both sides." >&2
fi
