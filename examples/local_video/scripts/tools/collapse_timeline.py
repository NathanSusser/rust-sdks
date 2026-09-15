#!/usr/bin/env python3
"""Line up a publisher's bitrate cut with what the subscriber saw, second by second.

Joins Host A's stats .jsonl (target bitrate, bytes sent, frames encoded/sent,
retransmits, NACKs) with Host B's per-frame subscriber CSV (transport latency,
frame-id gaps, packets lost, received rate) on the shared PTP clock. Both files
stamp host wall time, so no offset is applied.

What each pattern means for "where did the rate go":

  target drops, A sends what it is told, B latency climbs first, no loss
      -> a queue built on the path and the delay-based estimator backed off:
         capacity fell somewhere between A's socket and B's receiver
  target drops with retransmits/NACKs and B loss
      -> packets were lost on the path; the loss-based estimator backed off
  target holds but A's sent rate falls
      -> the encoder or sender under-spent its grant (content, pacing)
  A sends at target, B receives less, B frame-id gaps
      -> frames dropped between A and B (SFU never assembled them, or SFU->B loss)

Latency alone cannot say WHICH hop queued (A uplink, core, SFU, SFU->B downlink).
That split needs per-hop evidence: A-side modem buffer/grants, SFU logs, B-side.

Usage:
  collapse_timeline.py --publisher ov1-6000k-h264.jsonl --subscriber subscriber.csv --from 40 --to 70
"""
import argparse
import collections
import csv
import json
import math
import statistics


def load_publisher(path):
    polls = [json.loads(l) for l in open(path) if l.strip()]
    polls = [p for p in polls if p.get("video_out")]
    t0 = polls[0]["t_unix_us"]
    rows = {}
    for p0, p1 in zip(polls, polls[1:]):
        dt = (p1["t_unix_us"] - p0["t_unix_us"]) / 1e6
        if dt <= 0:
            continue
        v0, v1 = p0["video_out"], p1["video_out"]
        rows[round((p1["t_unix_us"] - t0) / 1e6)] = {
            "target_k": v1["target_bitrate_bps"] / 1e3,
            "sent_k": (v1["bytes_sent"] - v0["bytes_sent"]) * 8 / 1e3 / dt,
            "enc_fps": (v1["frames_encoded"] - v0["frames_encoded"]) / dt,
            "retx": v1["retransmitted_packets_sent"] - v0["retransmitted_packets_sent"],
            "nack": v1["nack_count"] - v0["nack_count"],
        }
    return t0, rows


def load_subscriber(path, t0):
    per_s = collections.defaultdict(list)
    lost = {}
    for r in csv.DictReader(open(path)):
        try:
            s = math.floor((int(r["webrtc_receive_timestamp_us"]) - t0) / 1e6)
            lat = float(r["exposure_to_receive_ms"])
            gap = int(r["frame_id_gap"] or 0)
            rx = float(r["receive_bitrate_mbps"] or 0)
            pl = int(r["packets_lost"] or 0)
        except (ValueError, KeyError, TypeError):
            continue
        per_s[s].append((lat, gap, rx))
        lost[s] = pl
    return per_s, lost


def find_episodes(pub, per_s, lost):
    """Runs of seconds where B's median transport latency leaves its baseline.

    Each episode is tagged with whether A's target was cut within 3 s of its start.
    A cut means A's estimator saw the delay, and its feedback comes from the SFU, so
    the queue was on A's leg (A -> SFU). No cut means it was after the SFU (SFU -> B).
    """
    p50 = {s: statistics.median(x[0] for x in v) for s, v in per_s.items() if len(v) >= 5}
    if len(p50) < 30:
        return None, []
    base = statistics.median(p50.values())
    limit = max(3 * base, base + 80)
    bad = sorted(s for s, v in p50.items() if v > limit)
    runs = []
    for s in bad:
        if runs and s - runs[-1][-1] <= 2:
            runs[-1].append(s)
        else:
            runs.append([s])
    out = []
    for run in runs:
        s0, s1 = run[0], run[-1]
        before = [pub[k]["target_k"] for k in range(s0 - 4, s0 - 1) if k in pub]
        after = [pub[k]["target_k"] for k in range(s0 - 1, s0 + 4) if k in pub]
        cut = min(after) / max(before) if before and after and max(before) > 0 else None
        retx = sum(pub[k]["retx"] for k in range(s0 - 2, s1 + 3) if k in pub)
        lost_delta = max(0, lost.get(s1 + 2, lost.get(s1, 0)) - lost.get(s0 - 2, lost.get(s0, 0)))
        missing = sum(max(0, x[1] - 1) for k in range(s0, s1 + 3) for x in per_s.get(k, []))
        out.append({"start": s0, "dur": s1 - s0 + 1, "peak_ms": max(p50[k] for k in run),
                    "target_ratio": cut, "retx": retx, "lost": lost_delta, "missing_ids": missing,
                    "leg": "A->SFU" if cut is not None and cut < 0.8 else "after SFU"})
    return base, out


