#!/usr/bin/env python3
"""Measure this host's clock offset from true UTC and print host_minus_utc_s.

WHY THIS MUST RUN PER CELL, NEVER BE REMEMBERED. Both hosts are PTP-synced to EACH OTHER but
not to UTC, and the offset moves fast and unpredictably: it went from -12.5 s to -14.75 s in a
single hour on 2026-09-17, and from -14.733 to -14.919 in about ninety minutes on 2026-09-18.
A modem reduction anchored with a REMEMBERED offset came out 2,458 ms misaligned against the
video timeline; the record counts were all fine, only the alignment was wrong, which is the
kind of error that survives review.

Median of three STABLE public servers, so one bad reply cannot move the answer.
Chosen on repeatability, not round trip: RTT does not predict stability (apple is
the slowest server here and the steadiest), so selecting the fastest reply sorts
on the wrong quantity. Prints the value in
the sign convention dlf-rates.py wants: host_minus_utc_s, i.e. host_clock = utc + this.
"""
import socket
import statistics
import struct
import sys
import time

# pool.ntp.org REMOVED 2026-09-22: it resolves to a different server per lookup,
# so every call samples a fresh path. Measured from Host B over 30 runs (stdev
# after common-mode detrend): pool 16.89 ms against cloudflare 4.72, apple 3.66.
# It was the single largest term in the offset's run-to-run scatter on BOTH hosts.
#
# time.apple.com is the steadiest server on either host and also the least
# reliable: it failed to answer 4/30 here and 3/30 on Host A, so ~10-13% and a
# property of the server rather than of one path. With three servers a miss
# degrades the median to a mean of two, which is why the count is three.
#
# MUST MATCH HOST A. Both hosts convert host-clock to UTC with this, and a split
# reintroduces an inter-host discrepancy in the absolute labels. Change together.
SERVERS = ("time.google.com", "time.cloudflare.com", "time.apple.com")

def offset(host):
    """UTC minus this host's clock, in seconds, by the standard NTP four-timestamp formula."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(3)
        t0 = time.time()
        s.sendto(b"\x1b" + 47 * b"\0", (host, 123))
        data, _ = s.recvfrom(48)
        t3 = time.time()
        s.close()
        t1 = struct.unpack("!I", data[32:36])[0] + struct.unpack("!I", data[36:40])[0] / 2**32 - 2208988800
        t2 = struct.unpack("!I", data[40:44])[0] + struct.unpack("!I", data[44:48])[0] / 2**32 - 2208988800
        return ((t1 - t0) + (t2 - t3)) / 2
    except Exception:
        return None

vals = [v for v in (offset(h) for h in SERVERS) if v is not None]
if not vals:
    print("no NTP server answered; refusing to guess an offset", file=sys.stderr)
    sys.exit(1)
if len(vals) < 2:
    print(f"only {len(vals)} server answered; median is not meaningful", file=sys.stderr)
spread = (max(vals) - min(vals)) * 1000 if len(vals) > 1 else 0.0
print(f"answered={len(vals)}/{len(SERVERS)} server_spread={spread:.1f}ms", file=sys.stderr)
if spread > 25:
    print(f"WARNING: servers disagree by {spread:.1f} ms; this offset is noisy", file=sys.stderr)
# host_minus_utc = -(utc_minus_host)
print(f"{-statistics.median(vals):.3f}")
