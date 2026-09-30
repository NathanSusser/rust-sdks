"""reduced/* + manifest.json -> metrics.json, exactly the CONTRACT.md schema. Host A only.

    build(cell_dir) -> dict      # also writes <cell>/metrics.json

Every distribution is stats.summ(); absent data gives nulls (Summary with n=0), and no key is
ever omitted. Keys beyond the base schema are listed in CONTRACT.md "added by reduce".
"""
from __future__ import annotations

import json
import math
import re
from pathlib import Path

from .reduce import frames as F, screens
from .reduce.segjoin import SEGMENTS
from .stats import sd, summ

SUMMARY_NULL = summ([])


def _load_json(p: Path):
    try:
        return json.loads(p.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _rows(p: Path) -> list[dict]:
    return F.read_csv(p) if p.exists() else []


def _col(rows, k, sel=None):
    return [r[k] for r in rows if r.get(k) is not None and (sel is None or sel(r))]


def _over(v: list[float], thr: float) -> dict:
    n = sum(1 for x in v if x > thr)
    return {"count": n if v else None, "share": (n / len(v)) if v else None}


def _int(v):
    return None if v is None else int(v)


def _path_mtu(manifest: dict):
    if manifest.get("path_mtu") is not None:
        return manifest["path_mtu"]
    for host in ("a", "b"):
        for g in (manifest.get("gates") or {}).get(host) or []:
            if "mtu" in str(g.get("name", "")).lower():
                m = re.search(r"(\d{3,5})", str(g.get("detail", "")))
                if m:
                    return int(m.group(1))
    return None


def _gates_passed(manifest: dict):
    gs = [g for h in ("a", "b") for g in ((manifest.get("gates") or {}).get(h) or [])]
    if not gs:
        return None
    return all(bool(g.get("pass")) for g in gs)


def _ptp_locked(manifest: dict):
    p = manifest.get("ptp") or {}
    a, b = p.get("a") or {}, p.get("b") or {}
    if not b.get("state"):
        return None
    ok = b.get("state") == "SLAVE" and (b.get("servo_lines_30s") or 0) > 0
    if b.get("offset_ns") is not None:
        ok = ok and abs(b["offset_ns"]) < 10_000
    if a.get("state"):
        ok = ok and a["state"] == "MASTER"
    return ok


def build(cell_dir) -> dict:
    cell = Path(cell_dir)
    manifest = _load_json(cell / "manifest.json") or {}
    red_dir = cell / "reduced"
    red = _load_json(red_dir / "reduce.json") or {}
    fr = _rows(red_dir / "frames.csv")
    secs = _rows(red_dir / "seconds.csv")
    spikes = _rows(red_dir / "spikes.csv")
    eps = _rows(red_dir / "episodes.csv")
    infl = _rows(red_dir / "inflight.csv")
    dur = float(red.get("duration_s") or (manifest.get("variables") or {}).get("duration_s") or 0) or None
    in_cell = (lambda r: 0 <= r["t"] < dur) if dur else (lambda r: True)
    cs = [r for r in secs if in_cell(r)]
    variables = manifest.get("variables") or {}
    enc = red.get("encoder") or {}
    fcount = red.get("frames") or {}
    tot = red.get("totals") or {}
    dlf = red.get("dlf") or {}
    impl = enc.get("implementation") or (manifest.get("negotiated") or {}).get("encoder_implementation")

    # ---------------- config
    clock = manifest.get("clock") or {}
    config = {
        "variables": variables, "requested": manifest.get("requested"), "negotiated": manifest.get("negotiated"),
        "epoch": manifest.get("epoch"), "commit": manifest.get("commit"),
        "clock_offsets": {h: (clock.get(h) or {}).get("host_minus_utc_s") for h in ("a", "b")},
        "ptp": manifest.get("ptp"), "band": manifest.get("band") or {"a": None, "b": None},
        "gates_passed": _gates_passed(manifest),
        "encoder_is_nvenc": (("nvidia" in impl.lower() or "nvenc" in impl.lower()) if impl else None),
        "encoder_implementation": impl, "label": manifest.get("label"), "grid_id": manifest.get("grid_id"),
        "status": manifest.get("status"),
        "screenshots": len(_rows(red_dir / "screens.csv")),
    }

    # ---------------- frame
    sizes = [b / 1000 for b in _col(fr, "bytes")]
    rx = sorted(_col(fr, "receive_us"))
    received = fcount.get("received")
    fps = (len(rx) - 1) / ((rx[-1] - rx[0]) / 1e6) if len(rx) > 1 and rx[-1] > rx[0] else None
    sent = enc.get("frames_sent")
    spread_sd = sd(sizes)
    frame = {
        "size_kb": summ(sizes), "packets_per_frame": summ(_col(fr, "packets_a")), "fps_delivered": fps,
        "frames_captured": fcount.get("captured"),
        "frames_encoded": enc.get("frames_encoded") if enc.get("frames_encoded") is not None else fcount.get("published"),
        "frames_sent": sent, "frames_received": received, "frames_rendered": fcount.get("rendered"),
        "dropped_pre_encode": fcount.get("dropped_pre_encode"),
        "dropped_post_encode": fcount.get("dropped_post_encode"),
        "keyframes": enc.get("key_frames_encoded"),
        "size_spread_pct": (100 * spread_sd / (sum(sizes) / len(sizes))) if spread_sd is not None and sizes else None,
    }

    # ---------------- rate
    # per-second rates over the stream's life in the cell: from the first second a video packet
    # was seen on that wire (the publisher needs ~1 s to start) to the end of the cell
    fa, fb = tot.get("first_video_second_a"), tot.get("first_video_second_b")
    ca = [r for r in cs if fa is not None and r["t"] >= fa]
    cb = [r for r in cs if fb is not None and r["t"] >= fb]
    a_bytes = _col(ca, "a_bytes")
    kbps_ach = (tot.get("a_video_bytes_in_cell") * 8 / dur / 1000) if (tot.get("a_video_bytes_in_cell") is not None and dur) else None
    pad = red.get("padding") or {}
    rate = {
        "bytes_per_s_a": summ(a_bytes), "packets_per_s_a": summ(_col(ca, "a_packets")),
        "bytes_per_s_b": summ(_col(cb, "b_bytes")), "packets_per_s_b": summ(_col(cb, "b_packets")),
        "kbps_target": variables.get("kbps"), "kbps_achieved": kbps_ach,
        "padding_bytes_share": pad.get("share"), "padding_note": pad.get("note"),
    }

    # ---------------- encoder
    ql = enc.get("quality_limitation_s") or {}
    # qp is per second from A's stats (unchanged); qp_per_frame is every frame B's decoder
    # logged (hostb/frames-qp.csv). Absent log (older subscriber) or empty qp (h265) -> n=0.
    qlog = screens.read_qp_log(cell / "hostb" / screens.QP_LOG)
    encoder = {
        "qp": summ(_col(cs, "qp")), "qp_per_frame": summ(qlog.qps() if qlog else []),
        "encode_ms": summ(_col(fr, "encode_ms")),
        "quality_limitation_s": {k: ql.get(k) for k in ("none", "bandwidth", "cpu", "other")},
        "implementation": impl,
    }

    # ---------------- latency / jitter / tail
    owd = _col(fr, "owd")
    e2e = _col(fr, "e2e")
    c2r = _col(fr, "capture_to_receive")
    latency = {k: summ(_col(fr, k)) for k in SEGMENTS}
    latency.update(decode=summ(_col(fr, "decode")), render=summ(_col(fr, "render")), e2e=summ(e2e), owd=summ(owd),
                   capture_to_receive=summ(c2r))
    jitter = {"owd_sd_ms": sd(owd), "interarrival_rfc3550_ms": summ(_col(fr, "jitter_rfc3550")),
              "frame_interval_b_ms": summ(_col(fr, "interval_b"))}
    tail = {
        "owd_over_100": _over(owd, 100), "owd_over_150": _over(owd, 150),
        "e2e_over_100": _over(e2e, 100), "e2e_over_150": _over(e2e, 150),
        "capture_to_receive_over_100": _over(c2r, 100), "capture_to_receive_over_150": _over(c2r, 150),
        "episodes": [{"start_s": e["start_s"], "end_s": e["end_s"], "n": _int(e["n"]), "max_ms": e["max_ms"],
                      "dominant_segment": e["dominant_segment"], "kinds": e.get("kinds")} for e in eps],
        "spike_kinds": {},
    }
    for s in spikes:
        tail["spike_kinds"][s["kind"]] = tail["spike_kinds"].get(s["kind"], 0) + 1

    # ---------------- network
    network = {
        "packets_lost": tot.get("packets_lost"), "loss_events": tot.get("loss_events"),
        "nacks_sfu_to_a": tot.get("nacks_sfu_to_a"), "nacks_b_to_sfu": tot.get("nacks_b_to_sfu"),
        "duplicates_a": tot.get("duplicates_a"), "duplicates_b": tot.get("duplicates_b"),
        "in_flight_packets": summ(_col(infl, "in_flight", (lambda r: r["t"] >= fa) if fa is not None else None)),
        "in_flight_clean": (tot.get("packets_lost") == 0) if tot.get("packets_lost") is not None else None,
        "path_mtu": _path_mtu(manifest),
    }

    # ---------------- modem
    def modem(h):
        d = dlf.get(h) or {}
        present = bool(d.get("present"))
        kq_key = "kq_a_bytes_max" if h == "a" else None
        kq = _col(cs, kq_key) if kq_key else []
        kq_p = _col(cs, "kq_b_pkts_max") if h == "b" else []
        return {
            "activity_index": summ(_col(cs, f"modem_{h}")) if present else summ([]),
            "activity_index_first15": d.get("first15_ratio") if present else None,
            "activity_steady_per_s": d.get("steady_per_s") if present else None,
            "x19ef_records": d.get("x19ef_in_cell") if present else None,
            "kernel_queue_max_bytes": _int(max(kq)) if kq else None,
            "kernel_queue_max_pkts": _int(max(kq_p)) if kq_p else None,
            "rsrp_dbm": summ(_col(cs, f"rsrp_{h}")), "snr_db": summ(_col(cs, f"snr_{h}")),
            "dlf_present": present,
        }
    modem_ = {"a": modem("a"), "b": modem("b")}

    # ---------------- integrity
    missing = list(red.get("missing") or [])
    reasons = [f"missing: {m}" for m in missing]
    if not red:
        reasons.append("reduced/reduce.json absent: reduce has not run")
    integ_m = manifest.get("integrity") or {}
    mirror = integ_m.get("mirror_verified")
    if not mirror:
        reasons.append("mirror not verified" + (" (legacy import)" if integ_m.get("legacy_import") else ""))
    status = manifest.get("status")
    if status and status != "OK":
        reasons.append(f"status {status}: {manifest.get('status_reason', '')}".strip())
    if manifest.get("excluded_from_comparison"):
        reasons.append(f"excluded: {manifest.get('exclusion_reason', '')}".strip())
    gp = config["gates_passed"]
    if gp is None:
        reasons.append("no gate results recorded")
    elif not gp:
        reasons.append("a pre-flight gate failed")
    if config["encoder_is_nvenc"] is False:
        reasons.append(f"encoder is not NVENC: {impl}")
    ptp = _ptp_locked(manifest)
    if ptp is False:
        reasons.append("PTP not locked")
    rs = integ_m.get("reduce_resyncs")
    integrity = {
        "captures_complete": (not missing) if red else False,
        "mirror_verified": bool(mirror),
        "reduce_resyncs": rs if rs is not None else None,
        "ptp_locked": ptp,
        "excluded": bool(manifest.get("excluded_from_comparison")) or (status not in (None, "OK")),
        "reasons": reasons + [f"note: {n}" for n in (red.get("notes") or [])],
    }

    out = {"config": config, "frame": frame, "rate": rate, "encoder": encoder, "latency": latency, "jitter": jitter,
           "tail": tail, "network": network, "modem": modem_, "integrity": integrity}
    (cell / "metrics.json").write_text(json.dumps(_clean(out), indent=1))
    return out


def _clean(o):
    """JSON-safe: NaN/inf -> null."""
    if isinstance(o, float) and not math.isfinite(o):
        return None
    if isinstance(o, dict):
        return {k: _clean(v) for k, v in o.items()}
    if isinstance(o, list):
        return [_clean(v) for v in o]
    return o
