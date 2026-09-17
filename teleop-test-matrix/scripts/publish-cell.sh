#!/usr/bin/env bash
# Publish one cell at an absolute epoch, with the environment loaded and a
# liveness assertion on the other side of startup.
#
# WHY THIS EXISTS. Three cells across two nights died silently at startup and
# were reported as running. The most recent died 4 ms after at-epoch fired --
# `LIVEKIT_API_KEY must be set` -- because the invocation was hand-assembled
# from run.sh without run.sh's line 5, which loads the credentials. The shell
# returned a pid, the pid was reported as "armed", and the partner host sat in
# an empty room for ninety seconds while the operator watched a blank window.
#
# The campaign guard that was supposed to catch this lived in run.sh, so the
# first hand-launched cell walked straight past it. A guard attached to the
# driver does not protect a cell started any other way; this one is attached to
# the act of starting a cell.
#
# `nohup cmd &` ALWAYS succeeds. A pid is not evidence of a running cell. The
# only evidence is output: this script waits for the first stats snapshot and
# fails loudly if it does not arrive.
#
# Usage: publish-cell.sh <epoch> <room> <cap_kbps> <codec> <outdir> [extra harness args...]
# DURATION=<seconds> overrides the default 150 s run.
# RUST_LOG=info also logs the room SID and join details (default warn).
set -uo pipefail

[ $# -ge 5 ] || { echo "usage: $0 <epoch> <room> <cap_kbps> <codec> <outdir> [extra...]" >&2; exit 2; }
epoch=$1 room=$2 cap=$3 codec=$4 D=$5; shift 5

REPO=/home/nsusser/code/rust-sdks
URL="wss://livekit-release-livekit-server-figure-ai-h265.apps.oai01.stc.edgeai.t-mobile.com"
# Source clip. Overridable per run: results depend on content as much as on bitrate --
# the 2026-09-04 depal face-lower capture has 12.6x the frame-to-frame motion of the
# default clip and wants ~2.8x the bitrate for the same quality, so every run logs which
# clip it used (first line of the log, below).
CLIP="${CLIP:-/home/nsusser/teleop-media/robot-src-30m.mp4}"
# Degradation axis. `locked` holds resolution AND frame rate, so a forced bitrate is
# delivered as forced pixels at frame rate rather than being paid in either. The
# UNFORCED control cell must NOT be locked: locking it would leave it constrained on a
# different axis from every other cell and it would stop being a control. Logged below
# from this variable, never as a literal -- a cell that mis-states its own pin or
# degradation state poisons every comparison drawn from it.
DEGRADATION="${DEGRADATION:-locked}"
LIVENESS_S=25

cd "$REPO" || exit 1
mkdir -p "$D"

# The line whose absence killed the last cell. Fail here, before the epoch,
# rather than 4 ms after it.
set -a && . .livekit-demo/.env && set +a
: "${LIVEKIT_API_KEY:?not set after sourcing .livekit-demo/.env}"
: "${LIVEKIT_API_SECRET:?not set after sourcing .livekit-demo/.env}"

log="$D/$room.log"
snap="$D/$room.jsonl"
rm -f "$snap"

# GCC REMOVED, ALL THREE OVERRIDES (RUN-DISCIPLINE section 12, operator directive).
# Until 2026-09-14 this script applied only --degradation locked. The pin and the
# start ceiling were set by hand for the archived 08-gcc-out-pinned runs and never
# made it into any launch script, so every cell launched since -- the overnight
# ladder, the motion run, the 10 Sep CBR/VBR pair, the 15 Sep modem-capture run --
# ran with the delay-based estimator free to lower the allocation. The 35 kbps
# collapses were that estimator reacting to real path stalls.
#
# With the pin engaged a cap above what the link carries drowns the stream instead
# of adapting, so the retransmission gate (section 10) is mandatory alongside it.
# Override for a deliberate GCC-on comparison with LK_PIN_BITRATE_TO_MAX=0.
export LK_PIN_BITRATE_TO_MAX="${LK_PIN_BITRATE_TO_MAX:-1}"
export LK_MAX_START_BITRATE_KBPS="${LK_MAX_START_BITRATE_KBPS:-$cap}"
{
  echo "gcc-overrides: LK_PIN_BITRATE_TO_MAX=$LK_PIN_BITRATE_TO_MAX LK_MAX_START_BITRATE_KBPS=$LK_MAX_START_BITRATE_KBPS degradation=$DEGRADATION"
  echo "source: clip=$CLIP cap=${cap}k codec=$codec duration=${DURATION:-150}s"
} > "$log"

teleop-test-matrix/scripts/at-epoch.sh "$epoch" \
  env RUST_LOG="${RUST_LOG:-warn}" ./target/release/teleop-harness \
    --url "$URL" --room-name "$room" \
    --duration-s "${DURATION:-150}" --warmup-s 5 --codec "$codec" --encoder nvenc \
    --width 1600 --height 1300 --fps 30 --max-bitrate $((cap * 1000)) \
    --degradation "$DEGRADATION" \
    --camera-source "$CLIP" \
    --attach-timestamp --attach-frame-id --buffering-mode zero_jitter \
    --control-transport dc_reliable \
    --publish-only --stats-poll-hz 1 --video-poll-hz 1 \
    --snapshots-out "$snap" --frame-csv-out "$D/$room" \
    "$@" >> "$log" 2>&1 &
pid=$!
# A Ctrl-C must not leave a cell scheduled. Bash makes background children ignore
# SIGINT, so an interrupt reaching this script would otherwise exit here and leave
# at-epoch waiting to fire the publisher on its own (seen 2026-09-14). Forward TERM,
# which background children do not ignore. Only on interrupt: a successful launch
# returns with the harness running, by design.
trap 'kill -TERM "$pid" 2>/dev/null; exit 130' INT TERM

# Liveness. Not "did the shell return a pid" but "did the process produce
# output". Poll until the first snapshot lands, the process dies, or we give up.
deadline=$(( $(date -u +%s) + (epoch - $(date -u +%s)) + LIVENESS_S ))
while :; do
  [ -s "$snap" ] && { echo "LIVE $room pid=$pid first-snapshot $(date -u +%H:%M:%S)"; exit 0; }
  if ! kill -0 "$pid" 2>/dev/null; then
    echo "DEAD $room pid=$pid exited before producing any snapshot" >&2
    echo "---- $log ----" >&2; tail -20 "$log" >&2
    exit 1
  fi
  [ "$(date -u +%s)" -ge "$deadline" ] && {
    echo "STALL $room pid=$pid alive but no snapshot within ${LIVENESS_S}s of epoch" >&2
    tail -20 "$log" >&2; exit 1; }
  sleep 0.5
done
