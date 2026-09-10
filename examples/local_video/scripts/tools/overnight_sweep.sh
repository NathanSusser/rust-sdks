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
export RUST_LOG=info

OUT=examples/local_video/scripts/results/overnight
LKQ=/tmp/claude-1000/-home-nsusser-code-rust-sdks/ed46c4c6-8c5b-4a78-9a25-69408affbcb1/scratchpad/lkq.py
DURATION=150
SAMPLE_EVERY="${SAMPLE_EVERY:-90}"      # 1 frame per 3 s: 50 frames/cell, ~156 MB
mkdir -p "$OUT"

EPOCHS=(1789025553 1789025763 1789025973 1789026183 1789026393 1789026603 1789026813 1789027023 \
        1789027233 1789027443 1789027653 1789027863 1789028073 1789028283 1789028493 1789028703)

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
pub=[r for r in cand if int(r.get("num_publishers",0))>0]
pick=(pub or cand)
if not pick: sys.exit(1)
print(pick[0]["name"])
' 2>/dev/null
}

log "sweep armed: ${#EPOCHS[@]} cells, ${DURATION}s each, sample every ${SAMPLE_EVERY} frames"

for i in "${!EPOCHS[@]}"; do
  T=${EPOCHS[$i]}; n=$((i+1))
  # Wake 8 s early: enough to discover the room and start, not so early that we join
  # before the publisher and sit in a room it might recreate.
  python3 -c "import time;t=$T-8;d=t-time.time();
import sys
if d>0: time.sleep(d)"
  now=$(date +%s)
  if [ $((now - T)) -gt 60 ]; then log "cell $n: SKIPPED, already $((now-T))s past epoch"; continue; fi

  ROOM=""
  for try in $(seq 1 20); do
    ROOM="$(live_room)"; [ -n "$ROOM" ] && break
    sleep 2
  done
  if [ -z "$ROOM" ]; then log "cell $n: no live room found at epoch $T -- skipping"; continue; fi

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
