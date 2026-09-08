#!/usr/bin/env python3
"""E2 sweep report: what each bitrate cap bought, publisher-side.

One report per experiment rather than one per cell. Twelve per-cell frame reports
answer "where did this frame's latency go", which is a drill-down question; the
sweep's own question is where the picture starts degrading as the cap falls, and
only a cross-cell view answers it.

Publisher-side only, deliberately. The receive-side decode is currently producing
incoherent frames (consecutive samples correlate 0.32-0.71 where a real sequence
gives 0.99), so no quality figure derived from received pixels can be trusted yet.
Everything here comes from the encoder's own stats and is unaffected by that.
"""
import argparse
import collections
import csv
import glob
import json
import os
import statistics

PIX_PER_S = 1600 * 1300 * 30  # production geometry


def cell_is_complete(path):
    """Whether the cell's run actually finished.

    A snapshot file grows while its run is in flight, so summarising one mid-run
    describes the part that has happened so far and says nothing about the rest.
    That is not a partial answer, it is a wrong one: e2-1500k-r1 read as "holds
    1600x1300, zero steps" when summarised 40 s in, and stepped down twice at t+84
    and t+89. The completion line in the run log is the only thing that says the
    cell is over.
    """
    log = path[: -len(".jsonl")] + ".log"
    if not os.path.exists(log):
        return False
    try:
        with open(log, errors="replace") as fh:
            return "run complete:" in fh.read()
    except OSError:
        return False


