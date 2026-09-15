#!/usr/bin/env python3
"""What a Qualcomm modem logged, and when, around a timed video cell.

Reads one or more QCSuper/QXDM .dlf captures and produces a self-contained HTML page:
for each host, a heatmap of log-record rate per log type over time, a bitrate strip on
the same time axis, and a table comparing each log type's rate idle-before, during,
and idle-after the video.

WHAT THIS CAN AND CANNOT TELL YOU. Every DLF record header carries a log-type code and
a timestamp, so record *rates* are readable without any Qualcomm software. The record
*contents* (actual uplink grant sizes, MCS, rank, BSR) use proprietary, firmware-
specific layouts that only QCAT/QXDM decode. So this page shows which log types switch
on, off, or scale with the video -- which narrows the IDs worth decoding -- and says
nothing about what those records contain. It does not name log types: guessed ID
ranges have already produced one wrong conclusion on this rig ("only 22 ML1 records").

Why not pcap: QCSuper's --pcap-dump emits only NR RRC OTA (0xB821) for 5G. A 16 MB,
59,725-record capture converted to 4 packets.

CLOCK. DLF modem timestamps are not a usable clock on their own. Each capture's offset
is estimated as the 0.1th-percentile record time minus the capture's known start on the
host clock (about +/-1 s), and records more than an hour from the capture median are
dropped as garbage. Counts are lower bounds: QCSuper drops frames on bad CRC, at a rate
that varies run to run and drifts within a run.

Usage:
  dlf_timeline.py --epoch 1789435198 --cell-end 150 \\
      --host "Host A=hosta.dlf@1789435078" --host "Host B=hostb.dlf@1789435197" \\
      --bitrate "Host A=publisher:grant-2000k-h264.jsonl" \\
      --bitrate "Host B=subscriber:subscriber.csv" -o dlf-timeline.html
"""
import argparse
import collections
import csv
import json
import math
import statistics
import struct
import sys
from pathlib import Path

GPS_EPOCH = 315964800          # 1980-01-06T00:00:00Z
TICK_S = 0.00125               # Qualcomm timestamp: upper 48 bits count 1.25 ms
NR_RANGE = (0xB800, 0xB9FF)


def parse_dlf(path):
    """(code, modem_seconds) for every record, resyncing past zero-filled gaps."""
    data = Path(path).read_bytes()
    out, i, gaps = [], 0, 0
    while i + 12 <= len(data):
        length, code = struct.unpack_from("<HH", data, i)
        if length < 12:
            # QCSuper can write a run of zero bytes mid-file after dismissing a
            # malformed log; a naive walk stops here and calls the file truncated.
            j = i
            while j < len(data) and data[j] == 0:
                j += 1
            i = j if j > i else i + 1
            gaps += 1
            continue
        ts = struct.unpack_from("<Q", data, i + 4)[0]
        out.append((code, (ts >> 16) * TICK_S + (ts & 0xFFFF) / 32768 * TICK_S + GPS_EPOCH))
        i += length
    return out, gaps


def window_rate(series, lo, hi, a, b):
    """Mean records/s over [a, b) seconds-from-epoch, or None if under 5 s of data."""
    a, b = max(a, lo), min(b, hi)
    if b - a < 5:
        return None
    return round(statistics.mean(series[a - lo:b - lo]), 2)


def analyse_host(label, path, capture_start, epoch, cell_end):
    recs, gaps = parse_dlf(path)
    if not recs:
        raise SystemExit(f"{label}: no records parsed from {path}")
    med = statistics.median(t for _, t in recs)
    good = [(c, t) for c, t in recs if abs(t - med) < 3600]
    times = sorted(t for _, t in good)
    offset = times[int(len(times) * 0.001)] - capture_start
    rel = [(c, t - offset - epoch) for c, t in good]
    lo = math.floor(min(t for _, t in rel))
    hi = math.floor(max(t for _, t in rel)) + 1
    counts = {}
    for c, t in rel:
        k = math.floor(t) - lo
        if 0 <= k < hi - lo:
            counts.setdefault(c, [0] * (hi - lo))[k] += 1
    windows = {
        "before": (lo + 5, -5),
        "during": (10, cell_end - 10),
        "after": (cell_end + 8, hi - 3),
    }
    rates = {c: {w: window_rate(s, lo, hi, *windows[w]) for w in windows} for c, s in counts.items()}
    return {
        "label": label, "path": str(path), "records": len(recs), "dropped_implausible": len(recs) - len(good),
        "gaps": gaps, "offset": round(offset, 2), "lo": lo, "hi": hi,
        "counts": counts, "rates": rates, "windows": windows,
    }


