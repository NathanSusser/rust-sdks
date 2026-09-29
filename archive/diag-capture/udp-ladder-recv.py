#!/usr/bin/env python3
"""Receive the UDP loss ladder and report per-step loss, with NOTHING else in the path.

WHY THIS EXISTS. Every failing measurement on 2026-09-17 ran through the SFU: three sweep
cells and one live run, all of them A -> SFU -> B, all with WebRTC, an encoder and a bitrate
pin involved. So "the loss is between the two hosts" is proven -- packets counted on A's wire,
absent from B's, matched by RTP sequence number with a 0.0% cross-cell control -- but WHICH
SEGMENT is not, because the SFU sits inside that span.

This removes everything except the 5G path. A and B are on the same carrier /28 and route to
each other directly over wwan0, so a plain UDP stream at media packet size and media rate
tests the link on its own.

  ~85% loss  -> the 5G path between the UEs is lossy; today's conclusion holds
  ~0% loss   -> the link carries this fine and the fault is the SFU or our WebRTC stack

ONE ASYMMETRY TO STATE WHEN REPORTING, raised by Host B and correct: UE-to-UE on the same /28
HAIRPINS THROUGH THE CARRIER CORE, while the SFU path goes out to the public internet and
back. Different route, different queues, possibly different QoS. So a clean result here
narrows hard toward the SFU or our stack but does NOT prove the link healthy for the SFU
path. A lossy result does prove the UE-to-UE 5G path is lossy.

Each packet carries step index, sequence number and send timestamp, so loss is counted
exactly per step and one-way delay VARIATION is measurable without the two clocks agreeing --
the spread of (recv - send) is meaningful even though its level carries the offset. That
distinguishes "dropped" from "delayed and reordered", which ICMP cannot.

Usage: udp-ladder-recv.py [port=55999] [idle_timeout_s=15] [out.csv]
Runs until idle_timeout_s passes with no packets, then prints the per-step report.
"""
import collections
import csv
import socket
import struct
import sys
import time

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 55999
IDLE = float(sys.argv[2]) if len(sys.argv) > 2 else 15.0
OUT = sys.argv[3] if len(sys.argv) > 3 else "/tmp/udp-ladder-recv.csv"
HDR = struct.Struct("!HIQ")          # step, seq, send_unix_us

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 << 20)
sock.bind(("0.0.0.0", PORT))
sock.settimeout(1.0)
print(f"listening on 0.0.0.0:{PORT}, idle timeout {IDLE:.0f}s -> {OUT}", flush=True)

rows = []
last = None
started = None
while True:
    try:
        data, addr = sock.recvfrom(2048)
    except socket.timeout:
        if last is not None and time.time() - last > IDLE:
            break
        continue
    now = time.time()
    if len(data) < HDR.size:
        continue
    step, seq, send_us = HDR.unpack_from(data, 0)
    if started is None:
        started = now
        print(f"first packet from {addr[0]} at {time.strftime('%H:%M:%SZ', time.gmtime(now))}", flush=True)
    last = now
    rows.append((step, seq, send_us, int(now * 1e6), len(data)))

if not rows:
    sys.exit("no packets received at all -- check routing, rp_filter, and that the sender ran")

with open(OUT, "w", newline="") as fh:
    w = csv.writer(fh)
    w.writerow(["step", "seq", "send_unix_us", "recv_unix_us", "bytes"])
    w.writerows(rows)

by_step = collections.defaultdict(list)
for step, seq, send_us, recv_us, n in rows:
    by_step[step].append((seq, send_us, recv_us, n))

print(f"\n{len(rows)} packets, {OUT}")
print(f"{'step':>4}  {'got':>7}  {'seq range':>15}  {'expected':>8}  {'loss%':>7}  "
      f"{'owd spread ms':>13}  {'reordered':>9}")
for step in sorted(by_step):
    pk = sorted(by_step[step])
    seqs = [s for s, _, _, _ in pk]
    lo, hi = min(seqs), max(seqs)
    expected = hi - lo + 1            # what the sender emitted between the first and last ARRIVAL
    loss = 100.0 * (1 - len(seqs) / expected) if expected else float("nan")
    owd = sorted((r - s) / 1000.0 for _, s, r, _ in pk)
    spread = owd[int(len(owd) * 0.99)] - owd[int(len(owd) * 0.01)] if len(owd) > 10 else float("nan")
    reord = sum(1 for a, b in zip(seqs, seqs[1:]) if b < a)
    print(f"{step:>4}  {len(seqs):>7}  {lo:>7}-{hi:<7}  {expected:>8}  {loss:>7.1f}  "
          f"{spread:>13.1f}  {reord:>9}")
print("\nloss% is per step, over the sequence range that ACTUALLY ARRIVED -- so it counts gaps,")
print("not packets the sender emitted after the last one that got through. The sender's own")
print("count is the authority on what was emitted; compare the two.")
print("owd spread is p99-p01 of (recv - send): the LEVEL carries the clock offset and is")
print("meaningless, the SPREAD is not.")
