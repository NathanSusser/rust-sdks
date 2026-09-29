"""Per-frame, per-second and per-spike tables from the joined frames (ported from proof.py).

Writes into <cell>/reduced/:
  frames.csv    one row per published frame: t_s, bytes, packets, every segment, owd, e2e,
                decode, render, size ratio to the steady median, RFC 3550 jitter, B interval
  seconds.csv   one row per second of the cell (see SECONDS_COLS)
  spikes.csv    every frame with owd > 100 ms: dominant segment by excess over the steady
                median, per-segment excess, size ratio, kind
  episodes.csv  spikes grouped with a > 1 s gap between groups
  inflight.csv  10 Hz samples of A-tx count - B-rx count (clean only when packets_lost = 0)
"""
from __future__ import annotations

import bisect
import csv
import json
import math
import statistics
from collections import Counter, defaultdict

from .segjoin import SEGMENTS

STEADY_FROM_S = 15.0
SPIKE_MS = 100.0
EPISODE_GAP_S = 1.0
BIGFRAME_RATIO = 2.0
TRANSIENT_WINDOW_S = 15.0
TRANSIENT_MODEM_INDEX = 2.0

FRAME_COLS = ("t_s", "frame_id", "capture_us", "receive_us", "rtp_ts", "bytes", "packets_a", "packets_b", "received", "rendered",
              *SEGMENTS, "owd", "capture_to_receive", "e2e", "decode", "render", "encode_ms", "capture_to_packetize_ms", "size_ratio", "jitter_rfc3550",
              "interval_b", "packets_lost")
SECONDS_COLS = ("t", "a_bytes", "a_packets", "b_bytes", "b_packets", "frames", "owd_p50", "owd_max",
                "kq_a_bytes_max", "kq_b_pkts_max", "lost_delta", "nacks_sfu_to_a", "nacks_b_to_sfu",
                "dup_a", "dup_b", "inflight_max", "modem_a", "modem_b", "x19ef_a", "x19ef_b",
                "qp", "frames_encoded", "fps_encoded", "target_kbps", "quality_limitation_reason",
                "rsrp_a", "snr_a", "rsrp_b", "snr_b")
SPIKE_COLS = ("t", "owd", "dominant", "kind", *(f"ex_{k}" for k in SEGMENTS), "bytes", "size_ratio", "packets_a",
              "b_nacks_0_3s")
EPISODE_COLS = ("start_s", "end_s", "n", "max_ms", "dominant_segment", "kinds")


def _fmt(v):
    if v is None:
        return ""
    if isinstance(v, float):
        if not math.isfinite(v):
            return ""
        return f"{v:.3f}"
    return str(v)


def write_csv(path, cols, rows) -> None:
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for r in rows:
            w.writerow([_fmt(r.get(c)) for c in cols])


def read_csv(path) -> list[dict]:
    """Read one of our tables back: numbers as float, blanks as None, other strings kept."""
    out = []
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            d = {}
            for k, v in r.items():
                if v is None or v == "":
                    d[k] = None
                    continue
                try:
                    d[k] = float(v)
                except ValueError:
                    d[k] = v
            out.append(d)
    return out


def _pct(v, p):
    if not v:
        return None
    v = sorted(v)
    return v[min(len(v) - 1, int(round(p * (len(v) - 1))))]


# ------------------------------------------------------------------ side inputs

def read_hops(path, epoch: float) -> list[dict]:
    """hops.csv (A or B layout) -> [{t, kq_bytes, kq_pkts, rsrp, snr, qdisc_dropped}] relative to epoch."""
    out = []
    try:
        f = open(path, newline="")
    except (FileNotFoundError, TypeError):
        return out
    with f:
        for r in csv.DictReader(f):
            try:
                t = float(r["unix_ms"]) / 1000 - epoch
            except (KeyError, TypeError, ValueError):
                continue

            def g(k, cast=float):
                v = (r.get(k) or "").strip()
                try:
                    return cast(float(v)) if v else None
                except ValueError:
                    return None
            out.append({"t": t, "kq_bytes": g("qdisc_backlog_bytes", int), "kq_pkts": g("qdisc_backlog_pkts", int),
                        "rsrp": g("nr_rsrp_dbm"), "snr": g("nr_snr_db"), "qdisc_dropped": g("qdisc_dropped", int)})
    return out


