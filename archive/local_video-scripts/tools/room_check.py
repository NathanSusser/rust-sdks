#!/usr/bin/env python3
"""Ask the SFU which participants share a room -- proof both hosts are on one SFU.

A LiveKit room lives on exactly one server node. If Host A's publisher and Host B's
subscriber are listed under the SAME room SID, their media meets in the same SFU
instance. Matching URLs are not enough: one hostname can front several nodes, and a
second deployment can hold a different room with the same name (the 2026-09-11 DIAG
test received nothing for exactly that reason).

Uses RoomService over Twirp with an HS256 token signed locally from LIVEKIT_API_KEY /
LIVEKIT_API_SECRET (read from the environment or .livekit-demo/.env; never printed).
Standard library only. TLS trusts SSL_CERT_FILE, defaulting to .livekit-demo/corp-ca.pem.

Usage:
  room_check.py                 list rooms: name, SID, participant count, creation time
  room_check.py <room>          that room's SID and each participant: identity, SID,
                                state, joined time, published tracks
"""
import base64
import hashlib
import hmac
import json
import os
import ssl
import sys
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]


def load_env():
    env = dict(os.environ)
    path = REPO / ".livekit-demo" / ".env"
    if path.exists():
        for line in path.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, _, v = line.partition("=")
                env.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    for k in ("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET"):
        if not env.get(k):
            raise SystemExit(f"{k} not set (environment or .livekit-demo/.env)")
    return env


def b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=")


def token(key, secret, room=None):
    now = int(time.time())
    grant = {"roomList": True, "roomAdmin": True}
    if room:
        grant["room"] = room
    claims = {"iss": key, "sub": "room-check", "nbf": now - 10, "exp": now + 300, "video": grant}
    head = b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    body = b64(json.dumps(claims).encode())
    sig = b64(hmac.new(secret.encode(), head + b"." + body, hashlib.sha256).digest())
    return (head + b"." + body + b"." + sig).decode()


def call(env, method, payload, room=None):
    base = env["LIVEKIT_URL"].replace("wss://", "https://").replace("ws://", "http://").rstrip("/")
    req = urllib.request.Request(
        f"{base}/twirp/livekit.RoomService/{method}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + token(env["LIVEKIT_API_KEY"], env["LIVEKIT_API_SECRET"], room)})
    cafile = env.get("SSL_CERT_FILE") or str(REPO / ".livekit-demo" / "corp-ca.pem")
    ctx = ssl.create_default_context(cafile=cafile if Path(cafile).exists() else None)
    with urllib.request.urlopen(req, context=ctx, timeout=10) as r:
        return json.loads(r.read() or b"{}")


def stamp(v):
    try:
        return time.strftime("%H:%M:%SZ", time.gmtime(int(v)))
    except (TypeError, ValueError):
        return "?"


def main():
    env = load_env()
    print(f"server: {env['LIVEKIT_URL']}")
    if len(sys.argv) < 2:
        rooms = call(env, "ListRooms", {}).get("rooms", [])
        print(f"{len(rooms)} room(s)")
        for r in rooms:
            print(f"  {r.get('name')}  sid={r.get('sid')}  participants={r.get('num_participants', r.get('numParticipants'))}"
                  f"  created={stamp(r.get('creation_time', r.get('creationTime')))}")
        return
    name = sys.argv[1]
    rooms = [r for r in call(env, "ListRooms", {"names": [name]}).get("rooms", []) if r.get("name") == name]
    if not rooms:
        raise SystemExit(f"room {name!r} does not exist on this server right now")
    print(f"room {name}  sid={rooms[0].get('sid')}")
    parts = call(env, "ListParticipants", {"room": name}, room=name).get("participants", [])
    for p in parts:
        tracks = ", ".join(f"{t.get('type', '?')}:{t.get('sid')}" for t in p.get("tracks", [])) or "none"
        print(f"  {p.get('identity')}  sid={p.get('sid')}  state={p.get('state', '?')}"
              f"  joined={stamp(p.get('joined_at', p.get('joinedAt')))}  tracks={tracks}")
    print(f"{len(parts)} participant(s) in ONE room sid -> same SFU node" if len(parts) >= 2
          else "fewer than 2 participants: cannot yet show both hosts share this room")


if __name__ == "__main__":
    main()
