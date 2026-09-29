#!/usr/bin/env python3
"""Unprivileged TCP-class RTT: time a non-blocking connect() to a CLOSED port.

A refused port answers the SYN with a RST, so SYN->RST is a TCP round trip that never
opens a connection or loads a service (S2 uses 10.1.20.16:3478, which refuses). An open
port (SYN->SYN-ACK) also works but creates real connections; avoid it at 5 Hz.

Usage: tcp-rtt-probe.py <dest_ip> <port> <duration_s> <out.csv> [hz=5] [tos=0x00]
CSV: seq,send_unix_ms,rtt_ms,result   (result: rst | synack | timeout | error)
"""
import errno, os, select, socket, sys, time

dst, port, dur, out = sys.argv[1], int(sys.argv[2]), float(sys.argv[3]), sys.argv[4]
hz = float(sys.argv[5]) if len(sys.argv) > 5 else 5.0
tos = int(sys.argv[6], 0) if len(sys.argv) > 6 else 0
f = open(out, 'w'); f.write('seq,send_unix_ms,rtt_ms,result\n')
pending = {}  # fd -> (seq, t0, sock)
start = time.time(); nxt = start; seq = 0; period = 1.0 / hz
while time.time() - start < dur or pending:
    now = time.time()
    if now >= nxt and now - start < dur:
        s = socket.socket(); s.setblocking(False)
        if tos: s.setsockopt(socket.SOL_IP, socket.IP_TOS, tos)
        t0 = time.time()
        try: s.connect((dst, port))
        except BlockingIOError: pass
        except OSError as e: f.write(f'{seq},{t0*1000:.1f},,error:{e.errno}\n'); s.close(); seq += 1; nxt += period; continue
        pending[s.fileno()] = (seq, t0, s); seq += 1; nxt += period
    wait = max(0.0, min(0.005, nxt - time.time()))
    _, w, _ = select.select([], [p[2] for p in pending.values()], [], wait)
    for s in w:
        sq, t0, _s = pending.pop(s.fileno())
        err = s.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR); t1 = time.time()
        res = 'synack' if err == 0 else 'rst' if err == errno.ECONNREFUSED else f'error:{err}'
        f.write(f'{sq},{t0*1000:.1f},{(t1-t0)*1000:.2f},{res}\n'); s.close()
    for fd, (sq, t0, s) in list(pending.items()):
        if time.time() - t0 > 3.0:
            f.write(f'{sq},{t0*1000:.1f},,timeout\n'); s.close(); pending.pop(fd)
    f.flush()
f.close()
