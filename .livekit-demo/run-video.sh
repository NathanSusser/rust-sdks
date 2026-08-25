#!/usr/bin/env bash
# Run the local_video publisher or subscriber against LiveKit Cloud.
#
# Usage:
#   ./run-video.sh list_devices
#   ./run-video.sh publisher [extra flags...]
#   ./run-video.sh subscriber [extra flags...]
#
# local_video mints its own tokens from the API key/secret, so this script only
# needs to load .env and forward flags. Extra flags are passed straight through,
# e.g. ./run-video.sh publisher --test-pattern --codec h265
set -euo pipefail

ROLE="${1:-}"
case "$ROLE" in
  publisher|subscriber|list_devices|clock) shift ;;
  *) echo "usage: $0 <publisher|subscriber|list_devices|clock> [flags...]" >&2; exit 2 ;;
esac

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(dirname "$HERE")"
ENV_FILE="${ENV_FILE:-$REPO/.env}"
ROOM="${ROOM:-rust-demo}"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "missing env file: $ENV_FILE" >&2
  exit 1
fi

# See note in run-data.sh: the corporate TLS proxy needs the T-Mobile roots.
if [[ -f "$HERE/corp-ca.pem" ]]; then
  export SSL_CERT_FILE="$HERE/corp-ca.pem"
fi

# shellcheck disable=SC1090
set -a; source "$ENV_FILE"; set +a

export LIVEKIT_URL LIVEKIT_API_KEY LIVEKIT_API_SECRET
export RUST_LOG="${RUST_LOG:-info}"

# list_devices and clock are standalone and take no room/identity.
if [[ "$ROLE" == "list_devices" || "$ROLE" == "clock" ]]; then
  exec cargo run -q --release -p local_video -F desktop --bin "$ROLE" -- "$@"
fi

echo "room=$ROOM identity=video-$ROLE url=$LIVEKIT_URL"
exec cargo run -q --release -p local_video -F desktop --bin "$ROLE" -- \
  --room-name "$ROOM" --identity "video-$ROLE" "$@"