def load_bitrate(spec, epoch):
    """'publisher:<jsonl>' (delivered, from bytes_sent deltas) or 'subscriber:<csv>' (received)."""
    kind, _, path = spec.partition(":")
    points = []
    if kind == "publisher":
        polls = [json.loads(l) for l in open(path) if l.strip()]
        polls = [p for p in polls if p.get("video_out")]
        for p0, p1 in zip(polls, polls[1:]):
            dt = (p1["t_unix_us"] - p0["t_unix_us"]) / 1e6
            if dt > 0:
                mbps = (p1["video_out"].get("bytes_sent", 0) - p0["video_out"].get("bytes_sent", 0)) * 8 / 1e6 / dt
                points.append([round(p1["t_unix_us"] / 1e6 - epoch, 2), round(mbps, 3)])
        return {"kind": "sent by the publisher", "points": points}
    if kind == "subscriber":
        per_s = collections.defaultdict(list)
        for r in csv.DictReader(open(path)):
            try:
                # Parse both before indexing: a defaultdict key made for a blank row is an empty list.
                second = math.floor(int(r["webrtc_receive_timestamp_us"]) / 1e6 - epoch)
                mbps = float(r["receive_bitrate_mbps"])
            except (ValueError, KeyError, TypeError):
                continue
            per_s[second].append(mbps)
        points = [[k, sorted(v)[len(v) // 2]] for k, v in sorted(per_s.items())]
        return {"kind": "received by the subscriber", "points": points}
    raise SystemExit(f"bitrate spec must start with publisher: or subscriber:, got {spec!r}")


def choose_codes(hosts, rows):
    """Top log types by volume, plus the ones that change most with the video."""
    volume = collections.Counter()
    for h in hosts:
        for c, s in h["counts"].items():
            volume[c] += sum(s)

    def movement(c):
        best = 0.0
        for h in hosts:
            r = h["rates"].get(c)
            if not r or r["during"] is None:
                continue
            idle = [x for x in (r["before"], r["after"]) if x is not None]
            if not idle:
                continue
            base = statistics.mean(idle)
            if max(base, r["during"]) < 2:          # too sparse to call a change
                continue
            best = max(best, abs(math.log2((r["during"] + 1) / (base + 1))))
        return best

    by_volume = [c for c, _ in volume.most_common(rows // 2)]
    by_move = sorted(volume, key=movement, reverse=True)[:rows // 2]
    chosen = list(dict.fromkeys(by_volume + by_move))[:rows]
    chosen.sort(key=lambda c: (0 if NR_RANGE[0] <= c <= NR_RANGE[1] else 1, -volume[c]))
    return chosen


def build(args):
    hosts = []
    for spec in args.host:
        label, _, rest = spec.partition("=")
        path, _, start = rest.rpartition("@")
        hosts.append(analyse_host(label, path, int(start), args.epoch, args.cell_end))
    bitrates = {}
    for spec in args.bitrate or []:
        label, _, rest = spec.partition("=")
        bitrates[label] = load_bitrate(rest, args.epoch)
    codes = choose_codes(hosts, args.rows)
    span = [min(h["lo"] for h in hosts), max(h["hi"] for h in hosts)]
    b0 = math.floor(span[0] / args.bin) * args.bin
    nbins = math.ceil((span[1] - b0) / args.bin)
    page = {"epoch": args.epoch, "cell_end": args.cell_end, "bin": args.bin, "bin_start": b0, "bins": nbins,
            "span": span, "title": args.title,
            "codes": [f"0x{c:04X}" for c in codes],
            "nr": [NR_RANGE[0] <= c <= NR_RANGE[1] for c in codes], "hosts": []}
    for h in hosts:
        grid = []
        for c in codes:
            s = h["counts"].get(c)
            row = []
            for k in range(nbins):
                t0 = b0 + k * args.bin
                vals = [s[t - h["lo"]] for t in range(t0, t0 + args.bin) if s and h["lo"] <= t < h["hi"]] if s else \
                       [0 for t in range(t0, t0 + args.bin) if h["lo"] <= t < h["hi"]]
                row.append(round(sum(vals) / len(vals), 2) if vals else None)
            grid.append(row)
        page["hosts"].append({
            "label": h["label"], "records": h["records"], "dropped": h["dropped_implausible"], "gaps": h["gaps"],
            "offset": h["offset"], "lo": h["lo"], "hi": h["hi"], "grid": grid,
            "rates": [h["rates"].get(c, {"before": None, "during": None, "after": None}) for c in codes],
            "bitrate": bitrates.get(h["label"]),
        })
    return page


def print_summary(page):
    labels = [h["label"] for h in page["hosts"]]
    head = "log type  " + "".join(f"| {l:^26s} " for l in labels)
    print(head)
    print("          " + "".join("| before  during   after   " for _ in labels))
    fmt = lambda v: "     -" if v is None else f"{v:6.1f}"
    for i, code in enumerate(page["codes"]):
        line = f"{code}{' NR' if page['nr'][i] else '   '}  "
        for h in page["hosts"]:
            r = h["rates"][i]
            line += f"| {fmt(r['before'])}  {fmt(r['during'])}  {fmt(r['after'])}  "
        print(line)
    for h in page["hosts"]:
        print(f"{h['label']}: {h['records']} records, {h['gaps']} zero-byte gap(s), {h['dropped']} implausible "
              f"timestamps dropped, modem-minus-host offset {h['offset']:+.2f} s, span t{h['lo']:+d}..t{h['hi']:+d}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--epoch", type=int, required=True, help="cell start, host clock (Unix seconds)")
    ap.add_argument("--cell-end", type=int, required=True, help="seconds from epoch when video stopped")
    ap.add_argument("--host", action="append", required=True, help='"LABEL=path.dlf@capture_start_unix"')
    ap.add_argument("--bitrate", action="append", help='"LABEL=publisher:x.jsonl" or "LABEL=subscriber:x.csv"')
    ap.add_argument("--bin", type=int, default=5)
    ap.add_argument("--rows", type=int, default=18)
    ap.add_argument("--title", default="Modem log timeline")
    ap.add_argument("-o", "--output", required=True)
    args = ap.parse_args()
    page = build(args)
    print_summary(page)
    template = (Path(__file__).with_name("dlf_timeline.html")).read_text(encoding="utf-8")
    blob = json.dumps(page).replace("</", "<\\/")
    Path(args.output).write_text(template.replace("__DATA__", blob).replace("__TITLE__", args.title), encoding="utf-8")
    print(f"wrote {args.output}")


if __name__ == "__main__":
    sys.exit(main())
