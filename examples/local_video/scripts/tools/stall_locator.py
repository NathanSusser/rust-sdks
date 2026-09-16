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


def window_stats(t, p, lo_us, hi_us, quiet_share=0.15):
    """Packets counted and longest quiet run (ms) inside [lo, hi].

    "Quiet" is not exact counter equality. On 2026-09-15 an 80 ms downlink stop carried
    one stray packet in the middle, and requiring a single no-increase run split it into
    40 + 40 ms and lost the verdict (Host A's tool made the same mistake independently).
    A stretch counts as quiet while it carries less than `quiet_share` of the packets the
    link would have delivered over it, with the rate taken from the log itself so the
    tolerance scales with bitrate -- ~1,350 pkt/s at 10 Mbps, ~350 at 2 Mbps.
    """
    i = max(0, bisect.bisect_left(t, lo_us) - 1)
    j = bisect.bisect_right(t, hi_us)
    if j - i < 2:
        return None, None
    pk = p[min(j, len(p)) - 1] - p[i]
    span_s = (t[-1] - t[0]) / 1e6
    rate_pps = (p[-1] - p[0]) / span_s if span_s > 0 else 0.0
    longest, run_start, run_start_p = 0.0, max(t[i], lo_us), p[i]
    for k in range(i + 1, min(j, len(t))):
        elapsed_s = (t[k] - run_start) / 1e6
        expected = rate_pps * elapsed_s
        # One packet is always tolerated, so a single stray never ends a stop.
        if p[k] - run_start_p > max(1.0, quiet_share * expected):
            longest = max(longest, (t[k] - run_start) / 1000)
            run_start, run_start_p = t[k], p[k]
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

    # Decisive test (2026-09-15, cycle 49): look where each frame SHOULD have reached the host
    # if on time (capture + baseline transport), not just before its late WebRTC receive.
    # A lookback window can catch neighbouring frames' packets; the expected-arrival window
    # cannot. NOTE: receive_and_assembly_ms is WebRTC receive -> decoder upload, NOT first-to-last
    # packet, so it cannot rule out a late last packet (NACK/RTX). The ordering test below does:
    # under a retransmission the next frame completes BEFORE the late one; under a read hold it
    # arrives just after, in the same batch.
    by_id = {}
    for r in csv.DictReader(open(os.path.join(args.outdir, "subscriber.csv"))):
        try:
            by_id[int(r["frame_id"])] = (int(r["capture_timestamp_us"]), float(r["receive_and_assembly_ms"] or "nan"))
        except (ValueError, KeyError, TypeError):
            continue
    exp_off = int(base * 1000)

    def at_expected(fid):
        cap = by_id.get(fid, (None,))[0]
        if cap is None:
            return None
        i = bisect.bisect_right(t, cap + exp_off - 10_000) - 1
        j = bisect.bisect_right(t, cap + exp_off + 15_000) - 1
        return p[j] - p[i] if i >= 0 and j >= 0 else None

    ord_exp = sorted(v for v in (at_expected(f[1]) for f in frames[1:-1] if f[2] <= 1.3 * base) if v is not None)
    if not ord_exp:
        return
    p10 = ord_exp[len(ord_exp) // 10]
    host_like, upstream_like = [], []
    asm = []
    for recv, fid, e2r, ng, pk, run in stalled:
        v = at_expected(fid)
        a = by_id.get(fid, (None, float("nan")))[1]
        if a == a:
            asm.append(a)
        if v is None:
            continue
        (upstream_like if v <= 2 else host_like if v >= p10 else []).append((fid, v))
    s_exp = sorted(v for _, v in host_like + upstream_like)
    print(f"\nEXPECTED-ARRIVAL TEST (capture + {base:.1f} ms, window -10..+15 ms):")
    print(f"  ordinary frames: packets p10/p50/p90 {p10}/{ord_exp[len(ord_exp)//2]}/{ord_exp[int(len(ord_exp)*.9)]}")
    # Ordering test: receive time of frame id+1 minus the stalled frame's. A NACK/RTX-recovered
    # packet delays only the stalled frame, so id+1 completes first (negative). A read hold
    # releases both in one batch, so id+1 lands just after (small positive).
    recv_by_id = {f[1]: f[0] for f in frames}
    deltas = sorted((recv_by_id[fid + 1] - recv) / 1000 for recv, fid, *_ in stalled if fid + 1 in recv_by_id)
    if deltas:
        neg = sum(1 for d in deltas if d < 0)
        print(f"  ordering test, receive(id+1) - receive(id) ms: n={len(deltas)} negative={neg} "
              f"p10/p50/p90 {deltas[len(deltas)//10]:.1f}/{deltas[len(deltas)//2]:.1f}/{deltas[int(len(deltas)*.9)]:.1f}  "
              f"-> {'read hold (not NACK/RTX)' if neg <= 0.05 * len(deltas) else 'retransmission possible'}")
    print(f"  stalled frames with packets on time at the interface (>= ordinary p10): {len(host_like)} of {len(stalled)}  -> inside Host B (after NAPI, before WebRTC receive)")
    print(f"  stalled frames with <= 2 packets at the expected instant: {len(upstream_like)} -> upstream of NAPI: {[f for f, _ in upstream_like]}")
    # This test asks whether ANY packets were at the interface when the frame was due. At
    # 2 Mbps a frame is ~12 packets and neighbours barely overlap the window, so that is a
    # fair question. At 10 Mbps it is ~45 packets, neighbouring frames fill the window, and
    # the test answers "inside Host B" for frames the downlink demonstrably stopped on
    # (2026-09-15 10 Mbps cell: this line said 5 of 10 inside Host B; the quiet-run verdicts
    # above, and Host A's independent pass, made it 9 of 11 upstream). So say when it is
    # confounded rather than letting the summary outrank the per-frame verdicts.
    ordinary_packets = sorted(pk for pk, _ in ordinary) if ordinary else []
    if ordinary_packets and ordinary_packets[len(ordinary_packets) // 2] >= 40:
        print(f"  WARNING: ordinary windows carry {ordinary_packets[len(ordinary_packets) // 2]} packets (>= 40), so the"
              f" expected-instant test above is confounded by neighbouring frames' packets."
              f" Trust the per-frame quiet-run verdicts instead.")


if __name__ == "__main__":
    main()
