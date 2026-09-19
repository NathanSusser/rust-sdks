#!/usr/bin/env python3
"""Measure this host's clock offset from true UTC and print host_minus_utc_s.

WHY THIS MUST RUN PER CELL, NEVER BE REMEMBERED. Both hosts are PTP-synced to EACH OTHER but
not to UTC, and the offset moves fast and unpredictably: it went from -12.5 s to -14.75 s in a
single hour on 2026-09-17, and from -14.733 to -14.919 in about ninety minutes on 2026-09-18.
A modem reduction anchored with a REMEMBERED offset came out 2,458 ms misaligned against the
video timeline; the record counts were all fine, only the alignment was wrong, which is the
kind of error that survives review.

Median of three public servers, so one bad reply cannot move the answer. Prints the value in
the sign convention dlf-rates.py wants: host_minus_utc_s, i.e. host_clock = utc + this.
"""
import socket
import statistics
import struct
import sys
import time

SERVERS = ("time.google.com", "pool.ntp.org", "time.cloudflare.com")

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
# host_minus_utc = -(utc_minus_host)
print(f"{-statistics.median(vals):.3f}")
