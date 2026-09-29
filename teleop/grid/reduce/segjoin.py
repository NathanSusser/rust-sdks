"""Join pub.csv <-> A wire <-> B wire <-> subscriber.csv per frame (ported from segjoin.py / proof.py).

Joins:
  pub.csv <-> subscriber.csv    by capture_timestamp_us (the same value travels in the frame)
  pub.csv <-> A wire            by ORDER: the i-th published frame is the (i+shift)-th RTP
                                timestamp on A's wire, with `shift` in -3..+3 chosen so the
                                steady-state app_to_wire_a (packetize -> first packet on A's
                                wire) median is nearest 0 from above (>= -0.5 ms). A wrong
                                shift makes that median jump by a frame interval, so the rule
                                is sharp.
  A wire <-> B wire             by RTP timestamp (the SFU forwards the timestamp unchanged).

RTP timestamps are unwrapped (mod 2^32) in first-seen order before sorting, so a run that
crosses the wrap still orders correctly.
"""
from __future__ import annotations

import csv
import statistics
from dataclasses import dataclass, field

from .pcap_extract import Flow, Pkt

DUP_WINDOW_S = 5.0


def _num(v):
    if v is None:
        return None
    v = v.strip()
    if not v:
        return None
    try:
        return float(v)
    except ValueError:
        return None


def _zero_ts(row: dict) -> bool:
    """A row with a missing/zero absolute timestamp poisons every stage measured from it."""
    for k, v in row.items():
        if k and k.endswith("_timestamp_us"):
            x = _num(v)
            if x is not None and x <= 0:
                return True
    return False


def load_pub(path) -> list[dict]:
    """Published frames in order: frame_id, capture_us, packetize_us, encode_ms, frame_id_gap."""
    out = []
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            c = _num(r.get("capture_timestamp_us"))
            p = _num(r.get("webrtc_packetize_timestamp_us"))
            if not c or not p or c <= 0 or p <= 0:
                continue
            fid = _num(r.get("frame_id"))
            out.append({"frame_id": int(fid) if fid is not None else None, "capture_us": int(c), "packetize_us": int(p),
                        "encode_ms": _num(r.get("encode_ms")), "frame_id_gap": _num(r.get("frame_id_gap"))})
    return out


SUB_COLS = ("webrtc_receive_timestamp_us", "frame_gpu_complete_timestamp_us", "decode_ms", "render_ms",
            "e2e_to_gpu_complete_ms", "packets_lost", "frames_dropped", "freeze_count", "receive_qp")


def load_sub(path) -> dict[int, dict]:
    """capture_us -> subscriber row (numbers). Rows with zero timestamps are dropped."""
    out = {}
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            c = _num(r.get("capture_timestamp_us"))
            w = _num(r.get("webrtc_receive_timestamp_us"))
            if not c or not w or _zero_ts(r):
                continue
            d = {k: _num(r.get(k)) for k in SUB_COLS}
            d["frame_id"] = _num(r.get("frame_id"))
            out[int(c)] = d
    return out


@dataclass
class Wire:
    """Per-RTP-timestamp frame on one host's wire, plus per-packet series for rates."""
    frames: dict = field(default_factory=dict)      # rtpts -> [first_t, last_t, npkts, bytes, marker]
    order: list = field(default_factory=list)       # rtpts sorted by unwrapped timestamp
    unwrapped: dict = field(default_factory=dict)   # rtpts -> unwrapped ticks (first-seen frame = 0)
    pkt_times: list = field(default_factory=list)   # (t, udplen, rtpts) of video packets
    dup_times: list = field(default_factory=list)   # t of duplicate seq (same seq within 5 s)
    nack_rx: list = field(default_factory=list)     # NACKs arriving from the SFU
    nack_tx: list = field(default_factory=list)     # NACKs sent to the SFU
    rtcp_rx: int = 0                                # RTCP packets from the SFU that tshark could dissect
    rtcp_tx: int = 0                                # RTCP packets to the SFU that tshark could dissect


