#!/usr/bin/env bash
# Run the basic_data_track publisher or subscriber against LiveKit Cloud.
#
# Usage:
#   ./run-data.sh publisher
#   ./run-data.sh subscriber
#
# Reads LIVEKIT_URL / LIVEKIT_API_KEY / LIVEKIT_API_SECRET from ../.env,
# mints a per-participant token, and launches the example.
set -euo pipefail

ROLE="${1:-}"
if [[ "$ROLE" != "publisher" && "$ROLE" != "subscriber" ]]; then
  echo "usage: $0 <publisher|subscriber>" >&2
  exit 2
fi

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(dirname "$HERE")"
ENV_FILE="${ENV_FILE:-$REPO/.env}"
ROOM="${ROOM:-rust-demo}"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "missing env file: $ENV_FILE" >&2
  exit 1
fi

# The corporate TLS proxy re-signs outbound HTTPS, so rustls needs the
# T-Mobile roots that plain rustls-native-roots does not pick up on macOS.
if [[ -f "$HERE/corp-ca.pem" ]]; then
  export SSL_CERT_FILE="$HERE/corp-ca.pem"
fi

# shellcheck disable=SC1090
set -a; source "$ENV_FILE"; set +a

LIVEKIT_TOKEN="$("$HERE/mint_token.py" --room "$ROOM" --identity "$ROLE" --ttl 7200)"
export LIVEKIT_TOKEN
export LIVEKIT_URL
export RUST_LOG="${RUST_LOG:-info}"

echo "room=$ROOM identity=$ROLE url=$LIVEKIT_URL"
exec cargo run -q -p basic_data_track --bin "$ROLE"