def load_probes(path):
    """Uplink speed-test rows (unix_s = probe END, mbps_parallel, mbps_single) from uplink-monitor.sh."""
    return [(int(r["unix_s"]), float(r["mbps_parallel"]), float(r["mbps_single"])) for r in csv.DictReader(open(path))]


def scan(hosta_dir, results_dir, probes_path=None):
    import datetime
    import glob
    import os
    # A bulk upload on the publisher's own uplink is the first thing to rule out: on
    # 2026-09-10, 20 of 21 episodes began 7-15 s before an uplink-monitor probe ended.
    probes = load_probes(probes_path) if probes_path else []
    cells = []
    for d in sorted(glob.glob(os.path.join(results_dir, "cell*-*"))):
        name = os.path.basename(d).split("-", 1)[1]
        jsonl = os.path.join(hosta_dir, name + ".jsonl")
        csvp = os.path.join(d, "subscriber.csv")
        if os.path.exists(jsonl) and os.path.exists(csvp):
            t0, pub = load_publisher(jsonl)
            cells.append((t0, name, pub, csvp))
    print("UTC start  cell                       base_ms  episodes: t+start/dur s/peak ms/target ratio/leg/retx/lost/missing ids")
    for t0, name, pub, csvp in sorted(cells):
        per_s, lost = load_subscriber(csvp, t0)
        base, eps = find_episodes(pub, per_s, lost)
        when = datetime.datetime.fromtimestamp(t0 / 1e6, datetime.timezone.utc).strftime("%H:%M:%S")
        if base is None:
            print(f"{when}   {name:26s}  (too few subscriber rows)")
            continue
        parts = []
        for e in eps:
            if e["target_ratio"] is None:
                parts.append(f"[t+{e['start']} {e['dur']}s {e['peak_ms']:.0f}ms no-A-data]")
                continue
            tag = ""
            if probes:
                start = t0 / 1e6 + e["start"]
                end, par, single = min(probes, key=lambda p: abs(p[0] - start))
                # Probes last up to ~50 s and log only their end, so an episode caused by
                # one starts before that end.
                near = -5 <= end - start <= 60
                tag = f" probe-end{end - start:+.0f}s@{par:.0f}Mbps{'' if near else '(far)'}"
            parts.append(f"[t+{e['start']} {e['dur']}s {e['peak_ms']:.0f}ms x{e['target_ratio']:.2f} {e['leg']}"
                         f" r{e['retx']} l{e['lost']} m{e['missing_ids']}{tag}]")
        print(f"{when}   {name:26s} {base:6.1f}   {'  '.join(parts) or 'none'}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--publisher")
    ap.add_argument("--subscriber")
    ap.add_argument("--scan", nargs=2, metavar=("HOSTA_DIR", "RESULTS_DIR"),
                    help="classify latency episodes in every cellNN-<name> that has both files")
    ap.add_argument("--probes", help="uplink.csv from uplink-monitor.sh; tags episodes with the nearest probe end")
    ap.add_argument("--from", dest="lo", type=int, default=0, help="seconds from first publisher poll")
    ap.add_argument("--to", dest="hi", type=int, default=160)
    args = ap.parse_args()

    if args.scan:
        return scan(*args.scan, probes_path=args.probes)
    if not (args.publisher and args.subscriber):
        ap.error("--publisher and --subscriber are required unless --scan is given")
    t0, pub = load_publisher(args.publisher)
    per_s, lost = load_subscriber(args.subscriber, t0)
    nan = float("nan")
    print("   t | A tgt_k A sent_k enc_fps retx nack | B fps xport_p50 xport_max missing_ids lost_cum rx_Mbps")
    for s in range(args.lo, args.hi):
        a = pub.get(s, {"target_k": nan, "sent_k": nan, "enc_fps": nan, "retx": 0, "nack": 0})
        line = f"{s:4d} | {a['target_k']:6.0f} {a['sent_k']:7.0f} {a['enc_fps']:6.1f} {a['retx']:4d} {a['nack']:4d} |"
        v = per_s.get(s)
        if v:
            lat = [x[0] for x in v]
            missing = sum(max(0, x[1] - 1) for x in v)
            line += f" {len(v):4d} {statistics.median(lat):8.1f} {max(lat):8.1f} {missing:10d} {lost.get(s, 0):8d} {v[-1][2]:6.2f}"
        else:
            line += "    0"
        print(line)


if __name__ == "__main__":
    main()
