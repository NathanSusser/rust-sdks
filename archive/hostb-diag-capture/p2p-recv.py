#!/usr/bin/env python3
"""Receive the p2p-send replay and record per-packet one-way delay.

  p2p-recv.py <port> <seconds> <out.csv>

Writes one row per packet: seq, recv_epoch, owd_ms. Loss and reordering are
derived from the sequence afterwards. One-way delay is meaningful only because
both hosts are PTP-disciplined; servo state must be s2.
"""
import socket, struct, sys, time

port, dur, out = int(sys.argv[1]), float(sys.argv[2]), sys.argv[3]
HDR = struct.Struct("!Qd")
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 22)
s.bind(("0.0.0.0", port))
s.settimeout(5)

n = 0
end = time.time() + dur
with open(out, "w") as f:
    f.write(f"# started_epoch={time.time():.3f} dur={dur}\n")
    f.write("seq,recv_epoch,owd_ms\n")
    while time.time() < end:
        try:
            p, _ = s.recvfrom(2048)
        except socket.timeout:
            continue
        seq, t = HDR.unpack_from(p)
        now = time.time()
        f.write(f"{seq},{now:.6f},{(now-t)*1000:.3f}\n")
        n += 1
        if n % 50000 == 0:
            f.flush()
print(f"received {n} packets -> {out}")
