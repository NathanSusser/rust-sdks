#!/usr/bin/env python3
"""Join both hosts' per-hop logs for one cell and test where the queue sits.

Inputs are whatever each host recorded; any may be missing and is reported as such.
  --b-dir    Host B receive-around-cell.sh outdir: subscriber.csv, ping-sfu.txt, hops.csv
  --a-ping   LABEL=path for each Host A `ping -D` log (e.g. hop2=..., hop3=..., sfu=...)
  --a-jsonl  Host A publisher stats (.jsonl)
  --a-hops   Host A hop-recorder .hops.csv
  --probe    probe window as START_MS:END_MS (unix ms, from Host A's probe log)
  --epoch    cell epoch (unix s)

Prints per-second series aligned to the epoch and a verdict table for predictions P1-P5
in teleop-test-matrix/docs/QUEUE-LOCATION-EXPERIMENTS.md, and writes the series as JSON
(-o) for the evidence page.
"""
import argparse
import collections
import csv
import json
import math
import re
import statistics
from pathlib import Path

PING_RE = re.compile(r"^\[(\d+\.\d+)\].*icmp_seq=(\d+).*time=([\d.]+) ms")
PING_SEQ = re.compile(r"^\[(\d+\.\d+)\].*icmp_seq=(\d+)")


def ping_series(path):
    """{second: (median rtt ms or None, sent, answered)} from `ping -D` output."""
    per = collections.defaultdict(list)
    seen = {}
    for line in open(path, errors="replace"):
        m = PING_RE.match(line)
        if m:
            per[math.floor(float(m.group(1)))].append(float(m.group(3)))
            seen[int(m.group(2))] = True
    return {s: statistics.median(v) for s, v in per.items()}, len(seen)


def hop_ttl_series(path, ttl):
    """{second: median rtt ms} for one TTL from Host A's hop-ttl.csv; unanswered rows skipped."""
    per = collections.defaultdict(list)
    for r in csv.DictReader(open(path)):
        if r.get("ttl") == str(ttl) and r.get("rtt_ms"):
            per[math.floor(float(r["send_unix_ms"]) / 1000)].append(float(r["rtt_ms"]))
    return {s: statistics.median(v) for s, v in per.items()}


def per_second(pairs):
    per = collections.defaultdict(list)
    for t, v in pairs:
        per[math.floor(t)].append(v)
    return {s: statistics.median(v) for s, v in per.items()}


def load_b(bdir):
    out = {}
    sub = Path(bdir) / "subscriber.csv"
    if sub.exists():
        lat, lost = [], {}
        for r in csv.DictReader(open(sub)):
            try:
                t = int(r["webrtc_receive_timestamp_us"]) / 1e6
                lat.append((t, float(r["exposure_to_receive_ms"])))
                lost[math.floor(t)] = int(r["packets_lost"] or 0)
            except (ValueError, KeyError, TypeError):
                continue
        out["b_transport_ms"] = per_second(lat)
        out["b_packets_lost_cum"] = lost
    for ping in sorted(Path(bdir).glob("ping-*.txt")):
        # ping-10.1.20.16.txt (SFU media node), ping-10.1.20.21.txt (ingress); ping-sfu.txt (older runs)
        name = ping.stem[len("ping-"):]
        key = {"10.1.20.16": "b_sfu_rtt_ms", "sfu": "b_sfu_rtt_ms"}.get(name, f"b_{name.replace('.', '_')}_rtt_ms")
        out[key], _ = ping_series(ping)
    hops = Path(bdir) / "hops.csv"
    if hops.exists():
        rows = list(csv.DictReader(open(hops)))
        out["b_udp_rcvbuf_errors_cum"] = {math.floor(int(r["unix_ms"]) / 1000): int(r["udp_rcvbuf_errors"] or 0)
                                          for r in rows if r.get("udp_rcvbuf_errors")}
        out["b_rx_dropped_cum"] = {math.floor(int(r["unix_ms"]) / 1000): int(r["rx_dropped"] or 0) for r in rows}
    return out


def load_a_jsonl(path):
    polls = [json.loads(l) for l in open(path) if l.strip()]
    polls = [p for p in polls if p.get("video_out")]
    tgt, sent, retx = {}, {}, {}
    for p0, p1 in zip(polls, polls[1:]):
        dt = (p1["t_unix_us"] - p0["t_unix_us"]) / 1e6
        if dt <= 0:
            continue
        s = math.floor(p1["t_unix_us"] / 1e6)
        v0, v1 = p0["video_out"], p1["video_out"]
        tgt[s] = v1["target_bitrate_bps"] / 1e3
        sent[s] = (v1["bytes_sent"] - v0["bytes_sent"]) * 8 / 1e3 / dt
        retx[s] = v1["retransmitted_packets_sent"] - v0["retransmitted_packets_sent"]
    return {"a_target_kbps": tgt, "a_sent_kbps": sent, "a_retx": retx}


