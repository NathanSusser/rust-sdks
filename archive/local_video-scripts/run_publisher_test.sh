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
WIDTH="${WIDTH:-1280}"
HEIGHT="${HEIGHT:-720}"
CODEC="${CODEC:-h264}"
# Always passed explicitly. Omitting it falls through to a static preset table
# indexed on resolution that knows nothing about this link -- Run B inherited
# 3.0 Mbps that way, and a review section was later written against a cap that
# had to be guessed rather than read.
MAX_BITRATE="${MAX_BITRATE:-10000000}"
# 1000 ms samples once inside a collapse that completes in 0.5-1.0 s, sometimes
# not at all. 100 ms is the experiment default.
STATS_INTERVAL_MS="${STATS_INTERVAL_MS:-100}"
# Was 60, to keep encoder ramp-up out of the statistics. That window is now the
# interval under study:
# the resolution collapse completes inside the first second, so excluding it
# excluded the evidence. Must match run_subscriber_test.sh exactly -- the two
# CSVs pair by frame ID and mismatched windows produce metrics that disagree.
START_FRAME="${START_FRAME:-0}"
END_FRAME=$(( START_FRAME + FPS * SECONDS_TO_RUN ))

: "${LIVEKIT_API_KEY:?set LIVEKIT_API_KEY}"
: "${LIVEKIT_API_SECRET:?set LIVEKIT_API_SECRET}"

# A bursty 30fps duty cycle never convinces the powersave governor to ramp up, which
# inflated capture->buffer from 0.17ms to 14.89ms with no visible symptom other than
# the latency itself. The governor resets on reboot, so check rather than assume.
#
# EPP is checked separately and is the one that actually bites: cpufrequtils
# persists the governor across a reboot but does NOT manage EPP, which is an
# intel_pstate knob outside its scope and reverts to balance_performance. So the
# governor can read 'performance' on a freshly booted machine while EPP has
# silently gone back to a power-saving setting -- the same invisible failure as
# the original governor bug, one layer down. Neither can be repaired without
# sudo, so warning loudly is the whole remedy.
warn_power_setting() {
  file="$1"; want="$2"; label="$3"; fix="$4"
  [ -r "$file" ] || return 0
  have="$(cat "$file" 2>/dev/null)"
  [ "$have" = "$want" ] && return 0
  echo >&2
  echo "  ####################################################################" >&2
  echo "  # WARNING: $label is '$have', not '$want'." >&2
  echo "  # Publisher stage timings will be inflated and are NOT comparable to" >&2
  echo "  # runs made at '$want'. Record this if you keep the run." >&2
  echo "  #   $fix" >&2
  echo "  ####################################################################" >&2
  echo >&2
}

CPUFREQ=/sys/devices/system/cpu/cpu0/cpufreq
warn_power_setting "$CPUFREQ/scaling_governor" performance \
  "cpu governor" "sudo cpupower frequency-set -g performance"
warn_power_setting "$CPUFREQ/energy_performance_preference" performance \
  "energy performance preference (EPP)" \
  "echo performance | sudo tee $CPUFREQ/energy_performance_preference"

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
#
# SHOW_PREVIEW=0 drops the preview too. Measured cost is small -- 0.24 ms at the
# median -- but it adds tail jitter (capture->buffer p95 19.32 vs 16.49 ms), so it
# is worth dropping when the tail is what is under study.
PREVIEW_ARGS=(--display-video)
if [ "${SHOW_PREVIEW:-1}" = "0" ]; then
  PREVIEW_ARGS=()
  echo "  preview: disabled (SHOW_PREVIEW=0)"
fi

"$BIN" \
  --url "$URL" \
  --room-name "$ROOM" \
  --identity cam-1 \
  --test-pattern 1 \
  --width "$WIDTH" --height "$HEIGHT" --fps "$FPS" \
  --codec "$CODEC" \
  --max-bitrate "$MAX_BITRATE" \
  --stats-interval-ms "$STATS_INTERVAL_MS" \
  --burn-timestamp \
  "${PREVIEW_ARGS[@]}" \
  --log-csv "$OUTDIR/publisher.csv" \
  --log-start-frame-id "$START_FRAME" \
  --log-end-frame-id "$END_FRAME"

echo
echo "done: $OUTDIR/publisher.csv ($(wc -l < "$OUTDIR/publisher.csv") lines)"
echo "copy it to the subscriber machine, then run:"
echo "  ./run_report.sh $OUTDIR"
