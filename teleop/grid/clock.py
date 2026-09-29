"""This host's clock offset from true UTC (host_minus_utc_s), by SNTP. Standard library only.

Port of archive/diag-capture/clock-offset.py. The rules it encodes:

* MEASURE PER CELL, NEVER REMEMBER. Both hosts are PTP-locked to each other, not to UTC,
  and the offset moves: -12.5 s to -14.75 s in one hour (2026-09-17); a reduction anchored
  with a remembered offset came out 2,458 ms misaligned while every count looked right.
* MEDIAN OF THREE STABLE SERVERS. pool.ntp.org resolves to a different server each lookup
  and was the worst source on both hosts; it is deliberately absent. RTT does not predict
  stability (time.apple.com is slowest and steadiest), so there is no least-RTT selection.
* REPORT THE SPREAD, so a noisy measurement is visible; warn above 25 ms.

Sign convention: host_clock = utc + host_minus_utc_s.
"""
from __future__ import annotations

import socket
import statistics
import struct
import threading
import time

SERVERS = ("time.google.com", "time.cloudflare.com", "time.apple.com")
NTP_EPOCH_DELTA = 2208988800
WARN_SPREAD_MS = 25.0


def query(host: str, timeout: float = 3.0):
    """(utc_minus_host_s, rtt_s) by the four-timestamp formula, or None."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(timeout)
            addr = socket.getaddrinfo(host, 123, socket.AF_INET, socket.SOCK_DGRAM)[0][4]
            t0 = time.time()
            s.sendto(b"\x1b" + 47 * b"\0", addr)
            data, _ = s.recvfrom(48)
            t3 = time.time()
    except (OSError, IndexError):
        return None
    if len(data) < 48:
        return None
    li_vn_mode, stratum = data[0], data[1]
    if stratum == 0 or (li_vn_mode >> 6) == 3:   # kiss-of-death or unsynchronised server
        return None

    def ts(off):
        return struct.unpack("!I", data[off:off + 4])[0] + struct.unpack("!I", data[off + 4:off + 8])[0] / 2**32 \
            - NTP_EPOCH_DELTA
    t1, t2 = ts(32), ts(40)
    return ((t1 - t0) + (t2 - t3)) / 2, (t3 - t0) - (t2 - t1)


def measure(servers=SERVERS, timeout: float = 3.0) -> dict:
    """clock.json content. Servers are queried in parallel, so this returns in ~timeout."""
    results: dict[str, tuple | None] = {}

    def run(h):
        results[h] = query(h, timeout)
    threads = [threading.Thread(target=run, args=(h,), daemon=True) for h in servers]
    for t in threads:
        t.start()
    deadline = time.monotonic() + timeout + 2
    for t in threads:
        t.join(max(0.0, deadline - time.monotonic()))
    answered = {h: r for h, r in results.items() if r is not None}
    out = {
        "host_minus_utc_s": None,
        "measured_at": time.time(),
        "servers": len(answered),
        "spread_ms": None,
        "asked": list(servers),
        "answered": sorted(answered),
        "rtts_ms": {h: round(r[1] * 1000, 1) for h, r in answered.items()},
        "warnings": [],
    }
    if not answered:
        out["warnings"].append("no NTP server answered; refusing to guess an offset")
        return out
    offs = [r[0] for r in answered.values()]
    out["host_minus_utc_s"] = round(-statistics.median(offs), 4)
    out["spread_ms"] = round((max(offs) - min(offs)) * 1000, 1) if len(offs) > 1 else 0.0
    if len(offs) < 2:
        out["warnings"].append("only 1 server answered; no cross-check on this measurement")
    elif out["spread_ms"] > WARN_SPREAD_MS:
        out["warnings"].append(f"servers disagree by {out['spread_ms']:.1f} ms; absolute UTC labels are soft")
    return out


if __name__ == "__main__":
    import json
    print(json.dumps(measure()))
