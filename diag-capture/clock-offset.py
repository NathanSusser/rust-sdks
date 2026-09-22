#!/usr/bin/env python3
"""Measure this host's clock offset from true UTC and print host_minus_utc_s.

WHY THIS MUST RUN PER CELL, NEVER BE REMEMBERED. Both hosts are PTP-synced to EACH OTHER but
not to UTC, and the offset moves fast and unpredictably: it went from -12.5 s to -14.75 s in a
single hour on 2026-09-17, and from -14.733 to -14.919 in about ninety minutes on 2026-09-18.
A modem reduction anchored with a REMEMBERED offset came out 2,458 ms misaligned against the
video timeline; the record counts were all fine, only the alignment was wrong, which is the
kind of error that survives review.

ESTIMATOR: median of three STABLE servers. The fix that mattered was not the estimator, it
was dropping pool.ntp.org, which resolves to a different server on every lookup and so
contributes a fresh path each time.

Measured on Host A, 30 runs, scored on the same replies and DETRENDED (the host drifts
~32 us/s, so a 60 s session adds real movement that every candidate shares):

    median of 3 with pool     stdev 3.4 ms
    median of 2 without pool  stdev 2.6 ms
    median of 3 stable        stdev 1.6 ms   <- google + cloudflare + apple
    least-RTT of 3            stdev 1.5 ms   <- ties, but see below

Least-RTT ties on A and gains only ~10% on Host B. The difference is a property of the
PATH, not of the estimator: on A one server won the round-trip race 28 times out of 30, so
least-RTT was really "always ask the same server", and picking consistently is what removed
the between-server bias that drags a median. On B no server dominates that race, so the
benefit does not survive. Worse, on B pool.ntp.org WON the race 3 times in 12, meaning
least-RTT actively selected the one server we are trying to avoid.

RTT does not predict stability, which is the reason not to select on it. On A:

    time.apple.com       median RTT  90 ms   stdev 2.0 ms   <- slowest but steadiest
    time.cloudflare.com  median RTT  24 ms   stdev 4.0 ms
    time.nist.gov        median RTT 102 ms   stdev 9.1 ms   <- excluded, twice the spread
    pool.ntp.org         median RTT  79 ms   stdev 4.4 ms   <- excluded, 23 ms range

Sample sizes here are small and these rankings move between runs; compare with stdev, never
with range, which can only grow as you add samples and so cannot be compared across
different n.

Prints the value in the sign convention dlf-rates.py wants: host_minus_utc_s, i.e.
host_clock = utc + this. Also prints the server spread to stderr, so a noisy measurement is
visible rather than silent.
"""
import socket
import struct
import sys
import time

SERVERS = ("time.google.com", "time.cloudflare.com", "time.apple.com")

def offset(host):
    """(utc_minus_host_s, round_trip_s) by the standard NTP four-timestamp formula."""
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
        return ((t1 - t0) + (t2 - t3)) / 2, (t3 - t0)
    except Exception:
        return None

vals = [r for r in (offset(h) for h in SERVERS) if r is not None]
if not vals:
    print("no NTP server answered; refusing to guess an offset", file=sys.stderr)
    sys.exit(1)

# Median across the stable set. With three servers this rejects one outlier outright;
# with two it degrades to their mean, which is why the spread is reported either way.
import statistics
offs = [v for v, _ in vals]
best = statistics.median(offs)
spread = (max(offs) - min(offs)) * 1000 if len(vals) > 1 else 0.0
print(f"answered={len(vals)}/{len(SERVERS)} server_spread={spread:.1f}ms "
      f"rtts_ms={','.join(f'{r*1000:.0f}' for _, r in vals)}", file=sys.stderr)
if len(vals) < 2:
    print("only 1 server answered; no cross-check on this measurement", file=sys.stderr)
elif spread > 25.0:
    print(f"WARNING: servers disagree by {spread:.1f} ms; this is a noisy measurement and the "
          f"absolute UTC labels derived from it are correspondingly soft", file=sys.stderr)
# host_minus_utc = -(utc_minus_host)
print(f"{-best:.3f}")