def read_webrtc_stats(path, epoch: float) -> dict:
    """A's WebRTC stats jsonl -> per-poll video_out series and the run_metadata record."""
    polls, meta = [], None
    try:
        f = open(path)
    except (FileNotFoundError, TypeError):
        return {"polls": [], "meta": None}
    with f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if d.get("record") == "run_metadata":
                meta = d
                continue
            vo = d.get("video_out")
            if vo and d.get("t_unix_us"):
                polls.append({"t": d["t_unix_us"] / 1e6 - epoch, **vo})
    polls.sort(key=lambda p: p["t"])
    return {"polls": polls, "meta": meta}


def encoder_per_second(polls: list[dict]) -> dict[int, dict]:
    """QP per second = delta qp_sum / delta frames_encoded between consecutive polls, assigned to the later poll's second."""
    out = {}
    for p0, p1 in zip(polls, polls[1:]):
        df = (p1.get("frames_encoded") or 0) - (p0.get("frames_encoded") or 0)
        dq = (p1.get("qp_sum") or 0) - (p0.get("qp_sum") or 0)
        dt = p1["t"] - p0["t"]
        s = int(math.floor(p1["t"]))
        out[s] = {"qp": dq / df if df > 0 and dq >= 0 else None, "frames_encoded": df if df >= 0 else None,
                  "fps_encoded": df / dt if dt > 0 and df >= 0 else None,
                  "target_kbps": p1["target_bitrate_bps"] / 1000 if p1.get("target_bitrate_bps") else None,
                  "quality_limitation_reason": p1.get("quality_limitation_reason")}
    return out


# ------------------------------------------------------------------ tables

def finish_frames(rows: list[dict]) -> float | None:
    """Fill size_ratio, jitter_rfc3550 and interval_b in place. Returns the steady median frame bytes."""
    steady = [r["bytes"] for r in rows if r["bytes"] and r["t_s"] >= STEADY_FROM_S]
    if not steady:
        steady = [r["bytes"] for r in rows if r["bytes"]]
    med = statistics.median(steady) if steady else None
    J = 0.0
    prev_owd = prev_rx = None
    for r in rows:
        r["size_ratio"] = r["bytes"] / med if med and r["bytes"] else None
        if r["owd"] is None:
            continue
        if prev_owd is not None:
            # RFC 3550 6.4.1 with S = packetize (A) and R = webrtc_receive (B): D = owd_j - owd_i.
            J += (abs(r["owd"] - prev_owd) - J) / 16.0
            r["jitter_rfc3550"] = J
            r["interval_b"] = (r["receive_us"] - prev_rx) / 1e3
        prev_owd, prev_rx = r["owd"], r["receive_us"]
    return med


def steady_medians(rows: list[dict]) -> dict:
    med = {}
    for k in SEGMENTS:
        v = [r[k] for r in rows if r[k] is not None and r["t_s"] >= STEADY_FROM_S]
        if not v:
            v = [r[k] for r in rows if r[k] is not None]
        med[k] = statistics.median(v) if v else None
    return med


