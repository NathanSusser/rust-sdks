#!/usr/bin/env python3
"""Send the UDP loss ladder: media-sized packets at rising rates, no SFU in the path.

Pairs with udp-ladder-recv.py. See that file for why this exists and for the one caveat that
must accompany any conclusion drawn from it (UE-to-UE hairpins through the carrier core; the
SFU path does not, so a clean result here does not prove the link healthy for the SFU path).

DESIGN NOTES, each one bought with a wrong answer earlier today:

  SEND FROM THE REAL SOURCE ADDRESS, NO GAMES. Both hosts run rp_filter=2 (loose). Loose is
  fine while the source is an address the receiver can route back to, but a spoofed or oddly
  bound source makes the kernel drop the packets at the far end and it looks EXACTLY like
  link loss. Bind wwan0's own address and nothing else.

  PACE AGAINST A MONOTONIC SCHEDULE, not sleep-per-packet. Accumulated sleep error would make
  the achieved rate drift below the target and understate the load, so the measurement would
  flatter the link.

  REPORT WHAT WAS EMITTED, per step. The receiver can only count gaps between packets that
  arrived; it cannot know about packets sent after the last one that got through. This side
  is the authority on the denominator.

Usage: udp-ladder-send.py <dst_ip> [port=55999] [secs_per_step=20] [payload=1200]
Ladder is 500k, 1M, 2M, 5M bits/s by default -- edit RATES to change it.
"""
import socket
import struct
import sys
import time

DST = sys.argv[1] if len(sys.argv) > 1 else sys.exit(__doc__)
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 55999
STEP_S = float(sys.argv[3]) if len(sys.argv) > 3 else 20.0
SIZE = int(sys.argv[4]) if len(sys.argv) > 4 else 1200
RATES = [500_000, 1_000_000, 2_000_000, 5_000_000]
HDR = struct.Struct("!HIQ")          # step, seq, send_unix_us

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4 << 20)
# Bind the modem's own address explicitly: no source-address games, so a loose rp_filter at
# the far end has no reason to discard anything.
src = None
try:
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    probe.connect((DST, PORT))
    src = probe.getsockname()[0]
    probe.close()
    sock.bind((src, 0))
except OSError as e:
    print(f"could not bind a source address ({e}); sending unbound", file=sys.stderr)

print(f"sending to {DST}:{PORT} from {src or 'default'}, {SIZE}B payload, {STEP_S:.0f}s per step")
print(f"{'step':>4}  {'rate':>10}  {'sent':>7}  {'achieved':>10}  {'elapsed':>8}")
total = 0
for step, rate in enumerate(RATES):
    pps = rate / (SIZE * 8)
    interval = 1.0 / pps
    pad = b"\x00" * max(0, SIZE - HDR.size)
    t0 = time.monotonic()
    nxt = t0
    sent = 0
    seq = 0
    while time.monotonic() - t0 < STEP_S:
        pkt = HDR.pack(step, seq, int(time.time() * 1e6)) + pad
        try:
            sock.sendto(pkt, (DST, PORT))
            sent += 1
            seq += 1
        except OSError as e:
            print(f"  send error at step {step} seq {seq}: {e}", file=sys.stderr)
            break
        nxt += interval
        d = nxt - time.monotonic()
        if d > 0:
            time.sleep(d)
        else:
            nxt = time.monotonic()      # fell behind; resynchronise rather than accumulate
    el = time.monotonic() - t0
    total += sent
    print(f"{step:>4}  {rate/1e6:>8.2f}M  {sent:>7}  {sent*SIZE*8/el/1e6:>8.2f}M  {el:>7.1f}s")

# A short quiet tail so the receiver's idle timeout fires cleanly rather than mid-step.
print(f"\n{total} packets emitted, {total*SIZE/1e6:.1f} MB total. Receiver reports the loss.")
