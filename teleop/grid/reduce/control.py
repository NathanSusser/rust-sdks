"""Control path (data track): A's publisher seq log + B's receive log -> reduced/control.csv.

Inputs, both optional (an older harness or subscriber writes neither):
  hosta/control-pub.jsonl   one line per sample that reached the transport (the harness logs a
                            sample only after the send succeeded):
                            {"seq", "t_send_unix_us", "t_send_monotonic_us", "probe"}
  hostb/control.csv         seq,t_send_unix_us,t_recv_unix_us,owd_us,probe_token,transport
                            one row per sample B received (a duplicate arrival is another row)
Probe round trips come from A's WebRTC-stats jsonl, which reduce_cell already reads once:
frames.read_webrtc_stats() collects them in the same pass and write_probes() stores them.

Outputs:
  reduced/control.csv   CONTROL_COLS, one row per seq A published or B received, in seq order
  reduced/probes.csv    t,rtt_ms  (t = the stats poll that reported the round trip, s since epoch)
  reduce.json["control"]  window, counts, transport, reason (see CONTRACT.md "Added by reports v2")

The window every control metric uses is the publisher's own span minus EDGE_S at each end:
[first published t_send + 2 s, last published t_send - 2 s] (B's first/last t_send when A's log
is absent). A sample belongs to the window by its send time. Host clocks are PTP-locked, so
owd = B receive (B clock) - A send (A clock); B's owd_us column is used when present.
"""
from __future__ import annotations

import csv
import json
from collections import Counter
from pathlib import Path

PUB_LOG = "control-pub.jsonl"      # in hosta/
RECV_LOG = "control.csv"           # in hostb/
EDGE_S = 2.0
T_SEND_TOL_US = 1_000              # the same seq must carry the same send time in both logs
CONTROL_COLS = ("seq", "t_s", "sent", "received", "t_recv_s", "owd_ms", "ia_ms", "probe", "dups",
                "in_window", "transport")
PROBE_COLS = ("t", "rtt_ms")


