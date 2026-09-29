#!/usr/bin/env python3
"""Replay the measured cell15m-a media profile over a DIRECT 5G path, no SFU.

  p2p-send.py <dst_ip> <port> <seconds> [iface]

Profile taken from the real stream (cell15m-a, 489,331 packets): 5.25 Mbps as
~19 packets of 1208 B per frame at 29.0 fps, i.e. bursts rather than a smooth
rate, because that is what a video encoder actually emits and burst structure is
what stresses a radio scheduler.

Each packet carries seq + send timestamp. The hosts are PTP-disciplined, so the
receiver can compute ONE-WAY delay directly -- the same quantity we measured at
35.1 ms through the SFU.
"""
import socket, struct, sys, time

dst, port, dur = sys.argv[1], int(sys.argv[2]), float(sys.argv[3])
iface = (sys.argv[4] if len(sys.argv) > 4 else "wwan0").encode()

FPS, PER_FRAME, SIZE = 29.0, 19, 1208
HDR = struct.Struct("!Qd")                      # seq, send time
PAD = b"\0" * (SIZE - 28 - HDR.size)            # 28 = IP+UDP

s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.setsockopt(socket.SOL_SOCKET, 25, iface)      # SO_BINDTODEVICE -- force 5G, not the PTP cable
s.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1 << 20)

seq = 0
start = time.time()
frame = 0
while True:
    now = time.time()
    if now - start >= dur:
        break
    for _ in range(PER_FRAME):
        s.sendto(HDR.pack(seq, time.time()) + PAD, (dst, port))
        seq += 1
    frame += 1
    nxt = start + frame / FPS
    d = nxt - time.time()
    if d > 0:
        time.sleep(d)
print(f"sent {seq} packets in {time.time()-start:.1f}s "
      f"({seq*SIZE*8/(time.time()-start)/1e6:.2f} Mbps)")
