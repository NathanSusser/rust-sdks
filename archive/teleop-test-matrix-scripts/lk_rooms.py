#!/usr/bin/env python3
"""List LiveKit rooms (and a room's participants) from the server API.

Proves which SFU a paired run used: a LiveKit room lives on exactly one node, so
one room SID listing BOTH hosts' identities means both hosts were on the same SFU.
Signs a short-lived admin JWT locally from LIVEKIT_API_KEY/SECRET; the secret is
never printed or sent anywhere but the server named by --url.

Usage: lk_rooms.py [--url wss://host] [--room NAME]
"""
import argparse, base64, hashlib, hmac, json, os, ssl, sys, time, urllib.request

def b64(b): return base64.urlsafe_b64encode(b).rstrip(b'=').decode()

def token(key, secret, room=None):
    now = int(time.time())
    grant = {'roomList': True, 'roomAdmin': True}
    if room: grant['room'] = room
    claims = {'iss': key, 'sub': 'hop-attribution', 'nbf': now - 10, 'exp': now + 300, 'video': grant}
    h = b64(json.dumps({'alg': 'HS256', 'typ': 'JWT'}).encode()); p = b64(json.dumps(claims).encode())
    s = b64(hmac.new(secret.encode(), f'{h}.{p}'.encode(), hashlib.sha256).digest())
    return f'{h}.{p}.{s}'

def call(base, method, body, tok, ctx):
    req = urllib.request.Request(f'{base}/twirp/livekit.RoomService/{method}', data=json.dumps(body).encode(),
                                 headers={'Authorization': f'Bearer {tok}', 'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=10, context=ctx) as r: return json.load(r)

def main():
    ap = argparse.ArgumentParser(); ap.add_argument('--url', default=os.environ.get('LIVEKIT_URL'))
    ap.add_argument('--room'); a = ap.parse_args()
    key, secret = os.environ.get('LIVEKIT_API_KEY'), os.environ.get('LIVEKIT_API_SECRET')
    if not (a.url and key and secret): sys.exit('need --url (or LIVEKIT_URL) and LIVEKIT_API_KEY/SECRET')
    base = a.url.replace('wss://', 'https://').replace('ws://', 'http://').rstrip('/')
    ca = os.environ.get('SSL_CERT_FILE') or os.path.join(os.path.dirname(__file__), '../../.livekit-demo/corp-ca.pem')
    ctx = ssl.create_default_context(cafile=ca) if os.path.exists(ca) else ssl.create_default_context()
    t = token(key, secret, a.room)
    print(f'server {base}')
    rooms = call(base, 'ListRooms', {'names': [a.room]} if a.room else {}, t, ctx).get('rooms', [])
    for rm in rooms:
        print(f"room {rm.get('name')}  sid {rm.get('sid')}  participants {rm.get('numParticipants', rm.get('num_participants'))}  created {rm.get('creationTime', rm.get('creation_time'))}")
        ps = call(base, 'ListParticipants', {'room': rm.get('name')}, token(key, secret, rm.get('name')), ctx).get('participants', [])
        for p in ps:
            tracks = ','.join(f"{t.get('type','?')}:{t.get('mimeType', t.get('mime_type',''))}" for t in p.get('tracks', []))
            print(f"   identity {p.get('identity')}  sid {p.get('sid')}  state {p.get('state')}  tracks [{tracks}]")
    if not rooms: print('no matching rooms')

if __name__ == '__main__': main()