def _int(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def read_pub_log(path: Path) -> tuple[dict[int, tuple[int, bool]], int] | None:
    """seq -> (t_send_unix_us, probe), streamed line by line; (map, unparsable lines).
    None when the file is absent."""
    p = Path(path)
    if not p.is_file():
        return None
    out: dict[int, tuple[int, bool]] = {}
    bad = 0
    with p.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                seq, t = int(d["seq"]), int(d["t_send_unix_us"])
            except (ValueError, KeyError, TypeError):
                bad += 1
                continue
            out.setdefault(seq, (t, bool(d.get("probe"))))
    return out, bad


def read_recv_log(path: Path) -> tuple[dict[int, dict], Counter, int] | None:
    """seq -> first arrival {t_send_us, t_recv_us, owd_us, probe, transport}; extra arrivals per
    seq; unparsable rows. None when the file is absent or has no header."""
    p = Path(path)
    if not p.is_file():
        return None
    first: dict[int, dict] = {}
    dups: Counter = Counter()
    bad = 0
    with p.open(newline="", encoding="utf-8", errors="replace") as f:
        rd = csv.DictReader(ln for ln in f if not ln.startswith("#"))
        if not rd.fieldnames:
            return None
        for r in rd:
            seq, ts, tr = _int(r.get("seq")), _int(r.get("t_send_unix_us")), _int(r.get("t_recv_unix_us"))
            if seq is None or tr is None:
                bad += 1
                continue
            owd = _int(r.get("owd_us"))
            if owd is None and ts is not None:
                owd = tr - ts
            rec = {"t_send_us": ts, "t_recv_us": tr, "owd_us": owd,
                   "probe": (_int(r.get("probe_token")) or 0) != 0, "transport": (r.get("transport") or "").strip()}
            if seq in first:
                dups[seq] += 1
                if tr < first[seq]["t_recv_us"]:     # a log written out of order: keep the earliest
                    first[seq] = rec
            else:
                first[seq] = rec
    return first, dups, bad


def _us(v, epoch: float) -> str:
    return "" if v is None else f"{v / 1e6 - epoch:.6f}"


def reduce_control(ha: Path, hb: Path, epoch: float, out: Path, notes: list, transport_var=None) -> dict:
    """Write reduced/control.csv and return the reduce.json["control"] block."""
    pub_r = read_pub_log(Path(ha) / PUB_LOG)
    recv_r = read_recv_log(Path(hb) / RECV_LOG) if Path(hb).is_dir() else None
    pub, bad_pub = pub_r if pub_r is not None else (None, 0)
    recv, dups, bad_recv = recv_r if recv_r is not None else (None, Counter(), 0)
    res = {"pub_log": pub is not None, "recv_log": recv is not None, "published_total": len(pub) if pub is not None else None,
           "received_total": len(recv) if recv is not None else None,
           "duplicates": sum(dups.values()) if recv is not None else None,
           "bad_lines": {"pub": bad_pub, "recv": bad_recv}, "window_s": None, "published": None, "received": None,
           "recv_not_published": None, "t_send_mismatch": None, "transport": None, "reason": None}

    reasons = []
    if pub is None:
        reasons.append(f"hosta/{PUB_LOG} absent (harness without --publisher-seq-log): delivered % and gaps "
                       "need the publisher's count and are never estimated")
    if recv is None:
        reasons.append(f"hostb/{RECV_LOG} absent (subscriber without --control-log): no control one-way, "
                       "delivery or gaps")
    if pub is not None and not pub:
        reasons.append(f"hosta/{PUB_LOG} is empty: the control publisher logged no sample")
    tr = Counter(r["transport"] for r in (recv or {}).values() if r["transport"])
    res["transport"] = tr.most_common(1)[0][0] if tr else (str(transport_var) if transport_var else None)
    if len(tr) > 1:
        notes.append(f"control: B logged several transports {dict(tr)}")

    # the window: the publisher's own span minus EDGE_S at each end (B's view when A's log is absent)
    sends = [t for t, _ in pub.values()] if pub else [r["t_send_us"] for r in (recv or {}).values()
                                                     if r["t_send_us"] is not None]
    lo = hi = None
    if sends:
        lo, hi = min(sends) + EDGE_S * 1e6, max(sends) - EDGE_S * 1e6
        if hi <= lo:
            reasons.append(f"control log spans less than {2 * EDGE_S:.0f} s: no window")
            lo = hi = None
        else:
            res["window_s"] = [round(lo / 1e6 - epoch, 6), round(hi / 1e6 - epoch, 6)]
    in_win = (lambda t: t is not None and lo <= t <= hi) if lo is not None else (lambda t: False)

    seqs = sorted(set(pub or ()) | set(recv or ()))
    rows = []
    mism = not_pub = 0
    for s in seqs:
        p = pub.get(s) if pub is not None else None
        r = recv.get(s) if recv is not None else None
        t_send = p[0] if p is not None else (r["t_send_us"] if r is not None else None)
        if p is not None and r is not None and r["t_send_us"] is not None and abs(r["t_send_us"] - p[0]) > T_SEND_TOL_US:
            mism += 1
        if pub is not None and p is None:
            not_pub += 1
        rows.append({"seq": s, "t_s": _us(t_send, epoch), "sent": "" if pub is None else int(p is not None),
                     "received": "" if recv is None else int(r is not None),
                     "t_recv_s": _us(r["t_recv_us"], epoch) if r else "",
                     "owd_ms": f"{r['owd_us'] / 1e3:.3f}" if r and r["owd_us"] is not None else "",
                     "ia_ms": "", "probe": int(bool((p and p[1]) or (r and r["probe"]))),
                     "dups": dups.get(s, 0) if r else "", "in_window": int(in_win(t_send)),
                     "transport": (r["transport"] if r else "")})
        rows[-1]["_t_recv"] = r["t_recv_us"] if r else None
    # interarrival: consecutive first arrivals (in receive order) inside the window
    arr = sorted((x["_t_recv"], i) for i, x in enumerate(rows) if x["_t_recv"] is not None and x["in_window"])
    for (t0, _), (t1, i1) in zip(arr, arr[1:]):
        rows[i1]["ia_ms"] = f"{(t1 - t0) / 1e3:.3f}"
    if pub is not None and recv is not None:
        res["recv_not_published"], res["t_send_mismatch"] = not_pub, mism
        if not_pub:
            notes.append(f"control: {not_pub} seq(s) B received are not in A's publisher log")
        if mism:
            notes.append(f"control: {mism} seq(s) carry a different send time at B than in A's log "
                         "(another publisher in the room?)")
    if lo is not None:
        win = [x for x in rows if x["in_window"]]
        if pub is not None:
            res["published"] = sum(1 for x in win if x["sent"] == 1)
            if recv is not None:
                res["received"] = sum(1 for x in win if x["sent"] == 1 and x["received"] == 1)
                if res["published"] and not res["received"]:
                    notes.append("control: B received none of the published control samples")
        elif recv is not None:
            res["received"] = sum(1 for x in win if x["received"] == 1)
    if bad_pub or bad_recv:
        notes.append(f"control: unparsable lines skipped (A log {bad_pub}, B log {bad_recv})")
    res["reason"] = "; ".join(reasons) or None

    if pub is not None or recv is not None:
        with open(out / "control.csv", "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(CONTROL_COLS)
            for x in rows:
                w.writerow([x[c] for c in CONTROL_COLS])
    else:
        (Path(out) / "control.csv").unlink(missing_ok=True)      # never leave an earlier reduction's table
    return res


def write_probes(path: Path, probes: list[tuple[float, float]]) -> None:
    """reduced/probes.csv: one row per probe round trip A's harness reported."""
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(PROBE_COLS)
        for t, rtt in probes:
            w.writerow([f"{t:.3f}", f"{rtt:.3f}"])