def cell_summary(path):
    rows = []
    for line in open(path):
        try:
            snap = json.loads(line)
        except json.JSONDecodeError:
            continue
        vo = snap.get("video_out") or {}
        if vo.get("frames_encoded"):
            rows.append((snap["t_unix_us"] / 1e6, vo))
    if len(rows) < 3:
        return None

    sent, qps, grants, widths = [], [], [], set()
    for (t1, a), (t2, b) in zip(rows, rows[1:]):
        dt = t2 - t1
        dby = b["bytes_sent"] - a["bytes_sent"]
        if dby > 0 and dt > 0:
            sent.append(dby * 8 / dt)
        dfr = b["frames_encoded"] - a["frames_encoded"]
        dqp = b["qp_sum"] - a["qp_sum"]
        if dfr > 0:
            qps.append(dqp / dfr)
    for _, vo in rows:
        if vo.get("target_bitrate_bps"):
            grants.append(vo["target_bitrate_bps"])
        if vo.get("frame_width"):
            widths.add((vo["frame_width"], vo["frame_height"]))
    last = rows[-1][1]

    # The ladder as it actually happened, with the time of each change, and how long
    # the encoder spent at each rung. "Final resolution" alone hides a cell that held
    # production geometry for 84 s and then collapsed.
    t0 = rows[0][0]
    ladder, prev = [], None
    for t, vo in rows:
        r = (vo.get("frame_width"), vo.get("frame_height"))
        if r != prev:
            ladder.append((t - t0, r))
            prev = r
    dwell = collections.Counter(
        (vo.get("frame_width"), vo.get("frame_height")) for _, vo in rows).most_common()

    return {
        "ladder": ladder,
        "dwell": dwell,
        "held_source_s": sum(1 for _, vo in rows
                             if (vo.get("frame_width"), vo.get("frame_height")) == (1600, 1300)),
        "encoder": last.get("encoder_implementation", ""),
        "grant_mbps": statistics.mean(grants) / 1e6 if grants else float("nan"),
        "sent_mbps": statistics.mean(sent) / 1e6 if sent else float("nan"),
        "sent_p50_mbps": statistics.median(sent) / 1e6 if sent else float("nan"),
        "bpp": (statistics.mean(sent) / PIX_PER_S) if sent else float("nan"),
        "qp_mean": statistics.mean(qps) if qps else float("nan"),
        "qp_max": max(qps) if qps else float("nan"),
        "resolutions": sorted(widths),
        "res_changes": last.get("quality_limitation_resolution_changes", 0),
        "final_res": f"{last.get('frame_width')}x{last.get('frame_height')}",
        "fps": last.get("frames_per_second", 0.0),
        "bw_limited_s": last.get("quality_limitation_bandwidth_s", 0.0),
        "cpu_limited_s": last.get("quality_limitation_cpu_s", 0.0),
        "nack": last.get("nack_count", 0),
        "pli": last.get("pli_count", 0),
        "frames": last.get("frames_encoded", 0),
        "polls": len(rows),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True, help="directory of <room>.jsonl snapshots")
    ap.add_argument("--out-csv", help="write the sweep table here")
    args = ap.parse_args()

    cells, skipped = {}, []
    for p in sorted(glob.glob(os.path.join(args.dir, "e2-*.jsonl"))):
        room = os.path.basename(p)[: -len(".jsonl")]
        if not cell_is_complete(p):
            skipped.append(room)
            continue
        s = cell_summary(p)
        if s:
            cap_k = int(room.split("-")[1].rstrip("k"))
            s["room"], s["cap_mbps"] = room, cap_k / 1000
            cells[room] = s

    if skipped:
        print(f"IN FLIGHT, not summarised: {', '.join(skipped)}\n")
    if not cells:
        print(f"no completed cells in {args.dir}")
        return

    hdr = (f"{'cell':<16}{'cap':>6}{'grant':>8}{'sent':>8}{'bpp':>8}"
           f"{'QP':>6}{'QPmax':>7}{'final res':>12}{'fps':>6}{'bw_s':>7}{'nack':>7}")
    print(hdr)
    print("-" * len(hdr))
    for room in sorted(cells, key=lambda r: (cells[r]["cap_mbps"], r)):
        c = cells[room]
        print(f"{room:<16}{c['cap_mbps']:>6.1f}{c['grant_mbps']:>8.3f}{c['sent_mbps']:>8.3f}"
              f"{c['bpp']:>8.4f}{c['qp_mean']:>6.1f}{c['qp_max']:>7.1f}"
              f"{c['final_res']:>12}{c['fps']:>6.1f}"
              f"{c['bw_limited_s']:>7.1f}{c['nack']:>7}")

    # The staircase is read from the resolutions actually observed, NOT from
    # quality_limitation_resolution_changes: that counter reads 0 on cells that
    # plainly stepped, because the step happened before the first poll. The set of
    # encoded resolutions is the evidence; the counter is not.
    print("\nresolutions encoded per cell (the staircase)")
    for room in sorted(cells, key=lambda r: cells[r]["cap_mbps"]):
        c = cells[room]
        rungs = " -> ".join(f"{w}x{h}@{t:.0f}s" for t, (w, h) in c["ladder"])
        print(f"  {room:<16} {rungs}")
        dwell = ", ".join(f"{w}x{h} {n}/{c['polls']}" for (w, h), n in c["dwell"])
        print(f"  {'':<16} dwell: {dwell}   (counter says {c['res_changes']})")

    # The cell is only interpretable when the CAP was the binding constraint. If the
    # grant came in below it, congestion control set the rate and the cell measured
    # the link instead of the cap.
    print("\nadmission: grant vs cap")
    for room in sorted(cells, key=lambda r: cells[r]["cap_mbps"]):
        c = cells[room]
        ratio = c["grant_mbps"] / c["cap_mbps"]
        verdict = "cap binding" if ratio >= 0.95 else "BWE BINDING - cell measures the link"
        print(f"  {room:<16} grant/cap {ratio:5.2f}  {verdict}")

    if args.out_csv:
        fields = ["room", "cap_mbps", "grant_mbps", "sent_mbps", "sent_p50_mbps", "bpp",
                  "qp_mean", "qp_max", "final_res", "res_changes", "fps",
                  "bw_limited_s", "cpu_limited_s", "nack", "pli", "frames", "polls",
                  "encoder"]
        with open(args.out_csv, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
            w.writeheader()
            for room in sorted(cells, key=lambda r: (cells[r]["cap_mbps"], r)):
                w.writerow(cells[room])
        print(f"\nwrote {args.out_csv}")


if __name__ == "__main__":
    main()
