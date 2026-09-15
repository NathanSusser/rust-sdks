#!/usr/bin/env python3
"""Place short media delivery stalls on Host B: before its network interface, or inside the host.

Host A's signature for the overnight render skips (2026-09-15): frame N-1 arrives with
exposure_to_receive ~75-90 ms against a ~27 ms baseline and frame N+1 follows within ~25 ms,
so three frames land inside one vsync. This joins Host B's per-frame subscriber.csv with the
unprivileged 200 Hz interface counters from nic-rx-sampler.py (nicrx.csv) on the shared clock.

For each stalled frame it measures, in the window [receive - LOOKBACK, receive - GUARD]:
  * how many rx_packets the interface counted, and the longest run with no increase.
Compared with ordinary frames' same windows:
  * interface quiet for the stall (long no-increase run, few packets) -> packets reached the
    host late: the delay is upstream (network, SFU, or Host B's modem downlink/USB batching);
  * interface busy as usual while WebRTC receive is late -> packets were on the host on time:
    the delay is inside Host B's receive path.

Usage: stall_locator.py <cycle_outdir> [--lookback-ms 80] [--guard-ms 3] [--late-factor 2.5]
"""
import argparse
import bisect
import csv
import os
import statistics


def load_frames(path):
    rows = []
    for r in csv.DictReader(open(path)):
        try:
            rows.append((int(r["webrtc_receive_timestamp_us"]), int(r["frame_id"]), float(r["exposure_to_receive_ms"])))
        except (ValueError, KeyError, TypeError):
            continue
    rows.sort()
    return rows


def load_nic(path):
    t, p = [], []
    for r in csv.DictReader(open(path)):
        t.append(int(r["unix_us"]))
        p.append(int(r["rx_packets"]))
    return t, p


def window_stats(t, p, lo_us, hi_us):
    """Packets counted and longest no-increase run (ms) inside [lo, hi]."""
    i = max(0, bisect.bisect_left(t, lo_us) - 1)
    j = bisect.bisect_right(t, hi_us)
    if j - i < 2:
        return None, None
    pk = p[min(j, len(p)) - 1] - p[i]
    longest, run_start = 0.0, max(t[i], lo_us)
    for k in range(i + 1, min(j, len(t))):
        if p[k] > p[k - 1]:
            longest = max(longest, (t[k] - run_start) / 1000)
            run_start = t[k]
    longest = max(longest, (hi_us - run_start) / 1000)
    return pk, longest


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("outdir")
    ap.add_argument("--lookback-ms", type=float, default=80)
    ap.add_argument("--guard-ms", type=float, default=3)
    ap.add_argument("--late-factor", type=float, default=2.5)
    args = ap.parse_args()
    frames = load_frames(os.path.join(args.outdir, "subscriber.csv"))
    nic_path = os.path.join(args.outdir, "nicrx.csv")
    if not os.path.exists(nic_path):
        raise SystemExit(f"no nicrx.csv in {args.outdir}")
    t, p = load_nic(nic_path)
    if not frames or not t:
        raise SystemExit("no frames or no NIC samples")
    base = statistics.median(f[2] for f in frames)
    lb, gd = args.lookback_ms * 1000, args.guard_ms * 1000
    stalled, ordinary = [], []
    for k in range(1, len(frames) - 1):
        recv, fid, e2r = frames[k]
        if not (t[0] + lb <= recv <= t[-1]):
            continue
        nxt_gap = (frames[k + 1][0] - recv) / 1000
        pk, run = window_stats(t, p, recv - lb, recv - gd)
        if pk is None:
            continue
        if e2r >= args.late_factor * base and nxt_gap <= 25:
            stalled.append((recv, fid, e2r, nxt_gap, pk, run))
        elif e2r <= 1.3 * base:
            ordinary.append((pk, run))
    print(f"frames {len(frames)}, transport baseline p50 {base:.1f} ms, NIC samples {len(t)}")
    if ordinary:
        opk = sorted(o[0] for o in ordinary)
        orun = sorted(o[1] for o in ordinary)
        print(f"ordinary frames n={len(ordinary)}: packets in window p50 {opk[len(opk)//2]} (p10 {opk[len(opk)//10]}); "
              f"longest quiet run p50 {orun[len(orun)//2]:.0f} ms, p90 {orun[int(len(orun)*.9)]:.0f} ms")
    print(f"stalled frames (e2r >= {args.late_factor}x baseline, next frame <= 25 ms later): {len(stalled)}")
    for recv, fid, e2r, ng, pk, run in stalled:
        verdict = "interface quiet -> upstream" if (ordinary and run >= 1.5 * sorted(o[1] for o in ordinary)[int(len(ordinary)*.9)]) else "interface active -> inside host?"
        print(f"  frame {fid}: e2r {e2r:.0f} ms, next +{ng:.0f} ms, packets in window {pk}, longest quiet {run:.0f} ms  => {verdict}")


if __name__ == "__main__":
    main()
