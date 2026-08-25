#!/usr/bin/env python3
"""Mint a LiveKit access token from LIVEKIT_API_KEY / LIVEKIT_API_SECRET.

Usage:
    ./mint_token.py --room demo --identity pub-1
    ./mint_token.py --room demo --identity sub-1 --ttl 7200

Reads credentials from the environment, or from a .env file via --env-file.
Prints only the JWT on stdout so it can be captured directly:

    TOKEN=$(./mint_token.py --room demo --identity pub-1)
"""

import argparse
import base64
import hashlib
import hmac
import json
import os
import sys
import time


def b64url(raw: bytes) -> str:
    """Base64url-encode without padding, as required by JWT."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def load_env_file(path: str) -> None:
    """Load KEY=VALUE lines from a .env file without overriding real env vars."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            lines = handle.readlines()
    except FileNotFoundError:
        sys.exit(f"env file not found: {path}")

    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def mint(api_key: str, api_secret: str, identity: str, room: str, ttl: int) -> str:
    now = int(time.time())
    claims = {
        "iss": api_key,
        "sub": identity,
        "name": identity,
        "nbf": now,
        "exp": now + ttl,
        "video": {
            "roomJoin": True,
            "room": room,
            "canPublish": True,
            "canSubscribe": True,
            "canPublishData": True,
        },
    }

    # Compact JSON keeps the token small; JWT requires HS256 here because that is
    # what the LiveKit server verifies API-key tokens with.
    segments = [
        b64url(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode()),
        b64url(json.dumps(claims, separators=(",", ":")).encode()),
    ]
    signing_input = ".".join(segments).encode("ascii")
    signature = hmac.new(api_secret.encode(), signing_input, hashlib.sha256).digest()
    return ".".join(segments) + "." + b64url(signature)


def main() -> None:
    parser = argparse.ArgumentParser(description="Mint a LiveKit access token.")
    parser.add_argument("--room", required=True, help="room name to grant access to")
    parser.add_argument("--identity", required=True, help="participant identity")
    parser.add_argument("--ttl", type=int, default=3600, help="lifetime in seconds (default 3600)")
    parser.add_argument("--env-file", help="path to a .env file with LIVEKIT_* values")
    args = parser.parse_args()

    if args.env_file:
        load_env_file(args.env_file)

    api_key = os.environ.get("LIVEKIT_API_KEY")
    api_secret = os.environ.get("LIVEKIT_API_SECRET")
    if not api_key or not api_secret:
        sys.exit("LIVEKIT_API_KEY and LIVEKIT_API_SECRET must be set (or use --env-file)")

    print(mint(api_key, api_secret, args.identity, args.room, args.ttl))


if __name__ == "__main__":
    main()
