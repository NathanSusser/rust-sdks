#!/usr/bin/env bash
#
# Run the local_video publisher against the MSO LiveKit server with the full
# environment set up, so it works from any fresh terminal.
#
# Usage:
#   ./scripts/run-publisher.sh                      # defaults below
#   ./scripts/run-publisher.sh --burn-timestamp --display-video --display-timing
#   ./scripts/run-publisher.sh --log-csv publisher.csv \
#       --log-start-frame-id 301 --log-end-frame-id 1200
#
# Any flags given are passed straight through to the publisher. Override the
# room/identity/codec with ROOM=, IDENTITY=, CODEC= in the environment.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

ENV_FILE="${ENV_FILE:-$REPO/.livekit-demo/.env}"
if [[ ! -f "$ENV_FILE" ]]; then
  echo "missing env file: $ENV_FILE" >&2
  exit 1
fi

# The MSO server presents a T-Mobile-issued cert; rustls native roots do not
# include those CAs, so point SSL_CERT_FILE at the combined bundle.
if [[ -f "$REPO/.livekit-demo/corp-ca.pem" ]]; then
  export SSL_CERT_FILE="$REPO/.livekit-demo/corp-ca.pem"
fi

# shellcheck disable=SC1090
set -a; source "$ENV_FILE"; set +a

: "${LIVEKIT_URL:?LIVEKIT_URL not set in $ENV_FILE}"

export CUDA_HOME="${CUDA_HOME:-/usr}"
export RUST_LOG="${RUST_LOG:-info}"

ROOM="${ROOM:-nathan-video-test-room}"
IDENTITY="${IDENTITY:-cam-1}"
CODEC="${CODEC:-av1}"

BIN="$REPO/target/release/publisher"
if [[ ! -x "$BIN" ]]; then
  echo "publisher not built. Run:" >&2
  echo "  CUDA_HOME=/usr CC=\$HOME/.local/llvm-21.1.8/bin/clang CXX=\$HOME/.local/llvm-21.1.8/bin/clang++ \\" >&2
  echo "  LIBCLANG_PATH=\$HOME/.local/llvm-21.1.8/lib \\" >&2
  echo "  BINDGEN_EXTRA_CLANG_ARGS=-I\$HOME/.local/llvm-21.1.8/lib/clang/21/include \\" >&2
  echo "  cargo build --release -p local_video -F desktop --bin publisher" >&2
  exit 1
fi

# A previous publisher holding the same identity gets kicked by the server for
# duplicate identity, which looks like the new process failing. Warn instead.
if pgrep -f "release/publisher .*--identity $IDENTITY" >/dev/null 2>&1; then
  echo "warning: a publisher with identity '$IDENTITY' is already running:" >&2
  pgrep -af "release/publisher .*--identity $IDENTITY" >&2
  echo "  kill it first, or pass IDENTITY=cam-2 to run alongside it." >&2
fi

echo "room=$ROOM identity=$IDENTITY codec=$CODEC url=$LIVEKIT_URL"

# Not exec'd: a wrapper process is kept alive so it can watch for and recover
# from a stuck shutdown (see below).
"$BIN" \
  --room-name "$ROOM" --identity "$IDENTITY" \
  --test-pattern 1 --codec "$CODEC" \
  --attach-timestamp --attach-frame-id \
  "$@" &
child=$!

# The publisher's own Ctrl-C handler sometimes logs "Ctrl-C received,
# exiting..." and then hangs instead of actually exiting (seen in
# --display-video mode: 20 threads, blocked on a futex, needing a manual kill
# from outside). Give it a grace period to exit on its own, then escalate to
# SIGTERM and finally SIGKILL so Ctrl-C always actually stops the process.
GRACE_SECS="${GRACE_SECS:-8}"
shutting_down=0
force_stop() {
  if ((shutting_down)); then
    return
  fi
  shutting_down=1
  echo >&2
  echo "waiting up to ${GRACE_SECS}s for publisher to exit..." >&2
  local waited=0
  while kill -0 "$child" 2>/dev/null && ((waited < GRACE_SECS)); do
    sleep 1
    waited=$((waited + 1))
  done
  if kill -0 "$child" 2>/dev/null; then
    echo "publisher did not exit on its own; sending SIGTERM..." >&2
    kill -TERM "$child" 2>/dev/null || true
    sleep 1
    if kill -0 "$child" 2>/dev/null; then
      echo "still alive; sending SIGKILL..." >&2
      kill -KILL "$child" 2>/dev/null || true
    fi
  fi
  wait "$child" 2>/dev/null
  exit $?
}
trap force_stop INT TERM

wait "$child"
exit $?
