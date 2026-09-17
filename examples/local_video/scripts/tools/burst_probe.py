#!/usr/bin/env python3
"""Does BURST STRUCTURE, not average rate, cause loss on this path?

The rig's video loses 20-42% at 1.6 Mbps while evenly-paced probes at 1.96 Mbps lose
nothing. The one structural difference left is shape: a video encoder emits a frame as a
clump of packets back-to-back 30 times a second, which is nothing like one packet every
4 ms. A shallow buffer anywhere on the path empties fine at the average rate and overflows
on the clump.

Two arms at an IDENTICAL average rate and packet size, differing only in shape:

    paced   one packet every interval
    bursty  N packets back-to-back, then idle until the next frame slot

ARMS ARE INTERLEAVED, NOT RUN IN SEQUENCE. This link has moved 7.3x in minutes, so
running all of arm A then all of arm B measures the link's drift as though it were the
effect. Host A's cap sweep died of exactly that and had to be withdrawn.

WITHIN-BURST POSITION IS THE REAL DISCRIMINATOR, and it is why this beats plain ping.
If loss rises with position inside the burst -- packet 8 lost more than packet 1 -- that
is a queue filling and overflowing. If loss is flat across positions, the path is
dropping for some other reason (ICMP policing being the obvious confound) and burst
shape is exonerated. A single loss percentage cannot tell those apart.

Unprivileged: SOCK_DGRAM/IPPROTO_ICMP, the same socket ping(8) uses. No root, no raw
sockets. The kernel rewrites the ICMP id; the sequence number survives, which is all we
need to match replies to sends.
"""
import argparse, os, select, socket, struct, sys, time

ICMP_ECHO = 8


def checksum(data: bytes) -> int:
    if len(data) % 2:
        data += b"\x00"
    total = 0
    for i in range(0, len(data), 2):
        total += (data[i] << 8) + data[i + 1]
    total = (total >> 16) + (total & 0xFFFF)
    total += total >> 16
    return ~total & 0xFFFF


def packet(seq: int, size: int) -> bytes:
    # id is rewritten by the kernel on a DGRAM icmp socket, so it carries no information
    # for us; seq is what we match on. Payload is padded to the media packet size because
    # a 64-byte probe does not exercise the same buffering as a 1200-byte one.
    header = struct.pack("!BBHHH", ICMP_ECHO, 0, 0, 0, seq & 0xFFFF)
    payload = struct.pack("!d", time.time()) + b"\x00" * max(0, size - 8)
    chk = checksum(header + payload)
    return struct.pack("!BBHHH", ICMP_ECHO, 0, chk, 0, seq & 0xFFFF) + payload


def run_arm(sock, dest, bursty, burst, slot_s, slots, size, seq0):
    """Send one arm and collect replies. Returns (sent_seqs, rtt_by_seq)."""
    sent, rtts = {}, {}
    seq = seq0
    gap = slot_s / burst if not bursty else 0.0
    deadline = time.perf_counter()
    for _ in range(slots):
        deadline += slot_s
        for k in range(burst):
            now = time.perf_counter()
            sock.sendto(packet(seq, size), (dest, 0))
            sent[seq & 0xFFFF] = (now, k)          # k = position within the burst
            seq += 1
            if not bursty and k < burst - 1:
                target = now + gap
                while True:                          # drain replies while we wait
                    remaining = target - time.perf_counter()
                    if remaining <= 0:
                        break
                    if select.select([sock], [], [], remaining)[0]:
                        drain(sock, sent, rtts)
        while True:
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                break
            if select.select([sock], [], [], remaining)[0]:
                drain(sock, sent, rtts)
    # Late replies still count as delivered: this measures loss, not latency budget.
    end = time.perf_counter() + 1.0
    while time.perf_counter() < end:
        if select.select([sock], [], [], 0.05)[0]:
            drain(sock, sent, rtts)
    return sent, rtts


def drain(sock, sent, rtts):
    try:
        data, _ = sock.recvfrom(2048)
    except OSError:
        return
    if len(data) < 8:
        return
    kind, _, _, _, seq = struct.unpack("!BBHHH", data[:8])
    if kind == 0 and seq in sent and seq not in rtts:
        rtts[seq] = (time.perf_counter() - sent[seq][0]) * 1000.0


def summarise(name, sent, rtts):
    n, got = len(sent), len(rtts)
    lost = n - got
    by_pos = {}
    for seq, (_, pos) in sent.items():
        s, l = by_pos.get(pos, (0, 0))
        by_pos[pos] = (s + 1, l + (0 if seq in rtts else 1))
    vals = sorted(rtts.values())
    p50 = vals[len(vals) // 2] if vals else float("nan")
    p95 = vals[int(len(vals) * 0.95)] if vals else float("nan")
    print(f"  {name:8s} sent {n:5d}  lost {lost:5d}  ({100.0*lost/max(n,1):5.1f}%)   "
          f"rtt p50 {p50:6.1f}  p95 {p95:7.1f} ms")
    return by_pos, 100.0 * lost / max(n, 1)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dest")
    ap.add_argument("--size", type=int, default=1200, help="payload bytes (media-sized)")
    ap.add_argument("--burst", type=int, default=8, help="packets per frame slot")
    ap.add_argument("--fps", type=float, default=30.0, help="frame slots per second")
    ap.add_argument("--seconds", type=float, default=10.0, help="seconds per arm")
    ap.add_argument("--rounds", type=int, default=3, help="interleaved A/B rounds")
    args = ap.parse_args()

    slot_s = 1.0 / args.fps
    slots = int(args.seconds * args.fps)
    pps = args.burst * args.fps
    mbps = pps * (args.size + 28) * 8 / 1e6
    print(f"target {args.dest}   {args.size}B payload   {args.burst} pkt/slot @ {args.fps:g} fps"
          f"   = {pps:.0f} pps, {mbps:.2f} Mbps in BOTH arms")
    print(f"{args.rounds} interleaved rounds of {args.seconds:g}s each\n")

    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_ICMP)
    except PermissionError:
        print("need net.ipv4.ping_group_range to include this gid (same permission ping(8) uses)",
              file=sys.stderr)
        return 1
    sock.setblocking(False)

    totals = {"paced": [{}, {}], "bursty": [{}, {}]}
    rates = {"paced": [], "bursty": []}
    seq = 0
    for r in range(args.rounds):
        print(f"round {r+1}")
        for name, bursty in (("paced", False), ("bursty", True)):
            sent, rtts = run_arm(sock, args.dest, bursty, args.burst, slot_s, slots,
                                 args.size, seq)
            seq += len(sent) + 16
            by_pos, pct = summarise(name, sent, rtts)
            rates[name].append(pct)
            for pos, (s, l) in by_pos.items():
                ps, pl = totals[name][0].get(pos, 0), totals[name][1].get(pos, 0)
                totals[name][0][pos], totals[name][1][pos] = ps + s, pl + l

    print("\n=== LOSS BY POSITION WITHIN THE BURST (the discriminator) ===")
    print("  a queue overflowing loses LATE positions; flat loss means something else")
    for name in ("paced", "bursty"):
        s_by, l_by = totals[name]
        row = "  ".join(f"{p}:{100.0*l_by.get(p,0)/max(s_by.get(p,1),1):.1f}%"
                        for p in sorted(s_by))
        print(f"  {name:8s} {row}")

    print("\n=== SUMMARY ===")
    for name in ("paced", "bursty"):
        vals = rates[name]
        print(f"  {name:8s} per-round loss {['%.1f%%' % v for v in vals]}  mean {sum(vals)/len(vals):.1f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