def load_a_hops(path):
    rows = list(csv.DictReader(open(path)))
    num = lambda v: float(v) if v not in (None, "") else None
    out = {"a_qdisc_backlog_pkts": {}, "a_qmi_tx_dropped_cum": {}, "a_qdisc_dropped_cum": {}}
    for r in rows:
        s = math.floor(int(r["unix_ms"]) / 1000)
        out["a_qdisc_backlog_pkts"][s] = num(r.get("qdisc_backlog_pkts"))
        out["a_qmi_tx_dropped_cum"][s] = num(r.get("qmi_tx_dropped"))
        out["a_qdisc_dropped_cum"][s] = num(r.get("qdisc_dropped"))
    return out


def dlf_rates(path, lo, hi, host_minus_utc):
    """Per-second record counts per log code between host-clock seconds lo..hi, streaming.

    Modem timestamps are network time (~UTC); both hosts run behind UTC together, so
    host = modem + host_minus_utc. Trust about +/-2 s: never use for sub-second ordering.
    """
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).resolve().parent))
    from dlf_timeline import iter_dlf
    per = collections.defaultdict(collections.Counter)
    for code, t in iter_dlf(path):
        s = math.floor(t + host_minus_utc)
        if lo <= s < hi:
            per[code][s] += 1
    return per


