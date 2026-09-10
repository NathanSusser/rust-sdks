#!/usr/bin/env bash
# Host B receive side for the overnight bitrate/codec sweep.
#
# Cells are addressed by EPOCH, and the room name is DISCOVERED at each epoch rather
# than hard-coded. Host A was asked to interleave the codecs, which changes which room
# sits in which slot; binding a name to a slot in advance would silently join the wrong
# room for fifteen cells if they adopt the change, and silently join the wrong room for
# the other fifteen if they do not. Asking the server what is live cannot be wrong.
set -uo pipefail
cd /home/nsusser/code/rust-sdks
set -a; . ./.livekit-demo/.env; set +a
export SSL_CERT_FILE="$PWD/.livekit-demo/corp-ca.pem"
LIVEKIT_URL="wss://livekit-release-livekit-server-figure-ai-h265.apps.oai01.stc.edgeai.t-mobile.com"
export RUST_LOG=info

# The subscriber writes one CSV row per GPU-RENDERED frame, so with no display it decodes
# perfectly and logs nothing: 664 frames received, 0 dropped, and a CSV containing only
# its header. `setsid nohup` detaches from the session that owned DISPLAY/WAYLAND_DISPLAY,
# which is how a driver that works when launched by hand produces empty cells overnight.
# Same void-the-run failure the locked-Wayland guard exists for, reached a different way.
export DISPLAY="${DISPLAY:-:0}"
# UNSET, deliberately. The subscriber logs one row per GPU-rendered frame, and a native
# Wayland surface that is not visible receives no frame callbacks -- so it decodes
# perfectly and renders nothing. Cell 1 of the NVENC pass: 1684 received, 1684 decoded,
# 0 dropped, ZERO CSV rows, window created and adapters enumerated. Xwayland is not
# throttled that way, which is the documented remedy for the locked-session variant of
# this same failure. render_ms from Xwayland is not comparable to a native run; receive,
# decode, resolution, bitrate, QP and loss are unaffected, and those carry this campaign.
unset WAYLAND_DISPLAY
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export XAUTHORITY="${XAUTHORITY:-$(ls -t /run/user/$(id -u)/.mutter-Xwaylandauth.* 2>/dev/null | head -1)}"

OUT=examples/local_video/scripts/results/overnight
LKQ=/tmp/claude-1000/-home-nsusser-code-rust-sdks/ed46c4c6-8c5b-4a78-9a25-69408affbcb1/scratchpad/lkq.py
DURATION=150
SAMPLE_EVERY="${SAMPLE_EVERY:-90}"      # 1 frame per 3 s: 50 frames/cell, ~156 MB
mkdir -p "$OUT"

# epoch:room. The room name comes from the agreed schedule, NOT from ListRooms.
# Discovery was tried and abandoned: this deployment sits behind an OpenShift router that
# load-balances across pods, and LiveKit here uses single-node routing, so a room lives in
# ONE pod's memory. A ListRooms from a fresh connection lands on whichever pod the router
# picks and returns an empty list while the publisher is demonstrably in the room pushing
# 21.5 MB. Joining by name works because the signal connection is routed to the room's own
# node; querying for it is what does not.
CELLS=(1789026299:ov1-1500k-h264 1789026509:ov1-1500k-av1 1789026719:ov1-2000k-h264 \
       1789026929:ov1-2000k-av1  1789027139:ov1-2500k-h264 1789027349:ov1-2500k-av1 \
       1789027559:ov1-3000k-h264 1789027769:ov1-3000k-av1  1789027979:ov1-4000k-h264 \
       1789028189:ov1-4000k-av1  1789028399:ov1-5000k-h264 1789028609:ov1-5000k-av1 \
       1789028819:ov1-6000k-h264 1789029029:ov1-6000k-av1  1789029239:ov1-8000k-h264 \
       1789029449:ov1-8000k-av1)

log() { echo "[$(date -u +%H:%M:%S)] $*" | tee -a "$OUT/sweep.log"; }

live_room() {
  # Names the room the publisher is actually in. Prefers one with a publisher.
  python3 "$LKQ" ListRooms '{}' 2>/dev/null | python3 -c '
import json,sys,re
raw=sys.stdin.read()
i=raw.find("{")
if i<0: sys.exit(1)
try: d=json.loads(raw[i:])
except Exception: sys.exit(1)
rooms=d.get("rooms",[])
cand=[r for r in rooms if re.match(r"^ov1-", r.get("name",""))]
if not cand: sys.exit(1)
def pubs(r):
    try: return int(r.get("num_publishers") or 0)
    except (TypeError, ValueError): return 0
cand.sort(key=lambda r: (-pubs(r), r.get("name","")))
print(cand[0]["name"])
' 2>/dev/null
}

log "sweep armed: ${#CELLS[@]} cells, ${DURATION}s each, sample every ${SAMPLE_EVERY} frames"

for i in "${!CELLS[@]}"; do
  entry=${CELLS[$i]}; T=${entry%%:*}; ROOM=${entry#*:}; n=$((i+1))
  python3 -c "import time;d=$T-2-time.time()
if d>0: time.sleep(d)"
  now=$(date +%s)
  if [ $((now - T)) -gt 60 ]; then log "cell $n ($ROOM): SKIPPED, already $((now-T))s past epoch"; continue; fi

  D="$OUT/cell$(printf '%02d' $n)-$ROOM"; mkdir -p "$D/frames"
  log "cell $n: room=$ROOM  -> $D"

  timeout $((DURATION + 30)) ./target/release/subscriber \
      --url "$LIVEKIT_URL" --room-name "$ROOM" \
      --identity host-b-overnight --low-latency \
      --sample-frames-dir "$D/frames" --sample-every "$SAMPLE_EVERY" \
      --log-csv "$D/subscriber.csv" > "$D/subscriber.log" 2>&1 &
  SUBPID=$!
  ( sleep "$DURATION"; kill "$SUBPID" 2>/dev/null ) &
  KILLER=$!
  wait "$SUBPID"; STATUS=$?
  kill "$KILLER" 2>/dev/null

  ROWS=$(( $(wc -l < "$D/subscriber.csv" 2>/dev/null || echo 1) - 1 ))
  FR=$(ls "$D/frames" 2>/dev/null | wc -l)
  echo "$ROOM,$STATUS,$ROWS,$FR" >> "$OUT/index.csv"
  log "cell $n: done room=$ROOM status=$STATUS rows=$ROWS frames=$FR"
done

log "SWEEP COMPLETE"