def build_wire(pkts: list[Pkt], flow: Flow, role: str) -> Wire:
    w = Wire()
    if flow.video_pt is None:
        return w
    src, dst = (flow.local, flow.sfu) if role == "a" else (flow.sfu, flow.local)
    seen: dict[int, float] = {}
    first_seen: list[int] = []
    for p in pkts:
        if p.src == src and p.dst == dst and p.pt == flow.video_pt and p.rtpts is not None:
            if p.seq in seen and p.t - seen[p.seq] < DUP_WINDOW_S:
                w.dup_times.append(p.t)
            seen[p.seq] = p.t
            w.pkt_times.append((p.t, p.udplen, p.rtpts))
            d = w.frames.get(p.rtpts)
            if d is None:
                w.frames[p.rtpts] = [p.t, p.t, 1, p.udplen, p.marker]
                first_seen.append(p.rtpts)
            else:
                if p.t < d[0]:
                    d[0] = p.t
                if p.t > d[1]:
                    d[1] = p.t
                d[2] += 1
                d[3] += p.udplen
                d[4] = d[4] or p.marker
        elif p.rtcp_pt:
            # Only RTCP whose first header is in the clear is visible: SRTCP encrypts everything
            # after it, so a NACK inside a compound packet that starts with RR/SR cannot be seen.
            if p.src == flow.sfu and p.dst == flow.local:
                w.rtcp_rx += 1
                if p.is_nack:
                    w.nack_rx.append(p.t)
            elif p.src == flow.local and p.dst == flow.sfu:
                w.rtcp_tx += 1
                if p.is_nack:
                    w.nack_tx.append(p.t)
    # unwrap in first-seen order
    unwrapped = {}
    prev = base = None
    for ts in first_seen:
        if prev is None:
            base = 0
        else:
            d = (ts - prev) & 0xFFFFFFFF
            if d >= 0x80000000:
                d -= 0x100000000
            base += d
        unwrapped[ts] = base
        prev = ts
    w.order = sorted(w.frames, key=lambda k: unwrapped[k])
    w.unwrapped = unwrapped
    return w


def choose_shift(pub: list[dict], a: Wire, epoch: float, steady_from_s: float = 30.0, min_n: int = 100):
    """Return (shift, steady median ms, steady_from_s used) or (None, None, None) if no shift puts
    app_to_wire_a >= -0.5 ms. The steady window falls back to t >= 15 s, then to all frames, on
    short cells."""
    ats = a.order
    best = None
    for frm in (steady_from_s, 15.0, 0.0):
        for sh in range(-3, 4):
            v = []
            for i, r in enumerate(pub):
                j = i + sh
                if j < 0 or j >= len(ats) or r["capture_us"] / 1e6 - epoch < frm:
                    continue
                v.append((a.frames[ats[j]][0] - r["packetize_us"] / 1e6) * 1e3)
            if len(v) < min_n:
                continue
            m = statistics.median(v)
            if m >= -0.5 and (best is None or m < best[1]):
                best = (sh, m)
        if best is not None:
            return best[0], best[1], frm
    return None, None, None


# RTP timestamps here are quantised to ~1 ms (90 ticks) of the capture clock, so a consecutive
# capture delta and RTP delta differ by up to ~90 ticks on a correctly joined pair; a misjoin
# is off by a whole frame interval (~3000 ticks at 30 fps).
ORDER_TOL_TICKS = 135


def order_agreement(pub: list[dict], a: Wire, shift: int) -> float | None:
    """Share of consecutive frame pairs whose capture delta (x 90 kHz) matches the RTP delta within ORDER_TOL_TICKS."""
    ats = a.order
    ok = tot = 0
    for i in range(1, len(pub)):
        j = i + shift
        if j - 1 < 0 or j >= len(ats):
            continue
        dc = (pub[i]["capture_us"] - pub[i - 1]["capture_us"]) * 0.09
        dt = (ats[j] - ats[j - 1]) & 0xFFFFFFFF
        tot += 1
        ok += abs(dc - dt) <= ORDER_TOL_TICKS
    return ok / tot if tot else None