def window_stat(series, lo, hi, fn=max):
    vals = [v for s, v in series.items() if lo <= s < hi and v is not None]
    return fn(vals) if vals else None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--epoch", type=int, required=True)
    ap.add_argument("--b-dir")
    ap.add_argument("--a-ping", action="append", default=[])
    ap.add_argument("--a-jsonl")
    ap.add_argument("--a-hops")
    ap.add_argument("--a-hop-ttl", action="append", default=[],
                    help="LABEL=hop-ttl.csv (TTL-limited UDP probe; TTL 2 and 3 rows become LABEL_hop2/hop3)")
    ap.add_argument("--a-tcp", action="append", default=[], help="LABEL=csv with send_unix_ms,rtt_ms (TCP SYN->RST)")
    ap.add_argument("--a-dlf")
    ap.add_argument("--b-dlf")
    ap.add_argument("--host-minus-utc", type=float, default=-4.548,
                    help="seconds; host = modem timestamp + this (both hosts run behind UTC together)")
    ap.add_argument("--probe", help="START_MS:END_MS")
    ap.add_argument("-o", "--output")
    args = ap.parse_args()

    S = {}
    if args.b_dir:
        S.update(load_b(args.b_dir))
    for spec in args.a_ping:
        label, _, path = spec.partition("=")
        S[f"a_{label}_rtt_ms"], _ = ping_series(path)
    for spec in args.a_hop_ttl:
        # Host A's TTL-limited UDP probe: ICMP time-exceeded from each hop (echo is not answered).
        label, _, path = spec.partition("=")
        for ttl in (2, 3):
            series = hop_ttl_series(path, ttl)
            if series:
                S[f"a_{label}_hop{ttl}_rtt_ms"] = series
    for spec in args.a_tcp:
        label, _, path = spec.partition("=")
        per = collections.defaultdict(list)
        for r in csv.DictReader(open(path)):
            if r.get("rtt_ms"):
                per[math.floor(float(r["send_unix_ms"]) / 1000)].append(float(r["rtt_ms"]))
        S[f"a_{label}_rtt_ms"] = {s: statistics.median(v) for s, v in per.items()}
    if args.a_jsonl:
        S.update(load_a_jsonl(args.a_jsonl))
    if args.a_hops:
        S.update(load_a_hops(args.a_hops))

    e = args.epoch
    p0 = p1 = None
    if args.probe:
        a, _, b = args.probe.partition(":")
        p0, p1 = int(a) // 1000, int(b) // 1000
    print("series:", ", ".join(f"{k}({len(v)})" for k, v in S.items()))

    keys = [k for k in S if k == "b_transport_ms" or k.endswith("_rtt_ms")] + \
           [k for k in ("a_target_kbps", "a_sent_kbps", "a_qdisc_backlog_pkts") if k in S]
    lo, hi = (p0 - 20, p1 + 30) if p0 else (e, e + 300)
    print("\n  t_rel " + " ".join(f"{k[:16]:>16s}" for k in keys))
    for s in range(lo, hi):
        mark = " PROBE" if p0 and p0 <= s <= p1 else ""
        cells = []
        for k in keys:
            v = S[k].get(s)
            cells.append(f"{v:16.1f}" if isinstance(v, (int, float)) else f"{'-':>16s}")
        print(f"  {s - e:+5d} " + " ".join(cells) + mark)

    if p0:
        print("\nPREDICTIONS (baseline = 60 s before probe start, window = probe start .. end+10 s)")
        def rise(key):
            if key not in S:
                return None, None, None
            base = window_stat(S[key], p0 - 60, p0, statistics.median)
            peak = window_stat(S[key], p0, p1 + 10)
            return base, peak, (None if base is None or peak is None else peak - base)
        for key, label, test in (
                ("b_transport_ms", "P1 B transport delay >= 3x baseline", lambda b, p: p >= 3 * b),
                ("a_hop2_rtt_ms", "P2 A->hop2 RTT rise >= +100 ms", lambda b, p: p - b >= 100),
                ("a_sfu_rtt_ms", "   A->SFU RTT rise (context)", lambda b, p: p - b >= 100),
                ("b_sfu_rtt_ms", "P4 B->SFU RTT flat (<= +20 ms)", lambda b, p: p - b <= 20)):
            b, p, d = rise(key)
            verdict = "no data" if b is None or p is None else ("HOLDS" if test(b, p) else "FAILS")
            print(f"  {label:40s} base {b if b is None else round(b,1)}  peak {p if p is None else round(p,1)}  -> {verdict}")
        if "a_qdisc_backlog_pkts" in S:
            mx = window_stat(S["a_qdisc_backlog_pkts"], p0, p1 + 10)
            print(f"  {'P3 A qdisc backlog stays ~0':40s} max {mx}  -> {'HOLDS' if mx is not None and mx <= 5 else 'FAILS/no data'}")
        if "b_packets_lost_cum" in S:
            L = S["b_packets_lost_cum"]
            d = (window_stat(L, p1, p1 + 30) or 0) - (window_stat(L, p0 - 30, p0, min) or 0)
            print(f"  {'P5 B packets lost during probe':40s} {d}  -> {'HOLDS' if d == 0 else 'FAILS'}")
        if "a_target_kbps" in S:
            before = window_stat(S["a_target_kbps"], p0 - 10, p0)
            after = window_stat(S["a_target_kbps"], p0, p1 + 10, min)
            print(f"  {'P5 A target cut':40s} {before} -> {after}")

    if p0:
        # S2 readout R1-R3: which traffic classes waited. Rise = peak in the probe window
        # minus the median of the 60 s before it; one table, same window for every class.
        rtt_keys = [k for k in S if k.endswith("_rtt_ms") or k == "b_transport_ms"]
        print("\nCLASS COMPARISON (baseline = median 60 s before probe; peak = max over probe start .. end+10 s)")
        print(f"  {'series':34s} {'n in window':>11s} {'base ms':>8s} {'peak ms':>8s} {'rise ms':>8s}")
        for k in rtt_keys:
            base = window_stat(S[k], p0 - 60, p0, statistics.median)
            peak = window_stat(S[k], p0, p1 + 10)
            n = sum(1 for s in S[k] if p0 <= s <= p1 + 10)
            rise = None if base is None or peak is None else peak - base
            fmt = lambda v: f"{v:8.1f}" if v is not None else f"{'-':>8s}"
            print(f"  {k:34s} {n:11d} {fmt(base)} {fmt(peak)} {fmt(rise)}")

    for label, path in (("A", args.a_dlf), ("B", args.b_dlf)):
        if not (path and p0):
            continue
        if path.endswith(".csv"):
            # dlf_rates.py summary (second_rel_probe,code,count), written on the host that holds the DLF.
            per = collections.defaultdict(collections.Counter)
            for r in csv.DictReader(line for line in open(path) if not line.startswith("#")):
                per[int(r["code"], 16)][p0 + int(r["second_rel_probe"])] += int(r["count"])
        else:
            per = dlf_rates(path, p0 - 30, p1 + 30, args.host_minus_utc)
        totals = collections.Counter({c: sum(v.values()) for c, v in per.items()})
        top = [c for c, _ in totals.most_common(12)]
        cols = range(p0 - 10, p1 + 12)
        print(f"\nHOST {label} DLF records/s by code (host = modem {args.host_minus_utc:+.3f} s; trust +/-2 s)")
        print("  code    " + " ".join(f"{s - p0:+5d}" for s in cols))
        for c in top:
            print(f"  0x{c:04X}  " + " ".join(f"{per[c][s]:5d}" for s in cols))
        print("  ALL     " + " ".join(f"{sum(per[c][s] for c in per):5d}" for s in cols))

    if args.output:
        Path(args.output).write_text(json.dumps({"epoch": e, "probe": [p0, p1],
                                                 "series": {k: {str(s): v for s, v in d.items()} for k, d in S.items()}}))
        print(f"\nwrote {args.output}")


if __name__ == "__main__":
    main()
