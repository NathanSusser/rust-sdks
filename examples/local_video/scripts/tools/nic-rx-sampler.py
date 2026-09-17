#!/usr/bin/env python3
"""Sample a network interface's receive counters at ~200 Hz, unprivileged.

Placing a tens-of-milliseconds media delivery stall between "the network" and "this host"
normally needs a packet capture (root or CAP_NET_RAW). Without either, the kernel's
per-interface counters are the next best view: /sys/class/net/<if>/statistics/rx_packets
advances as the driver hands packets to the stack. Media at ~400 packets/s arrives in
per-frame bursts ~33 ms apart, so a frame that reaches the interface ~50 ms late shows up
as an unusually long run of samples with no rx_packets increase. If the interface kept
receiving through a gap in the WebRTC receive timestamps, the delay was inside this host.

Writes CSV: unix_us,rx_packets,rx_bytes (one row per sample whose counters changed, plus
one heartbeat row per second), host wall clock (PTP-disciplined on this rig).

Usage: nic-rx-sampler.py <outfile.csv> <duration_s> [iface=wwan0] [interval_ms=5]
"""
import os
import sys
import time


def main():
    if len(sys.argv) < 3:
        raise SystemExit(__doc__)
    out, dur = sys.argv[1], float(sys.argv[2])
    iface = sys.argv[3] if len(sys.argv) > 3 else "wwan0"
    interval = (float(sys.argv[4]) if len(sys.argv) > 4 else 5.0) / 1000
    base = f"/sys/class/net/{iface}/statistics/"
    fp = open(base + "rx_packets", "rb", buffering=0)
    fb = open(base + "rx_bytes", "rb", buffering=0)

    def read(f):
        f.seek(0)
        return int(f.read())

    try:
        os.nice(-0)  # no priority change requested; stays unprivileged
    except OSError:
        pass
    end = time.time() + dur
    last = None
    last_beat = 0
    n_rows = 0
    periods = []           # achieved spacing between consecutive samples, microseconds
    prev_t = None
    with open(out, "w") as w:
        w.write("unix_us,rx_packets,rx_bytes\n")
        nxt = time.monotonic()
        while time.time() < end:
            t = time.time()     # CLOCK_REALTIME, same clock as webrtc_receive_timestamp_us
            p, b = read(fp), read(fb)
            if prev_t is not None:
                periods.append(int((t - prev_t) * 1e6))
            prev_t = t
            if (p, b) != last or t - last_beat >= 1.0:
                w.write(f"{int(t * 1e6)},{p},{b}\n")
                n_rows += 1
                last = (p, b)
                if t - last_beat >= 1.0:
                    last_beat = t
            nxt += interval
            delay = nxt - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            else:
                nxt = time.monotonic()
    periods.sort()
    q = lambda f: periods[min(len(periods) - 1, int(len(periods) * f))] / 1000 if periods else float("nan")
    summary = (f"samples {len(periods) + 1}; achieved sample period ms: p50 {q(0.5):.2f} p95 {q(0.95):.2f} "
               f"p99 {q(0.99):.2f} max {q(1.0):.2f}; over 10 ms: {sum(1 for x in periods if x > 10000)}; "
               f"target {interval * 1000:.1f} ms; clock CLOCK_REALTIME")
    with open(out + ".period.txt", "w") as s:
        s.write(summary + "\n")
    print(f"{out}: {n_rows} rows over {dur:.0f} s; {summary}")


if __name__ == "__main__":
    main()