def classify(t: float, dom: str | None, ratio: float | None, modem_a_index: float | None,
             kq_a: float | None) -> str:
    """Spike kind, generalising the spike ledger's hand labels (spike-ledger-all-runs.html kindOf):
      transient: t < 15 s, delay carried by in_flight on a normal-size frame, while A's modem
                 activity index is >= 2x its own steady state or A's kernel queue is non-empty
                 (the ledger labelled exactly this pattern by hand, on one cell)
      bigframe:  dominant segment is on the emission side (app_to_wire_a, emission_a, arrival_b)
                 and the frame is >= 2x the steady median size
      inflight:  in_flight dominant
      bhost:     wire_to_app_b dominant (B kernel -> B app)
      other:     small emission jitter on a normal-size frame
      unjoined:  the frame has no wire segments, so no dominant segment exists
    """
    if dom is None:
        return "unjoined"
    small = ratio is None or ratio < BIGFRAME_RATIO
    if (t < TRANSIENT_WINDOW_S and dom == "in_flight" and small
            and ((modem_a_index is not None and modem_a_index >= TRANSIENT_MODEM_INDEX) or (kq_a or 0) > 0)):
        return "transient"
    if dom in ("app_to_wire_a", "emission_a", "arrival_b") and not small:
        return "bigframe"
    if dom == "in_flight":
        return "inflight"
    if dom == "wire_to_app_b":
        return "bhost"
    return "other"


def spikes_and_episodes(rows, med, modem_a_index: dict, kq_a_by_s: dict, b_nacks_rel: list[float]):
    spikes = []
    bn = sorted(b_nacks_rel)
    for r in rows:
        if r["owd"] is None or r["owd"] <= SPIKE_MS:
            continue
        ex = {k: (r[k] - med[k]) if (r[k] is not None and med.get(k) is not None) else None for k in SEGMENTS}
        have = {k: v for k, v in ex.items() if v is not None}
        dom = max(have, key=have.get) if len(have) == len(SEGMENTS) else None
        s = int(math.floor(r["t_s"]))
        near = bisect.bisect_right(bn, r["t_s"] + 0.3) - bisect.bisect_left(bn, r["t_s"] - 0.3)
        x = {"t": r["t_s"], "owd": r["owd"], "dominant": dom, "bytes": r["bytes"], "size_ratio": r["size_ratio"],
             "packets_a": r["packets_a"], "b_nacks_0_3s": near}
        for k in SEGMENTS:
            x[f"ex_{k}"] = ex[k]
        x["kind"] = classify(r["t_s"], dom, r["size_ratio"], modem_a_index.get(s), kq_a_by_s.get(s))
        spikes.append(x)
    episodes = []
    for x in spikes:
        if episodes and x["t"] - episodes[-1]["end_s"] <= EPISODE_GAP_S:
            e = episodes[-1]
            e["end_s"] = x["t"]; e["n"] += 1; e["max_ms"] = max(e["max_ms"], x["owd"])
            e["_doms"][x["dominant"]] += 1; e["_kinds"][x["kind"]] += 1
        else:
            episodes.append({"start_s": x["t"], "end_s": x["t"], "n": 1, "max_ms": x["owd"],
                             "_doms": Counter({x["dominant"]: 1}), "_kinds": Counter({x["kind"]: 1})})
    for e in episodes:
        doms = Counter({k: v for k, v in e["_doms"].items() if k is not None})
        e["dominant_segment"] = doms.most_common(1)[0][0] if doms else None
        e["kinds"] = ";".join(f"{k}:{v}" for k, v in e["_kinds"].most_common())
        del e["_doms"], e["_kinds"]
    return spikes, episodes


def loss_timeline(sub_rows_by_time: list[tuple[float, float]]) -> tuple[int | None, dict[int, int]]:
    """packets_lost is NON-monotonic in subscriber.csv: total = max; an event = a second where it increased."""
    vals = [(t, v) for t, v in sub_rows_by_time if v is not None]
    if not vals:
        return None, {}
    events: dict[int, int] = {}
    prev = 0.0
    for t, v in sorted(vals):
        if v > prev:
            s = int(math.floor(t))
            events[s] = events.get(s, 0) + int(v - prev)
            prev = v
        elif v < prev:
            prev = v
    return int(max(v for _, v in vals)), events


def inflight_samples(a_tx: list[float], b_rx: list[float], duration_s: float, hz: int = 10):
    """(t, in_flight) at `hz` over [0, duration): A-tx count - B-rx count at the same instant (times rel epoch)."""
    a_tx = sorted(a_tx); b_rx = sorted(b_rx)
    out = []
    for i in range(int(duration_s * hz)):
        t = i / hz
        out.append((t, bisect.bisect_right(a_tx, t) - bisect.bisect_right(b_rx, t)))
    return out


