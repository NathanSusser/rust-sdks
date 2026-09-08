#!/usr/bin/env bash
# Serve the E1 fixture: ref.mp4 looped over RTSP at 127.0.0.1:8554/fixture.
#
# The sweep cannot use the live camera. The harness's own --camera-source
# documentation says a camera is never a swept axis, because scene content,
# lighting and framing make bitrate depend on things unrelated to the axis being
# swept -- and cells that differ for unrelated reasons cannot be compared
# afterwards by any amount of care in analysis.
#
# ffmpeg cannot serve this itself: its rtsp muxer's `listen` flag is decode-only,
# so ffmpeg can receive a push but not serve a client. Hence mediamtx, with
# ffmpeg pushing into it.
#
# Verified properties of the served stream (see FIXTURE-VERIFICATION.md):
#   - the loop is bit-identical in decoded content, period exactly 901 frames
#   - all 901 frames are distinct, so a frame-ID misalignment is DETECTABLE
#     rather than silently producing plausible-looking PSNR
set -euo pipefail

SCRATCH="${SCRATCH:-/tmp/claude-1000/-home-nsusser-code/323bfd6c-d311-471f-8de9-bc1272bf213b/scratchpad}"
REF="${REF:-$SCRATCH/ref.mp4}"
MTX="${MTX:-$SCRATCH/mediamtx}"
PORT="${PORT:-8554}"

[ -f "$REF" ] || { echo "fixture clip not found: $REF" >&2; exit 1; }
[ -x "$MTX" ] || { echo "mediamtx not found: $MTX" >&2; exit 1; }

cat > "$SCRATCH/mediamtx.yml" <<YML
logLevel: info
rtspAddress: 127.0.0.1:$PORT
rtmp: no
hls: no
webrtc: no
srt: no
api: no
paths:
  fixture:
    source: publisher
YML

# Never pkill -f here: it matches this script's own invocation. Kill by pidfile.
for f in "$SCRATCH/mediamtx.pid" "$SCRATCH/fixture-push.pid"; do
  [ -f "$f" ] && { kill "$(cat "$f")" 2>/dev/null || true; rm -f "$f"; }
done
sleep 1

nohup "$MTX" "$SCRATCH/mediamtx.yml" > "$SCRATCH/mediamtx.log" 2>&1 &
echo $! > "$SCRATCH/mediamtx.pid"
sleep 2

# -c copy re-sends the same encoded frames every pass, so the source is
# bit-identical across cells. -re paces it at wall-clock rate.
nohup ffmpeg -hide_banner -loglevel warning -re -stream_loop -1 -i "$REF" \
    -c copy -f rtsp -rtsp_transport tcp "rtsp://127.0.0.1:$PORT/fixture" \
    > "$SCRATCH/fixture-push.log" 2>&1 &
echo $! > "$SCRATCH/fixture-push.pid"
sleep 4

if ffprobe -v error -rtsp_transport tcp -select_streams v:0 \
     -show_entries stream=width,height,avg_frame_rate -of default=nw=1 \
     "rtsp://127.0.0.1:$PORT/fixture"; then
  echo "fixture up at rtsp://127.0.0.1:$PORT/fixture"
else
  echo "fixture failed to come up; see $SCRATCH/mediamtx.log and fixture-push.log" >&2
  exit 1
fi
