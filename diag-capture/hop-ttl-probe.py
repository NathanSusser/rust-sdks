#!/usr/bin/env python3
"""Unprivileged per-hop RTT: TTL-limited UDP toward a destination, ICMP time-exceeded
read back from the socket error queue (IP_RECVERR), the way tracepath does it.

Carrier routers on this path (hop 2 10.169.180.252, hop 3 10.198.3.237) do not answer
ICMP echo, so `ping` to them measures nothing. They do return time-exceeded. Routers
commonly rate-limit that, so the series can be sparse: coverage is reported, and a
missing reply is recorded as missing, never as zero RTT.

DSCP: pass tos=0xb8 (EF) as the 6th argument to mark the probes, so a DSCP-aware
classifier upstream can be told apart from a protocol-aware one.

Usage: hop-ttl-probe.py <dest_ip> <duration_s> <out.csv> [hz=5] [ttls=2,3] [tos=0x00]
CSV: ttl,seq,send_unix_ms,recv_unix_ms,rtt_ms,hop_ip,icmp_type,icmp_code
"""
import socket, struct, sys, time, select

IP_RECVERR = 11
dst, dur, out = sys.argv[1], float(sys.argv[2]), sys.argv[3]
hz = float(sys.argv[4]) if len(sys.argv) > 4 else 5.0
ttls = [int(x) for x in (sys.argv[5] if len(sys.argv) > 5 else '2,3').split(',')]
tos = int(sys.argv[6], 0) if len(sys.argv) > 6 else 0

socks = {}
for ttl in ttls:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_IP, socket.IP_TTL, ttl)
    s.setsockopt(socket.SOL_IP, IP_RECVERR, 1)
    if tos: s.setsockopt(socket.SOL_IP, socket.IP_TOS, tos)
    s.setblocking(False)
    socks[s.fileno()] = (ttl, s)

sent = {}      # (ttl, seq) -> send time
rows = {}
f = open(out, 'w'); f.write('ttl,seq,send_unix_ms,recv_unix_ms,rtt_ms,hop_ip,icmp_type,icmp_code\n')

def drain(s, ttl):
    while True:
        try:
            data, anc, _flags, _addr = s.recvmsg(64, 512, socket.MSG_ERRQUEUE)
        except (BlockingIOError, InterruptedError):
            return
        now = time.time()
        if len(data) < 4: continue
        seq = struct.unpack('!I', data[:4])[0]
        hop, itype, icode = '', '', ''
        for level, ctype, cdata in anc:
            if level == socket.SOL_IP and ctype == IP_RECVERR and len(cdata) >= 24:
                _errno, origin, itype, icode, _pad, _info, _data = struct.unpack('=IBBBBII', cdata[:16])
                hop = socket.inet_ntoa(cdata[20:24])
        t0 = sent.pop((ttl, seq), None)
        if t0 is None: continue
        rows[(ttl, seq)] = (t0, now, hop, itype, icode)

start = time.time(); seq = 0; period = 1.0 / hz; nxt = start
while time.time() - start < dur:
    now = time.time()
    if now >= nxt:
        for fd, (ttl, s) in socks.items():
            try:
                s.sendto(struct.pack('!I', seq) + b'hop-ttl-probe', (dst, 33434 + (seq % 64)))
                sent[(ttl, seq)] = time.time()
            except OSError:
                pass
        seq += 1; nxt += period
    r, _, _ = select.select([s for _, s in socks.values()], [], [], max(0.0, min(0.005, nxt - time.time())))
    for s in r:
        drain(s, socks[s.fileno()][0])
    # flush replies older than 3 s and declare unanswered sends missing
    cutoff = time.time() - 3.0
    for key in [k for k, t in sent.items() if t < cutoff]:
        t0 = sent.pop(key); f.write(f'{key[0]},{key[1]},{t0*1000:.1f},,,,,\n')
    for key in list(rows):
        t0, t1, hop, it, ic = rows.pop(key)
        f.write(f'{key[0]},{key[1]},{t0*1000:.1f},{t1*1000:.1f},{(t1-t0)*1000:.2f},{hop},{it},{ic}\n')
    f.flush()
time.sleep(2.0)
for s in (s for _, s in socks.values()): drain(s, socks[s.fileno()][0])
for key, (t0, t1, hop, it, ic) in rows.items():
    f.write(f'{key[0]},{key[1]},{t0*1000:.1f},{t1*1000:.1f},{(t1-t0)*1000:.2f},{hop},{it},{ic}\n')
for key, t0 in sent.items(): f.write(f'{key[0]},{key[1]},{t0*1000:.1f},,,,,\n')
f.close()