def per_second(rows, a_pk, b_pk, nacks_a_visible, nacks_b_visible, a_dup, b_dup, nacks_a, nacks_b, loss_ev, inflight, hops_a, hops_b,
               modem_a, modem_b, x19ef_a, x19ef_b, enc, seconds) -> list[dict]:
    """All inputs are relative to epoch. a_pk/b_pk: [(t, udplen)]."""
    def bucket(pairs):
        d = defaultdict(lambda: [0, 0])
        for t, ln in pairs:
            s = int(math.floor(t)); d[s][0] += ln; d[s][1] += 1
        return d
    def count(ts):
        d = Counter(int(math.floor(t)) for t in ts); return d
    ab, bb = bucket(a_pk), bucket(b_pk)
    ad, bd, na, nb = count(a_dup), count(b_dup), count(nacks_a), count(nacks_b)
    fr = defaultdict(list)
    for r in rows:
        if r["owd"] is not None:
            fr[int(math.floor(r["t_s"]))].append(r["owd"])
    ifl = defaultdict(list)
    for t, v in inflight:
        ifl[int(math.floor(t))].append(v)
    def hmax(h, key):
        d = {}
        for x in h:
            if x[key] is None:
                continue
            s = int(math.floor(x["t"])); d[s] = max(d.get(s, x[key]), x[key])
        return d
    def hmean(h, key):
        d = defaultdict(list)
        for x in h:
            if x[key] is not None:
                d[int(math.floor(x["t"]))].append(x[key])
        return {s: sum(v) / len(v) for s, v in d.items()}
    kqa, kqb = hmax(hops_a, "kq_bytes"), hmax(hops_b, "kq_pkts")
    ra, sa, rb, sb = hmean(hops_a, "rsrp"), hmean(hops_a, "snr"), hmean(hops_b, "rsrp"), hmean(hops_b, "snr")
    out = []
    for s in seconds:
        o = fr.get(s, [])
        e = enc.get(s, {})
        out.append({"t": s, "a_bytes": ab[s][0] if s in ab else (0 if a_pk else None),
                    "a_packets": ab[s][1] if s in ab else (0 if a_pk else None),
                    "b_bytes": bb[s][0] if s in bb else (0 if b_pk else None),
                    "b_packets": bb[s][1] if s in bb else (0 if b_pk else None),
                    "frames": len(o), "owd_p50": _pct(o, .5), "owd_max": max(o) if o else None,
                    "kq_a_bytes_max": kqa.get(s), "kq_b_pkts_max": kqb.get(s),
                    "lost_delta": loss_ev.get(s, 0) if loss_ev is not None else None,
                    "nacks_sfu_to_a": na.get(s, 0) if nacks_a_visible else None,
                    "nacks_b_to_sfu": nb.get(s, 0) if nacks_b_visible else None,
                    "dup_a": ad.get(s, 0) if a_pk else None, "dup_b": bd.get(s, 0) if b_pk else None,
                    "inflight_max": max(ifl[s]) if ifl.get(s) else None,
                    "modem_a": modem_a.get(s) if modem_a else None, "modem_b": modem_b.get(s) if modem_b else None,
                    "x19ef_a": x19ef_a.get(s, 0) if x19ef_a is not None else None,
                    "x19ef_b": x19ef_b.get(s, 0) if x19ef_b is not None else None,
                    "qp": e.get("qp"), "frames_encoded": e.get("frames_encoded"), "fps_encoded": e.get("fps_encoded"),
                    "target_kbps": e.get("target_kbps"), "quality_limitation_reason": e.get("quality_limitation_reason"),
                    "rsrp_a": ra.get(s), "snr_a": sa.get(s), "rsrp_b": rb.get(s), "snr_b": sb.get(s)})
    return out