def resolve(pub: list[dict], a: Wire, shift: int, epoch: float = 0.0, steady_from_s: float = 0.0) -> tuple[list, int]:
    """Per pub row, the A-wire RTP timestamp it belongs to, checked against the capture clock.

    The order join (row i -> i+shift) is taken wherever it agrees with the capture clock:
    unwrapped RTP ticks ~= capture_us * 0.09 + K, K the median over the order join in the same
    steady window choose_shift() validated the shift on (outside it the order join may be off by
    a frame, and those rows can outnumber the good ones). A row where
    it does not (a spurious wire "frame" such as an RTP padding probe inserted mid-run, or a frame
    missing from the capture) is re-joined to the nearest wire timestamp within ORDER_TOL_TICKS,
    or left unjoined. Returns (ts list aligned with pub, resyncs) where resyncs counts the runs of
    rows that had to leave the order join.
    """
    import bisect
    ats = a.order
    u = a.unwrapped
    diffs = [u[ats[i + shift]] - pub[i]["capture_us"] * 0.09
             for i in range(len(pub)) if 0 <= i + shift < len(ats)
             and pub[i]["capture_us"] / 1e6 - epoch >= steady_from_s]
    if not diffs:
        return [None] * len(pub), 0
    K = statistics.median(diffs)
    us = sorted((u[t], t) for t in ats)
    keys = [x[0] for x in us]
    out, resyncs, off_order = [], 0, False
    for i, r in enumerate(pub):
        exp = r["capture_us"] * 0.09 + K
        j = i + shift
        if 0 <= j < len(ats) and abs(u[ats[j]] - exp) <= ORDER_TOL_TICKS:
            out.append(ats[j]); off_order = False
            continue
        if not off_order:
            resyncs += 1; off_order = True
        k = bisect.bisect_left(keys, exp)
        best = None
        for c in (k - 1, k):
            if 0 <= c < len(keys) and abs(keys[c] - exp) <= ORDER_TOL_TICKS:
                if best is None or abs(keys[c] - exp) < abs(keys[best] - exp):
                    best = c
        out.append(us[best][1] if best is not None else None)
    return out, resyncs


SEGMENTS = ("app_to_wire_a", "emission_a", "in_flight", "arrival_b", "wire_to_app_b")


def join(pub: list[dict], sub: dict[int, dict] | None, a: Wire | None, b: Wire | None, epoch: float,
         wire_ts: list | None) -> list[dict]:
    """One row per published frame. `wire_ts` is resolve()'s output. Missing pieces leave their columns None."""
    rows = []
    for i, r in enumerate(pub):
        cap, pk = r["capture_us"], r["packetize_us"]
        row = {"t_s": cap / 1e6 - epoch, "frame_id": r["frame_id"], "capture_us": cap, "encode_ms": r["encode_ms"],
               "capture_to_packetize_ms": (pk - cap) / 1e3, "rtp_ts": None, "bytes": None, "packets_a": None, "packets_b": None, "received": 0,
               "receive_us": None, "owd": None, "capture_to_receive": None, "e2e": None, "decode": None, "render": None, "rendered": 0,
               "packets_lost": None}
        for k in SEGMENTS:
            row[k] = None
        s = sub.get(cap) if sub else None
        if s is not None:
            w = s["webrtc_receive_timestamp_us"]
            row.update(received=1, receive_us=w, owd=(w - pk) / 1e3, capture_to_receive=(w - cap) / 1e3, e2e=s["e2e_to_gpu_complete_ms"],
                       decode=s["decode_ms"], render=s["render_ms"], packets_lost=s["packets_lost"],
                       rendered=1 if (s["frame_gpu_complete_timestamp_us"] or 0) > 0 else 0)
        ts = wire_ts[i] if wire_ts else None
        if a is not None and ts is not None:
            a0, a1, n, by, _mk = a.frames[ts]
            row.update(rtp_ts=ts, bytes=by, packets_a=n, app_to_wire_a=(a0 - pk / 1e6) * 1e3, emission_a=(a1 - a0) * 1e3)
            if b is not None and ts in b.frames:
                b0, b1, nb, _by, _ = b.frames[ts]
                row.update(packets_b=nb, in_flight=(b1 - a1) * 1e3, arrival_b=(b1 - b0) * 1e3)
                if s is not None:
                    row["wire_to_app_b"] = (s["webrtc_receive_timestamp_us"] / 1e6 - b1) * 1e3
        rows.append(row)
    return rows
