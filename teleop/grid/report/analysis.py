"""comparison/analysis.html: the grid's main analysis page (self-contained: inline CSS, JS, data).

    build(grid_dir, cells, axes) -> str        # the page; report.grid.render writes it

Sections: Overview (what was swept, cell status, PTP, band, encoders, a computed plain-language
line per KPI), KPI curves against bpp (one line per codec x fps = median across repeats, each
repeat a dot, a statistic selector), a radar per codec x fps, the combo x KPI matrix (sortable,
colour-scaled per column, linking every combo's summary.pdf and every repeat's report.pdf), and
every repeat with its flags. Each section has a collapsible "how to read this".

Everything shown comes from each repeat's metrics.json (plus manifest.json): the page never
reads reduced tables, so it renders in well under a second for 60 cells. Medians across repeats
are the nearest-rank median of stats.percentile (the JS reproduces Python's half-to-even
rounding), over the repeats that are OK and not excluded unless the viewer ticks "count
INCOMPLETE repeats".
"""
from __future__ import annotations

import json
import math
import os
import time
from collections import Counter
from pathlib import Path

from .. import stats
from . import html as H
from . import style as S

STATS = ("mean", "p50", "p95", "p99", "max")

# id, metrics path, label, unit, summary|value, better (lower|higher|None), per codec, primary, help
KPIS = [
    ("e2e", "latency.e2e", "Glass-to-glass (e2e)", "ms", "summary", "lower", False, True,
     "Capture on A to GPU-complete on B, per frame (the subscriber's e2e_to_gpu_complete_ms)."),
    ("owd", "latency.owd", "Network", "ms", "summary", "lower", False, True,
     "One direction across the network: A packetize to B webrtc receive, per frame, on PTP-locked host clocks."),
    ("jit_sd", "jitter.owd_sd_ms", "Jitter: network sd", "ms", "value", "lower", False, True,
     "Standard deviation of the per-frame network latency."),
    ("jit_ia", "jitter.interarrival_rfc3550_ms", "Jitter: RFC 3550 interarrival", "ms", "summary", "lower", False,
     True, "RFC 3550 6.4.1 running interarrival jitter, S = packetize on A, R = receive on B."),
    ("qp", "encoder.qp_per_frame", "QP per frame", "QP", "summary", "lower", True, True,
     "The bitstream QP of every decoded frame (B's decoder). H.264/H.265 0-51, AV1 q-index 0-255: "
     "never compared across codecs."),
    ("spread", "frame.size_spread_pct", "Frame size spread (sd/mean)", "%", "value", "lower", False, True,
     "Frame size standard deviation over its mean, A wire."),
    ("fps", "derived.fps_delivered_pct", "Delivered frame rate, % of target", "%", "value", "higher", False, True,
     "frame.fps_delivered (frames reaching webrtc_receive on B per second) / the fps the cell asked for."),
    ("lost", "network.packets_lost", "Packets lost at B", "packets", "value", "lower", False, True,
     "The subscriber's packets_lost (max over the cell)."),
    ("c_owd", "control.owd", "Control path network", "ms", "summary", "lower", False, True,
     "Data-track control samples, A send to B receive, inside the window (publisher span minus 2 s each end)."),
    ("c_del", "control.delivered_pct", "Control path delivered", "%", "value", "higher", False, True,
     "Distinct control seq B received / seq A published, inside the window."),
    ("size", "frame.size_kb", "Frame size", "kB", "summary", None, False, False,
     "Frame size on A's wire. Rises with bpp by design: shown for context, not scored."),
    ("kbps", "rate.kbps_achieved", "Achieved bitrate", "kbps", "value", None, False, False,
     "A-wire video UDP bytes over the cell x 8 / duration."),
    ("fps_abs", "frame.fps_delivered", "Delivered frame rate", "fps", "value", None, False, False,
     "Frames that reached webrtc_receive on B per second (the % of target is the KPI of record)."),
    ("e2e100", "tail.e2e_over_100.share", "Frames over 100 ms e2e", "share", "value", "lower", False, False,
     "Share of frames whose glass-to-glass exceeded 100 ms."),
    ("owd100", "tail.owd_over_100.share", "Frames over 100 ms on the network", "share", "value", "lower", False, False,
     "Share of frames whose network latency exceeded 100 ms."),
    ("c_rtt", "control.rtt", "Control probe round trip", "ms", "summary", "lower", False, False,
     "The harness's probe: A to B and back over the control transport, plus scheduling (about 2x network RTT)."),
    ("c_gap", "control.gaps.max_consecutive_lost", "Control longest gap", "samples", "value", "lower", False, False,
     "Longest run of consecutive published control seq B never received."),
    ("c_ia", "control.interarrival", "Control interarrival at B", "ms", "summary", "lower", False, False,
     "Time between consecutive control samples arriving at B."),
    ("qp_s", "encoder.qp", "QP per second (A stats)", "QP", "summary", "lower", True, False,
     "delta qp_sum / delta frames_encoded per second from A's WebRTC stats. Codec scale as per-frame QP."),
    ("decode", "latency.decode", "Decode on B", "ms", "summary", "lower", False, False, "The subscriber's decode_ms."),
    ("encode", "encoder.encode_ms", "Encode on A", "ms", "summary", "lower", False, False, "The publisher's encode_ms."),
]
KPI_BY_ID = {k[0]: k for k in KPIS}
PRIMARY = [k[0] for k in KPIS if k[7]]

# radar spokes (kpi id, statistic, label) -- all lower-is-better
RADAR = [("e2e", "p50", "e2e p50"), ("e2e", "p99", "e2e p99"), ("owd", "p99", "network p99"),
         ("jit_sd", "value", "jitter sd"), ("qp", "p50", "QP p50"), ("qp", "p99", "QP p99"),
         ("c_owd", "p99", "control network p99")]

QP_SCALE = {"h264": "H.264 QP 0–51", "h265": "H.265 QP 0–51", "av1": "AV1 q-index 0–255"}
FLAG_TEXT = {
    "incomplete": "INCOMPLETE", "skipped": "SKIPPED", "aborted": "ABORTED", "status": "status not OK",
    "excluded": "excluded", "no_metrics": "no metrics.json", "not_nvenc": "not NVENC",
    "ptp_unlocked": "PTP not locked", "ptp_unknown": "PTP not recorded", "codec_fallback": "codec fallback",
    "gates_failed": "gate failed", "no_control": "no control-path data",
}
SERIOUS_FLAGS = {"incomplete", "skipped", "aborted", "status", "excluded", "no_metrics", "not_nvenc",
                 "ptp_unlocked", "codec_fallback"}


# ======================================================================================
# per-repeat facts
# ======================================================================================

def norm_codec(v) -> str | None:
    """'video/H265', 'HEVC', 'h.265' -> 'h265'; None when not recorded."""
    if v is None:
        return None
    s = str(v).strip().lower()
    if not s:
        return None
    s = s.split("/")[-1].replace(".", "").replace("-", "")
    return {"hevc": "h265", "avc": "h264", "av01": "av1"}.get(s, s)


def derived_value(c, path: str):
    """KPIs computed from metrics.json + the cell's variables (never written to metrics.csv):
    derived.fps_delivered_pct = 100 x frame.fps_delivered / variables.fps."""
    if c is None:
        return None
    if path == "derived.fps_delivered_pct":
        fps, want = c.value("frame.fps_delivered", "value"), c.variables.get("fps")
        if S.is_num(fps) and isinstance(want, (int, float)) and not isinstance(want, bool) and want > 0:
            return 100.0 * fps / want
    return None


DERIVED_PATHS = ("derived.fps_delivered_pct",)


def _mx(c) -> dict:
    """The repeat's flattened metrics (5 significant digits) plus the derived KPIs."""
    mx = {p: {k: _round(v) for k, v in sts.items() if S.is_num(v)} for p, sts in c.flat.items()}
    for p in DERIVED_PATHS:
        v = derived_value(c, p)
        if v is not None:
            mx[p] = {"value": _round(v)}
    return mx


def repeat_flags(c) -> list[str]:
    """Machine flags for one repeat (a report.grid.GridCell)."""
    f = []
    st = c.status
    if st == "INCOMPLETE":
        f.append("incomplete")
    elif st == "SKIPPED":
        f.append("skipped")
    elif st == "ABORTED":
        f.append("aborted")
    elif st != "OK":
        f.append("status")
    if c.metrics is None:
        f.append("no_metrics")
    elif c.excluded and st == "OK":
        f.append("excluded")
    if c.metrics is not None or c.cfg("negotiated.encoder_implementation"):
        if not c.nvenc:
            f.append("not_nvenc")
    ptp = ((c.metrics or {}).get("integrity") or {}).get("ptp_locked")
    if ptp is False:
        f.append("ptp_unlocked")
    elif ptp is None and c.metrics is not None:
        f.append("ptp_unknown")
    req = norm_codec(c.variables.get("codec"))
    neg = norm_codec(c.cfg("negotiated.codec"))
    if req and neg and req != neg:
        f.append("codec_fallback")
    _, failed = c.gates()
    if failed:
        f.append("gates_failed")
    ctl = (c.metrics or {}).get("control")
    if c.metrics is not None and (not isinstance(ctl, dict) or not (ctl.get("owd") or {}).get("n")):
        f.append("no_control")
    return f


def _round(v):
    """5 significant digits: the page shows at most 4, and the data block stays small."""
    if not S.is_num(v):
        return None
    if v == 0 or isinstance(v, int):
        return v
    return float(f"{v:.5g}")


def _var_fmt(axis: str, v) -> str:
    if v is None or v == "":
        return "–"
    if axis == "fps":
        return f"{v} fps"
    if axis == "bpp":
        return f"bpp {v:g}" if isinstance(v, (int, float)) else f"bpp {v}"
    if axis == "kbps":
        return f"{v} kbps"
    if axis == "codec":
        return str(v)
    return f"{axis} {v}"


def _rel(target: Path, out_dir: Path) -> str:
    return os.path.relpath(target, out_dir).replace(os.sep, "/")


# ======================================================================================
# the model embedded in the page
# ======================================================================================

def _combos(cells, axes) -> list[dict]:
    """Group repeats by combination: the <combo>/ directory in layout v2, the swept values otherwise."""
    groups: dict[str, dict] = {}
    for c in cells:
        if c.kind == "control":
            continue
        key = c.combo or " · ".join(_var_fmt(a, c.variables.get(a)) for a in axes) or "all"
        g = groups.setdefault(key, {"name": key, "dir": c.dir.parent if c.combo else None, "cells": []})
        g["cells"].append(c)
    return list(groups.values())


def _norm(v):
    """Axis values as the page compares them: booleans as 'True'/'False' (JSON keeps the rest)."""
    return str(v) if isinstance(v, bool) else v


def _value_order(vals):
    vals = list(dict.fromkeys(_norm(v) for v in vals if v is not None))
    if all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in vals):
        return sorted(vals)
    return sorted(vals, key=str)


def build_model(grid_dir: Path, cells, axes: list[str], plan: dict | None = None) -> dict:
    gd = Path(grid_dir)
    out_dir = gd / "comparison"
    groups = _combos(cells, axes)
    values = {a: _value_order([c.variables.get(a) for g in groups for c in g["cells"]]) for a in axes}
    x = "bpp" if "bpp" in axes else next((a for a in axes if values[a] and all(
        isinstance(v, (int, float)) and not isinstance(v, bool) for v in values[a])), None)
    if x is None and not axes:
        bp = _value_order([c.variables.get("bpp") for c in cells if c.kind != "control"])
        if bp and all(isinstance(v, (int, float)) for v in bp):
            x, values = "bpp", {"bpp": bp}
    series_axes = [a for a in axes if a != x]
    codec_axis = "codec" if ("codec" in axes or any(c.variables.get("codec") for c in cells)) else None

    # QP source per codec: per-frame QP when any repeat of that codec has it, else per-second
    qp_path: dict[str, str] = {}
    by_codec: dict[str, list] = {}
    for c in cells:
        if c.kind != "control" and c.metrics is not None:
            by_codec.setdefault(str(c.variables.get("codec") or ""), []).append(c)
    for codec, cs in by_codec.items():
        if any(c.value("encoder.qp_per_frame", "n") for c in cs):
            qp_path[codec] = "encoder.qp_per_frame"
        elif any(c.value("encoder.qp", "n") for c in cs):
            qp_path[codec] = "encoder.qp"

    def repeat_entry(c) -> dict:
        flags = repeat_flags(c)
        pdf, html = c.dir / "report.pdf", c.dir / "report.html"
        return {
            "n": c.repeat, "label": c.label, "status": c.status, "has": c.metrics is not None,
            "included": c.metrics is not None and not c.excluded and c.status == "OK",
            "flags": flags, "reasons": c.reasons()[:8], "encoder": c.encoder,
            "negotiated_codec": c.cfg("negotiated.codec"),
            "report_pdf": _rel(pdf, out_dir) if pdf.is_file() else None,
            "report_html": _rel(html, out_dir) if html.is_file() else None,
            "mx": _mx(c),
        }

    combos = []
    for g in groups:
        cs = sorted(g["cells"], key=lambda c: (c.repeat if isinstance(c.repeat, int) else 0, c.label))
        v = {a: cs[0].variables.get(a) for a in axes} if cs else {}
        if x and x not in v and cs:
            v[x] = cs[0].variables.get(x)
        d = g["dir"]
        spdf = d / "summary.pdf" if d is not None else None
        shtml = d / "summary.html" if d is not None else None
        combos.append({
            "name": g["name"], "vars": {k: _norm(val) for k, val in v.items()},
            "label": " · ".join(_var_fmt(a, v.get(a)) for a in axes) or g["name"],
            "codec": str(cs[0].variables.get("codec") or "") if cs else "",
            "summary_pdf": _rel(spdf, out_dir) if spdf is not None and spdf.is_file() else None,
            "summary_html": _rel(shtml, out_dir) if shtml is not None and shtml.is_file() else None,
            "repeats": [repeat_entry(c) for c in cs],
            "missing": [n for n in range(1, int((plan or {}).get("repeats") or 0) + 1)
                        if n not in {c.repeat for c in cs}],
        })

    def combo_key(cb):
        return tuple((values.get(a) or []).index(cb["vars"].get(a)) if cb["vars"].get(a) in (values.get(a) or [])
                     else 999 for a in axes) + (cb["name"],)
    combos.sort(key=combo_key)

    controls = []
    for c in cells:
        if c.kind == "control":
            e = repeat_entry(c)
            e["name"] = c.dir.name
            controls.append(e)

    kpis = [{"id": k[0], "path": k[1], "label": k[2], "unit": k[3], "kind": k[4], "better": k[5],
             "per_codec": k[6], "primary": k[7], "help": k[8]} for k in KPIS]
    all_paths = sorted({p for c in cells for p in c.flat})
    path_kind = {}
    for c in cells:
        for p, sts in c.flat.items():
            path_kind.setdefault(p, "summary" if "p50" in sts else "value")
    model = {
        "grid_id": next((str(c.manifest.get("grid_id")) for c in cells if c.manifest.get("grid_id")), gd.name),
        "generated": time.strftime("%Y-%m-%d %H:%M:%S %Z"),
        "axes": axes, "x": x, "series_axes": series_axes, "values": {a: values.get(a, []) for a in axes + ([x] if x and x not in axes else [])},
        "codec_axis": codec_axis, "qp_path": qp_path, "qp_scale": QP_SCALE,
        "kpis": kpis, "radar": [{"kpi": k, "stat": s, "label": lab} for k, s, lab in RADAR],
        "stats": list(STATS), "flag_text": FLAG_TEXT, "serious_flags": sorted(SERIOUS_FLAGS),
        "combos": combos, "controls": controls,
        "paths": [{"path": p, "name": S.path_name(p), "kind": path_kind[p]} for p in all_paths],
        "plan": plan or {},
    }
    model["overview"] = overview(model)
    return model


# ======================================================================================
# computed plain-language summaries (Python, so they are testable and fixed per render)
# ======================================================================================

def _median(vals):
    v = [x for x in vals if S.is_num(x)]
    return stats.summ(v)["p50"] if v else None


def kpi_value(model: dict, rep: dict, kpi: dict, stat: str, codec: str = ""):
    """One repeat's value; QP per frame falls back to per-second QP for a codec whose decoder
    gave no per-frame QP (model["qp_path"])."""
    path = model["qp_path"].get(codec, kpi["path"]) if kpi["id"] == "qp" else kpi["path"]
    m = rep["mx"].get(path) or {}
    v = m.get(stat if kpi["kind"] == "summary" else "value")
    return v if S.is_num(v) else None


def combo_value(model: dict, combo: dict, kpi: dict, stat: str, include_all: bool = False):
    """Median across the combination's counted repeats (all repeats with metrics if include_all)."""
    return _median([kpi_value(model, r, kpi, stat, combo["codec"]) for r in combo["repeats"]
                    if r["has"] and (include_all or r["included"])])


def _fmt(v, unit: str) -> str:
    if not S.is_num(v):
        return "–"
    if unit == "share":
        return f"{100 * v:.2f}%"
    if unit == "%":
        return f"{v:.2f}%" if abs(v) < 1000 else f"{v:,.0f}%"
    s = S.fmt(v)
    return f"{s} {unit}" if unit not in ("", "QP") else (f"QP {s}" if unit == "QP" else s)


def _diff(a, b, unit: str) -> str:
    d = b - a
    sign = "+" if d >= 0 else "−"
    if unit == "share":
        return f"{sign}{abs(100 * d):.2f} pp"
    if unit == "%":
        return f"{sign}{abs(d):.2f} pp"
    return f"{sign}{S.fmt(abs(d))}" + (f" {unit}" if unit not in ("", "QP") else "")


def overview(model: dict) -> dict:
    """Status counts, PTP, band, encoders, and one computed sentence set per primary KPI."""
    reps = [r for cb in model["combos"] for r in cb["repeats"]]
    st = Counter(r["status"] for r in reps)
    flags = Counter(f for r in reps for f in r["flags"])
    ptp_locked = sum(1 for r in reps if r["has"] and "ptp_unlocked" not in r["flags"] and "ptp_unknown" not in r["flags"])
    enc: dict[str, Counter] = {}
    for cb in model["combos"]:
        for r in cb["repeats"]:
            if r["has"] or r["encoder"] != "unknown":
                enc.setdefault(cb["codec"] or "?", Counter())[r["encoder"]] += 1
    lines = []
    for kid in PRIMARY:
        k = next(x for x in model["kpis"] if x["id"] == kid)
        lines.append(kpi_sentences(model, k))
    return {"repeats": len(reps), "status": dict(st), "flags": dict(flags), "ptp_locked": ptp_locked,
            "with_metrics": sum(1 for r in reps if r["has"]),
            "encoders": {c: dict(v) for c, v in enc.items()}, "kpi_lines": lines}


def _series_of(model, cb):
    return tuple(cb["vars"].get(a) for a in model["series_axes"])


def kpi_sentences(model: dict, k: dict) -> dict:
    """{id, label, stat, text: [sentences], empty: bool}. p99 for a Summary KPI, the value otherwise."""
    stat = "p99" if k["kind"] == "summary" else "value"
    head = f"{k['label']}, {stat} (median of repeats)"
    groups = [None]
    if k["per_codec"] and model["codec_axis"]:
        groups = _value_order([cb["codec"] for cb in model["combos"]])
    out = []
    empty = True
    for grp in groups:
        cbs = [cb for cb in model["combos"] if grp is None or cb["codec"] == grp]
        vals = [(cb, combo_value(model, cb, k, stat)) for cb in cbs]
        vals = [(cb, v) for cb, v in vals if v is not None]
        prefix = ""
        if grp is not None:
            src = model["qp_path"].get(grp)
            scale = QP_SCALE.get(grp, "")
            prefix = f"{grp} ({scale}{', per-second QP from A: no per-frame QP' if src == 'encoder.qp' and k['id'] == 'qp' else ''}): "
        if not vals:
            out.append(prefix + "no data in any included repeat.")
            continue
        empty = False
        if len(vals) == 1:
            out.append(prefix + f"one setting only: {vals[0][0]['label']} = {_fmt(vals[0][1], k['unit'])}.")
            continue
        better = k["better"]
        key = (lambda t: t[1]) if better != "higher" else (lambda t: -t[1])
        best, worst = min(vals, key=key), max(vals, key=key)
        ratio = (worst[1] / best[1]) if best[1] and better == "lower" and best[1] > 0 else None
        out.append(prefix + f"best {best[0]['label']} = {_fmt(best[1], k['unit'])}; worst {worst[0]['label']} = "
                   f"{_fmt(worst[1], k['unit'])} ({_diff(best[1], worst[1], k['unit'])}"
                   + (f", {ratio:.2f}×" if ratio and math.isfinite(ratio) else "") + ").")
        # effect of the x axis (bpp): lowest -> highest per series
        x = model["x"]
        if x and len(model["values"].get(x, [])) > 1:
            xs = model["values"][x]
            deltas = []
            for s in sorted({_series_of(model, cb) for cb, _ in vals}, key=str):
                pts = {cb["vars"].get(x): v for cb, v in vals if _series_of(model, cb) == s}
                have = [xv for xv in xs if xv in pts]
                if len(have) >= 2:
                    deltas.append((s, have[0], have[-1], pts[have[-1]] - pts[have[0]]))
            if deltas:
                lo_d = min(deltas, key=lambda d: d[3])
                hi_d = max(deltas, key=lambda d: d[3])
                x0, x1 = deltas[0][1], deltas[0][2]
                if lo_d is hi_d or abs(lo_d[3] - hi_d[3]) < 1e-12:
                    rng = _diff(0, lo_d[3], k["unit"])
                else:
                    rng = f"{_diff(0, lo_d[3], k['unit'])} to {_diff(0, hi_d[3], k['unit'])}"
                out.append(prefix + f"{x} {x0:g} → {x1:g}: {rng}"
                           + (f" across {len(deltas)} {' × '.join(model['series_axes'])} lines" if model['series_axes']
                              else "") + ".")
        # two-valued axes: paired difference over matched settings of every other axis
        for a in model["series_axes"]:
            if k["per_codec"] and a == model["codec_axis"]:
                continue
            av = [v for v in model["values"].get(a, []) if any(cb["vars"].get(a) == v for cb, _ in vals)]
            if len(av) != 2:
                continue
            others = [b for b in model["axes"] if b != a]
            pa = {tuple(cb["vars"].get(b) for b in others): v for cb, v in vals if cb["vars"].get(a) == av[0]}
            pb = {tuple(cb["vars"].get(b) for b in others): v for cb, v in vals if cb["vars"].get(a) == av[1]}
            both = [key for key in pa if key in pb]
            if not both:
                continue
            d = [pb[key] - pa[key] for key in both]
            md = _median(d)
            good = sum(1 for x_ in d if (x_ < 0 if better != "higher" else x_ > 0))
            out.append(prefix + f"{_var_fmt(a, av[1])} vs {_var_fmt(a, av[0])}: median {_diff(0, md, k['unit'])} over "
                       f"{len(both)} matched setting{'s' if len(both) != 1 else ''}"
                       + (f" ({_var_fmt(a, av[1])} better in {good} of {len(both)})" if better else "") + ".")
    return {"id": k["id"], "label": k["label"], "head": head, "text": out, "empty": empty}


# ======================================================================================
# html
# ======================================================================================

def _tile(label: str, value: str, sub: str = "", cls: str = "") -> str:
    return (f'<div class="tile {cls}"><div class="tl">{H.esc(label)}</div><div class="tv">{H.esc(value)}</div>'
            + (f'<div class="ts">{sub}</div>' if sub else "") + "</div>")


def _how(text: str) -> str:
    return f'<details class="how"><summary>How to read this</summary><div>{text}</div></details>'


def overview_html(model: dict, cells) -> str:
    ov = model["overview"]
    st = ov["status"]
    plan = model.get("plan") or {}
    planned = plan.get("cells")
    n_rep = ov["repeats"]
    status_bits = " · ".join(f"{k} {v}" for k, v in sorted(st.items())) or "none"
    tiles = [
        _tile("Repeats present", f"{n_rep}" + (f" of {planned}" if planned else ""),
              H.esc(status_bits) + (f" · {max(0, planned - n_rep)} not run yet" if planned else "")),
        _tile("OK and counted", f"{sum(1 for cb in model['combos'] for r in cb['repeats'] if r['included'])}",
              "status OK, not excluded, metrics.json present"),
        _tile("PTP locked", f"{ov['ptp_locked']} of {ov['with_metrics']}",
              "repeats with metrics" + (f" · <b class='bad'>{ov['flags'].get('ptp_unlocked', 0)} NOT locked</b>"
                                        if ov['flags'].get('ptp_unlocked') else ""),
              "warn" if ov["flags"].get("ptp_unlocked") else ""),
        _tile("Combinations", f"{len(model['combos'])}",
              H.esc(" × ".join(f"{a} {len(model['values'].get(a, []))}" for a in model["axes"]) or "single combination")),
    ]
    flag_items = [f"{H.esc(FLAG_TEXT.get(f, f))} {n}" for f, n in sorted(ov["flags"].items()) if f in SERIOUS_FLAGS]
    if flag_items:
        tiles.append(_tile("Flagged repeats", str(sum(1 for cb in model["combos"] for r in cb["repeats"]
                                                        if set(r["flags"]) & SERIOUS_FLAGS)),
                           " · ".join(flag_items), "warn"))
    swept = "".join(
        f"<tr><td>{H.esc(a)}</td><td>{H.esc(', '.join(_var_fmt(a, v) for v in model['values'].get(a, [])))}</td></tr>"
        for a in model["axes"]) or "<tr><td colspan=2>no axes: a single combination</td></tr>"
    const, derived = [], []
    fixed: dict[str, set] = {}
    for c in cells:
        if c.kind == "control":
            continue
        for k, v in c.variables.items():
            if k in model["axes"]:
                continue
            fixed.setdefault(k, set()).add(json.dumps(v, default=str))
    for k in sorted(fixed):
        vs = [json.loads(x) for x in fixed[k]]
        if len(vs) == 1:
            v = vs[0]
            const.append(f"{k}={Path(str(v)).name if k == 'clip' else v}")
        else:
            nums = [v for v in vs if isinstance(v, (int, float)) and not isinstance(v, bool)]
            derived.append(f"{k} {min(nums):g}–{max(nums):g}" if len(nums) == len(vs) else f"{k} ({len(vs)} values)")
    enc_rows = []
    for codec, e in sorted(ov["encoders"].items()):
        parts = []
        for name, n in sorted(e.items(), key=lambda t: -t[1]):
            nv = "nvenc" in name.lower() or "nvidia" in name.lower()
            parts.append(f"<span class='{'' if nv else 'bad'}'>{H.esc(name)} ×{n}{'' if nv else ' (not NVENC)'}</span>")
        enc_rows.append(f"<tr><td>{H.esc(codec or '?')}</td><td>{', '.join(parts)}</td></tr>")
    bands = {"a": Counter(), "b": Counter()}
    for c in cells:
        if c.kind == "control":
            continue
        for h in ("a", "b"):
            b = c.cfg(f"band.{h}") or {}
            if b:
                bands[h][f"{b.get('band', '?')} · ARFCN {b.get('arfcn', '?')} · PCI {b.get('pci', '?')}"] += 1
            elif c.metrics is not None:
                bands[h]["not recorded"] += 1
    band_rows = "".join(
        f"<tr><td>Host {h.upper()}</td><td>{', '.join(f'{H.esc(k)} ×{n}' for k, n in bands[h].most_common()) or '–'}</td></tr>"
        for h in ("a", "b"))
    lines = []
    for ln in ov["kpi_lines"]:
        items = "".join(f"<li>{H.esc(t)}</li>" for t in ln["text"])
        lines.append(f'<div class="kline{" empty" if ln["empty"] else ""}"><div class="kh">{H.esc(ln["head"])}</div>'
                     f"<ul>{items}</ul></div>")
    ctl = ""
    if model["controls"]:
        rows = []
        for e in model["controls"]:
            p99 = (e["mx"].get("latency.owd") or {}).get("p99")
            lost = (e["mx"].get("network.packets_lost") or {}).get("value")
            lab = (f'<a href="{H.esc(e["report_pdf"])}">{H.esc(e["name"])}</a>' if e["report_pdf"] else H.esc(e["name"]))
            rows.append(f"<tr><td>{lab}</td><td>{H.esc(e['status'])}</td><td class='num'>{H.esc(S.fmt(p99))}</td>"
                        f"<td class='num'>{H.esc(S.fmt(lost))}</td></tr>")
        ctl = ("<h3>Control cells</h3><table class='t'><thead><tr><th>cell</th><th>status</th>"
               "<th class='num'>network p99 ms</th><th class='num'>packets lost</th></tr></thead><tbody>"
               + "".join(rows) + "</tbody></table>")
    return f"""
<section id="overview"><h2>Overview</h2>
{_how("Tiles count the repeat directories found under the grid (layout v2: &lt;combo&gt;/r&lt;n&gt;/). "
      "A repeat is <b>counted</b> in every median on this page only when its status is OK, it is not excluded and it "
      "has metrics.json; the toolbar's <i>count INCOMPLETE repeats</i> adds the others to the medians. PTP, band and "
      "encoder are read from each repeat's manifest and metrics. The KPI lines below are <b>computed</b> from the data "
      "at p99 (or the value, for scalar KPIs): best and worst combination, the change from the lowest to the highest "
      "bpp along each codec × fps line, and paired differences for two-valued axes over matched settings. They state "
      "numbers, not causes.")}
<div class="tiles">{''.join(tiles)}</div>
<div class="cols">
  <div><h3>What was swept</h3><table class="t kv"><tbody>{swept}</tbody></table>
  <p class="note">fixed: {H.esc(', '.join(const) or '–')}</p>
  {f'<p class="note">follow from the axes: {H.esc(", ".join(derived))}</p>' if derived else ''}</div>
  <div><h3>Encoder per codec</h3><table class="t kv"><tbody>{''.join(enc_rows) or '<tr><td>–</td></tr>'}</tbody></table>
  <h3>Serving band</h3><table class="t kv"><tbody>{band_rows}</tbody></table></div>
</div>
<h3>KPIs at p99 <span class="badge">computed from metrics.json</span></h3>
<div class="klines">{''.join(lines)}</div>
{ctl}
</section>"""


def build(grid_dir, cells, axes, plan: dict | None = None) -> str:
    model = build_model(Path(grid_dir), cells, axes, plan)
    n_rep = model["overview"]["repeats"]
    sub = (f"{n_rep} repeat{'s' if n_rep != 1 else ''} in {len(model['combos'])} combination"
           f"{'s' if len(model['combos']) != 1 else ''} · axes: {', '.join(axes) or 'none'} · generated {model['generated']}")
    body = TOOLBAR + overview_html(model, cells) + SECTIONS + '<div id="tip" role="tooltip"></div>'
    return H.page(f"Grid {model['grid_id']} — analysis", sub, body, extra_css=CSS,
                  script=H.json_script(model, "analysis-data") + f"<script>{JS}</script>")


TOOLBAR = """
<nav class="bar" aria-label="sections and controls">
  <div class="links"><a href="#overview">Overview</a><a href="#curves">KPI curves</a><a href="#radar">Radar</a>
    <a href="#matrix">Matrix</a><a href="#repeats">Repeats</a><a href="comparison.html">comparison.html</a></div>
  <div class="ctl">
    <label>statistic <select id="stat"></select></label>
    <label class="chk"><input type="checkbox" id="incl"> count INCOMPLETE repeats</label>
    <label>theme <select id="theme"><option value="">auto</option><option value="light">light</option>
      <option value="dark">dark</option></select></label>
  </div>
</nav>
"""

SECTIONS = """
<section id="curves"><h2>KPI curves <span id="xname"></span></h2>
<details class="how"><summary>How to read this</summary><div>
One chart per KPI of record. The x axis is the swept bpp (other variables fixed per line); the y axis starts at
zero except for the "% delivered" and "% of target" KPIs, which zoom on the shortfall from 100%. Each <b>line</b> is one
codec × fps combination and joins the <b>median across that combination's counted repeats</b> at each bpp (the
larger marker); every <b>small dot</b> is one repeat, hollow when it is not counted (INCOMPLETE, SKIPPED or excluded).
Hue is the codec, marker shape and dash the frame rate. The <b>statistic</b> selector in the bar above chooses
which of each repeat's per-frame summary is plotted (mean, p50, p95, p99, max); scalar KPIs (jitter sd, spread,
fps, packets lost, delivered %) have one value per repeat and ignore it. QP is drawn per codec, never on one axis:
H.264/H.265 QP is 0–51 and AV1's q-index 0–255. Hover a chart for every line's value at the nearest bpp; click
near a dot to open that repeat's report.pdf. Lower is better unless the subtitle says otherwise.
</div></details>
<div id="legend" class="legend"></div>
<div id="charts" class="charts"></div>
<details class="more"><summary>More KPIs (frame size, bitrate, tails, control round trip, codec timing)</summary>
<div id="charts-more" class="charts"></div></details>
<details class="more"><summary>Explore any metric in metrics.json</summary>
<div class="ctl-row"><label>metric <select id="xp-path"></select></label>
<label>statistic <select id="xp-stat"></select></label></div>
<div id="charts-explore" class="charts"></div></details>
</section>

<section id="radar"><h2>Radar: one polygon per bpp</h2>
<details class="how"><summary>How to read this</summary><div>
Pick one codec × fps line. Each polygon is one bpp (darker = higher bpp), its vertices the median across counted
repeats of seven lower-is-better KPIs. Every spoke is scaled on its own to the values shown: in <b>range</b>
scaling the worst polygon on that spoke touches the outer ring and the best sits at the centre ring, which
exaggerates small differences; <b>ratio</b> scaling puts 0 at the centre and the worst value at the ring, so a
spoke's length is proportional to its value. The table under the radar has the numbers. A smaller polygon is
better; a missing KPI leaves the outline open at that spoke.
</div></details>
<div class="ctl-row"><label>line <select id="radar-series"></select></label>
<label>scaling <select id="radar-scale"><option value="range">range: best at centre, worst at edge</option>
<option value="ratio">ratio: 0 at centre, worst at edge</option></select></label></div>
<div class="radar-wrap"><div id="radar-svg"></div><div id="radar-legend" class="legend col"></div></div>
<div id="radar-table"></div>
</section>

<section id="matrix"><h2>Matrix: every combination × KPI</h2>
<details class="how"><summary>How to read this</summary><div>
One row per combination; each cell is the median across the counted repeats of the selected statistic (the value,
for scalar KPIs). Tint marks the worse end within each column (darker = worse; QP within each codec; frame size
and bitrate are context and untinted), at full strength only where the column's worst is at least 20% off its best
(for delivered % and % of target: 20% more shortfall from 100); a column that barely varies stays pale. Click a header to sort. The combination links to its summary.pdf (all its
repeats side by side); r1, r2, r3 link to each repeat's report.pdf; a warning sign marks a repeat with a flag
(hover it for the reason).
</div></details>
<div class="ctl-row"><label class="chk"><input type="checkbox" id="mx-more"> more columns</label>
<span class="scale-key" id="mx-key"></span></div>
<div id="mx" class="scroll"></div>
</section>

<section id="repeats"><h2>Every repeat</h2>
<details class="how"><summary>How to read this</summary><div>
Each repeat directory found, with its status and flags: <b>not NVENC</b> (the encoder that ran is not NVIDIA's),
<b>PTP not locked</b> (cross-host network numbers are not trustworthy), <b>codec fallback</b> (the negotiated codec
differs from the requested one), <b>INCOMPLETE</b>/<b>SKIPPED</b> (not counted in medians). A combination whose
repeat directory does not exist yet is simply not listed.
</div></details>
<div id="rep-table" class="scroll"></div>
</section>
"""

CSS = """
nav.bar { position: sticky; top: 0; z-index: 4; display: flex; flex-wrap: wrap; gap: 8px 16px; align-items: center;
  justify-content: space-between; background: var(--surface); border-bottom: 1px solid var(--grid); padding: 8px 16px; }
nav.bar .links a { margin-right: 12px; text-decoration: none; font-weight: 600; font-size: 13px; }
nav.bar .ctl, .ctl-row { display: flex; flex-wrap: wrap; gap: 8px 16px; align-items: center; font-size: 12px; color: var(--ink2); }
.ctl-row { margin: 8px 0 12px; }
select { font: inherit; padding: 3px 6px; background: var(--surface); color: var(--ink); border: 1px solid var(--base);
  border-radius: 4px; max-width: 320px; }
label.chk { display: inline-flex; gap: 4px; align-items: center; }
section { scroll-margin-top: 56px; }
h2 .sub, #xname { color: var(--ink2); font-weight: 400; font-size: 13px; }
details.how { margin: 0 0 12px; font-size: 12.5px; color: var(--ink2); }
details.how summary { cursor: pointer; color: var(--s1); font-weight: 600; }
details.how > div { margin-top: 6px; max-width: 900px; }
details.more { margin-top: 16px; }
details.more summary { cursor: pointer; font-weight: 600; }
.badge { font-size: 11px; font-weight: 600; color: var(--ink2); border: 1px solid var(--base); border-radius: 10px;
  padding: 1px 8px; vertical-align: middle; }
.tiles { display: grid; grid-template-columns: repeat(auto-fill, minmax(190px, 1fr)); gap: 10px; margin: 8px 0 12px; }
.tile { border: 1px solid var(--grid); border-radius: 6px; padding: 10px 12px; background: var(--page); }
.tile.warn { border-left: 4px solid var(--warn); }
.tile .tl { font-size: 12px; color: var(--ink2); }
.tile .tv { font-size: 24px; font-weight: 600; margin: 2px 0; }
.tile .ts { font-size: 11.5px; color: var(--ink2); }
.cols { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 8px 24px; }
.klines { display: grid; grid-template-columns: repeat(auto-fill, minmax(420px, 1fr)); gap: 8px 16px; }
.kline { border-left: 3px solid var(--base); padding: 2px 0 2px 10px; }
.kline.empty { opacity: .7; }
.kline .kh { font-weight: 600; font-size: 13px; }
.kline ul { margin: 2px 0 0; padding-left: 18px; font-size: 12.5px; }
.legend { display: flex; flex-wrap: wrap; gap: 6px 18px; font-size: 12px; color: var(--ink2); margin: 4px 0 10px; }
.legend.col { flex-direction: column; }
.legend .item { display: inline-flex; align-items: center; gap: 6px; }
.charts { display: grid; grid-template-columns: repeat(auto-fill, minmax(380px, 1fr)); gap: 12px; }
.card { border: 1px solid var(--grid); border-radius: 6px; padding: 8px 10px 4px; background: var(--surface); min-width: 0; }
.card h4 { margin: 0; font-size: 13px; }
.card .cs { font-size: 11.5px; color: var(--ink2); margin: 1px 0 4px; }
.card .facets { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 4px; }
.card .ft { font-size: 11.5px; color: var(--ink2); text-align: center; margin-top: 2px; }
.card.wide { grid-column: span 2; }
@media (max-width: 820px) { .card.wide { grid-column: auto; } }
svg.ch { width: 100%; height: auto; display: block; overflow: visible; touch-action: none; }
svg.ch text { fill: var(--muted); font-size: 10px; font-variant-numeric: tabular-nums; }
svg.ch text.lab { fill: var(--ink2); font-size: 10px; }
svg.ch .gl { stroke: var(--grid); stroke-width: 1; }
svg.ch .ax { stroke: var(--base); stroke-width: 1; }
svg.ch .xh { stroke: var(--ink2); stroke-width: 1; opacity: .6; }
svg.ch .nodata { fill: var(--muted); font-size: 12px; }
#tip { position: fixed; pointer-events: none; background: var(--surface); color: var(--ink); border: 1px solid var(--base);
  border-radius: 6px; padding: 8px 10px; font-size: 12px; box-shadow: 0 2px 10px rgba(0,0,0,.18); display: none;
  font-variant-numeric: tabular-nums; max-width: 420px; z-index: 10; }
#tip .th { font-weight: 600; margin-bottom: 4px; color: var(--ink2); }
#tip .tr { display: grid; grid-template-columns: 26px 1fr auto; gap: 2px 6px; align-items: center; }
#tip .tv { font-weight: 700; text-align: right; }
#tip .tn { grid-column: 2 / 4; color: var(--ink2); font-size: 11px; margin-bottom: 3px; }
.radar-wrap { display: flex; flex-wrap: wrap; gap: 16px; align-items: flex-start; }
#radar-svg { flex: 1 1 420px; max-width: 560px; }
#radar-svg svg text { fill: var(--ink2); font-size: 11px; }
#radar-svg svg .ring { fill: none; stroke: var(--grid); }
#radar-svg svg .spoke { stroke: var(--base); }
.scroll { overflow-x: auto; }
table.mx { border-collapse: separate; border-spacing: 0; font-size: 12px; font-variant-numeric: tabular-nums; }
table.mx th, table.mx td { padding: 4px 8px; border-bottom: 1px solid var(--grid); white-space: nowrap; }
table.mx th { position: sticky; top: 0; background: var(--surface); color: var(--ink2); font-weight: 600;
  cursor: pointer; user-select: none; vertical-align: bottom; text-align: right; white-space: normal;
  min-width: 64px; max-width: 118px; line-height: 1.25; }
table.mx th.l, table.mx td.l { text-align: left; }
table.mx th .u { display: block; font-weight: 400; font-size: 11px; color: var(--muted); }
table.mx th[aria-sort="ascending"]::after { content: " ▲"; font-size: 9px; }
table.mx th[aria-sort="descending"]::after { content: " ▼"; font-size: 9px; }
table.mx td.v { text-align: right; }
table.mx td.v.hot { color: #fff; }
.reps a, .reps span { margin-right: 6px; }
.flag { color: var(--crit); font-weight: 700; cursor: help; }
.scale-key { display: inline-flex; align-items: center; gap: 6px; }
.scale-key i { display: inline-block; width: 90px; height: 10px; border-radius: 2px;
  background: linear-gradient(90deg, rgba(42,120,214,0), rgba(42,120,214,.62)); }
.muted { color: var(--muted); }
"""

JS = r"""
(function () {
'use strict';
const D = JSON.parse(document.getElementById('analysis-data').textContent);
const $ = (id) => document.getElementById(id);
const SVGNS = 'http://www.w3.org/2000/svg';
const isNum = (v) => typeof v === 'number' && isFinite(v);
function h(tag, attrs, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === 'class') e.className = v; else if (k === 'text') e.textContent = v; else e.setAttribute(k, v);
  }
  for (const k of kids) if (k != null) e.append(k instanceof Node ? k : document.createTextNode(String(k)));
  return e;
}
function s(tag, attrs, parent) {
  const e = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs || {})) if (v != null) e.setAttribute(k, v);
  if (parent) parent.appendChild(e);
  return e;
}
function stxt(parent, x, y, str, attrs) {
  const t = s('text', Object.assign({x, y}, attrs || {}), parent); t.textContent = str; return t;
}
// ---- persisted viewer preferences (per browser; never data)
const PREF_KEY = 'teleop-analysis-' + D.grid_id;
let prefs = {};
try { prefs = JSON.parse(localStorage.getItem(PREF_KEY) || '{}') || {}; } catch (e) { prefs = {}; }
function savePrefs() { try { localStorage.setItem(PREF_KEY, JSON.stringify(prefs)); } catch (e) { /* private window */ } }

// ---- numbers: nearest-rank median with Python's round-half-to-even (stats.percentile)
function pyRound(x) { const f = Math.floor(x), d = x - f; if (d > 0.5) return f + 1; if (d < 0.5) return f; return (f % 2 === 0) ? f : f + 1; }
function median(vals) {
  const v = vals.filter(isNum).sort((a, b) => a - b); if (!v.length) return null;
  return v[Math.min(v.length - 1, Math.max(0, pyRound(0.5 * (v.length - 1))))];
}
function fnum(v) {
  if (!isNum(v)) return '–';
  const a = Math.abs(v);
  if (a >= 1000) return v.toLocaleString('en-US', {maximumFractionDigits: 0});
  if (a >= 100) return v.toFixed(0);
  if (a >= 10) return v.toFixed(1);
  if (a >= 0.01 || a === 0) return v.toFixed(2);
  return v.toPrecision(2);
}
const COUNTS = new Set(['packets', 'samples']);
function fval(v, unit) {
  if (!isNum(v)) return '–';
  if (unit === 'share') return (100 * v).toFixed(2) + '%';
  if (unit === '%') return v.toFixed(2) + '%';
  if (COUNTS.has(unit) && Number.isInteger(v)) return v.toLocaleString('en-US') + ' ' + unit;
  if (unit === 'QP') return fnum(v);
  return fnum(v) + (unit ? ' ' + unit : '');
}
function stepDecimals(step) {
  if (!(step > 0)) return 0;
  let d = 0; while (d < 6 && Math.abs(Math.round(step * Math.pow(10, d)) - step * Math.pow(10, d)) > 1e-6) d++;
  return d;
}
function tickFmt(v, unit, dec) {
  if (unit === 'share') return (100 * v).toFixed(Math.max(0, dec - 2)) + '%';
  return Math.abs(v) >= 1000 ? v.toLocaleString('en-US', {minimumFractionDigits: dec, maximumFractionDigits: dec}) : v.toFixed(dec);
}
function varFmt(a, v) {
  if (v == null || v === '') return '–';
  if (a === 'fps') return v + ' fps';
  if (a === 'bpp') return 'bpp ' + v;
  if (a === 'kbps') return v + ' kbps';
  if (a === 'codec') return String(v);
  return a + ' ' + v;
}
const KPI = {}; D.kpis.forEach(k => { KPI[k.id] = k; });
const ST = {stat: prefs.stat || 'p99', incl: !!prefs.incl};

function kpiPath(k, codec) { return (k.id === 'qp') ? (D.qp_path[codec] || k.path) : k.path; }
function repVal(rep, k, stat, codec) {
  const m = rep.mx[kpiPath(k, codec)]; if (!m) return null;
  const v = m[k.kind === 'summary' ? stat : 'value']; return isNum(v) ? v : null;
}
function counted(rep) { return rep.has && (ST.incl || rep.included); }
function comboVal(cb, k, stat) { return median(cb.repeats.filter(counted).map(r => repVal(r, k, stat, cb.codec))); }
function statOf(k) { return k.kind === 'summary' ? ST.stat : 'value'; }
function statLabel(k, stat) { return k.kind === 'summary' ? (stat || ST.stat) : 'value'; }

// ---- series: every swept axis except x; hue = first series axis, shape/dash = second
const X = D.x;
const SA = D.series_axes;
const xVals = X ? (D.values[X] || []) : [];
function serKey(vars) { return JSON.stringify(SA.map(a => vars[a] == null ? null : vars[a])); }
const series = [];
(function () {
  const seen = new Map();
  D.combos.forEach(cb => { const k = serKey(cb.vars); if (!seen.has(k)) { seen.set(k, {key: k, vars: cb.vars, combos: []}); } seen.get(k).combos.push(cb); });
  const ord = (a, v) => { const i = (D.values[a] || []).indexOf(v); return i < 0 ? 999 : i; };
  const arr = [...seen.values()].sort((p, q) => { for (const a of SA) { const d = ord(a, p.vars[a]) - ord(a, q.vars[a]); if (d) return d; } return 0; });
  const hueAxis = SA[0], shpAxis = SA[1];
  arr.forEach((sr, i) => {
    sr.hue = hueAxis ? Math.max(0, (D.values[hueAxis] || []).indexOf(sr.vars[hueAxis])) : 0;
    sr.shape = shpAxis ? Math.max(0, (D.values[shpAxis] || []).indexOf(sr.vars[shpAxis])) % 4 : 0;
    if (SA.length > 2) { sr.hue = i; sr.shape = 0; }
    sr.label = SA.map(a => varFmt(a, sr.vars[a])).join(' · ') || 'all';
    sr.color = sr.hue < 8 ? `var(--s${sr.hue + 1})` : 'var(--muted)';
    sr.idx = i;
    series.push(sr);
  });
})();
const DASH = ['', '6 4', '2 3', '8 3 2 3'];
function marker(parent, shape, x, y, r, attrs) {
  if (shape === 1) return s('rect', Object.assign({x: x - r, y: y - r, width: 2 * r, height: 2 * r, rx: 1}, attrs), parent);
  if (shape === 2) return s('path', Object.assign({d: `M${x},${y - r * 1.2}L${x + r * 1.1},${y + r * 0.8}L${x - r * 1.1},${y + r * 0.8}Z`}, attrs), parent);
  if (shape === 3) return s('path', Object.assign({d: `M${x},${y - r * 1.25}L${x + r * 1.1},${y}L${x},${y + r * 1.25}L${x - r * 1.1},${y}Z`}, attrs), parent);
  return s('circle', Object.assign({cx: x, cy: y, r}, attrs), parent);
}
function lineKey(sr, w) {
  const sv = document.createElementNS(SVGNS, 'svg'); sv.setAttribute('width', w || 26); sv.setAttribute('height', 12);
  sv.setAttribute('aria-hidden', 'true');
  s('line', {x1: 1, x2: (w || 26) - 1, y1: 6, y2: 6, stroke: sr.color, 'stroke-width': 2, 'stroke-dasharray': DASH[sr.shape] || null}, sv);
  marker(sv, sr.shape, (w || 26) / 2, 6, 3.5, {fill: sr.color, stroke: 'var(--surface)', 'stroke-width': 1});
  return sv;
}

// ---- tooltip
const tip = $('tip');
function showTip(ev, node) {
  tip.replaceChildren(node); tip.style.display = 'block';
  const r = tip.getBoundingClientRect();
  let x = ev.clientX + 16, y = ev.clientY + 14;
  if (x + r.width > window.innerWidth - 8) x = ev.clientX - r.width - 16;
  if (y + r.height > window.innerHeight - 8) y = Math.max(8, window.innerHeight - r.height - 8);
  tip.style.left = x + 'px'; tip.style.top = y + 'px';
}
function hideTip() { tip.style.display = 'none'; }

// ---- scales
function niceTicks(lo, hi, n) {
  if (!(hi > lo)) { const p = Math.abs(lo) || 1; lo -= p * 0.1; hi += p * 0.1; }
  const step0 = (hi - lo) / n, mag = Math.pow(10, Math.floor(Math.log10(step0)));
  const step = [1, 2, 2.5, 5, 10].map(m => m * mag).find(q => q >= step0) || step0;
  const t = [], top = Math.ceil(hi / step - 1e-9) * step;
  for (let v = Math.floor(lo / step + 1e-9) * step; v <= top + step * 1e-9; v += step) t.push(+v.toFixed(10));
  return t;
}
function yDomain(vals, k) {
  let lo = Math.min(...vals), hi = Math.max(...vals);
  const pctOfTarget = k.unit === '%' && k.better === 'higher';
  const cap100 = pctOfTarget && hi <= 100;          // a share of target never exceeds 100
  // zero baseline, so a 1 ms spread on a 36 ms KPI looks like one; only "% of target" KPIs
  // (99.6 .. 100) zoom, where the shortfall from 100 is the quantity
  if (!pctOfTarget && lo >= 0) lo = 0;
  if (hi === lo) { hi = hi + (Math.abs(hi) * 0.1 || 1); lo = lo - (lo === 0 ? 0 : Math.abs(lo) * 0.1); }
  const pad = (hi - lo) * 0.06; if (lo !== 0) lo -= pad; hi += pad;
  if (cap100) hi = Math.min(hi, 100);
  let t = niceTicks(lo, hi, 5);
  if (cap100) t = t.filter(x => x <= 100 + 1e-9);
  return t;
}

// ---- one chart: KPI statistic against x, a line per series, a dot per repeat
function chart(k, stat, combos, opts) {
  opts = opts || {};
  const W = 420, H = 230, m = {l: 46, r: 12, t: 10, b: 30};
  const pw = W - m.l - m.r, ph = H - m.t - m.b;
  const sv = document.createElementNS(SVGNS, 'svg');
  sv.setAttribute('viewBox', `0 0 ${W} ${H}`); sv.setAttribute('class', 'ch'); sv.setAttribute('role', 'img');
  sv.setAttribute('aria-label', `${k.label} ${statLabel(k, stat)} against ${X || 'combination'}`);
  const sers = series.filter(sr => sr.combos.some(cb => combos.includes(cb)));
  const numericX = X && xVals.length && xVals.every(v => typeof v === 'number');
  const xs = X ? xVals.filter(v => combos.some(cb => cb.vars[X] === v)) : combos.map(cb => cb.name);
  const xmin = numericX ? Math.min(...xs) : 0, xmax = numericX ? Math.max(...xs) : xs.length - 1;
  const span = xmax - xmin;
  const XP = (v) => {
    if (!xs.length) return m.l + pw / 2;
    if (numericX) return span > 0 ? m.l + 14 + (pw - 28) * (v - xmin) / span : m.l + pw / 2;
    const i = xs.indexOf(v); return xs.length === 1 ? m.l + pw / 2 : m.l + 14 + (pw - 28) * i / (xs.length - 1);
  };
  const pts = [];      // {sr, x, med, reps:[{rep, v}]}
  const all = [];
  sers.forEach(sr => {
    xs.forEach(xv => {
      const cb = sr.combos.find(c => combos.includes(c) && (X ? c.vars[X] === xv : c.name === xv));
      if (!cb) return;
      const reps = cb.repeats.filter(r => r.has).map(r => ({rep: r, cb, v: repVal(r, k, stat, cb.codec)})).filter(q => isNum(q.v));
      const med = median(reps.filter(q => counted(q.rep)).map(q => q.v));
      reps.forEach(q => all.push(q.v)); if (isNum(med)) all.push(med);
      pts.push({sr, xv, cb, med, reps});
    });
  });
  if (!all.length) {
    stxt(sv, W / 2, H / 2, opts.empty || 'no data', {'text-anchor': 'middle', class: 'nodata'});
    return sv;
  }
  const ticks = yDomain(all, k), lo = ticks[0], hi = ticks[ticks.length - 1];
  const dec = stepDecimals(ticks.length > 1 ? ticks[1] - ticks[0] : 1);
  const YP = (v) => m.t + ph - ph * (v - lo) / ((hi - lo) || 1);
  ticks.forEach(t => {
    s('line', {class: 'gl', x1: m.l, x2: W - m.r, y1: YP(t), y2: YP(t)}, sv);
    stxt(sv, m.l - 6, YP(t) + 3.5, tickFmt(t, k.unit, dec), {'text-anchor': 'end'});
  });
  s('line', {class: 'ax', x1: m.l, x2: W - m.r, y1: m.t + ph, y2: m.t + ph}, sv);
  xs.forEach((xv, i) => {
    const full = X ? String(xv) : ((combos.find(cb => cb.name === xv) || {}).label || String(xv));
    const lab = full.length > 22 ? full.slice(0, 21) + '…' : full;
    const anchor = (!X && xs.length > 1) ? (i === 0 ? 'start' : i === xs.length - 1 ? 'end' : 'middle') : 'middle';
    const t = stxt(sv, XP(xv) + (anchor === 'start' ? -8 : anchor === 'end' ? 8 : 0), m.t + ph + 14, lab, {'text-anchor': anchor});
    if (lab !== full) s('title', {}, t).textContent = full;
  });
  if (X) stxt(sv, W - m.r, H - 2, X, {'text-anchor': 'end'});
  // lines (break at a missing median)
  const ns = sers.length;
  const dodge = (sr) => (sers.indexOf(sr) - (ns - 1) / 2) * Math.min(7, 26 / Math.max(ns, 1));
  sers.forEach(sr => {
    const ps = xs.map(xv => pts.find(p => p.sr === sr && p.xv === xv)).map(p => (p && isNum(p.med)) ? [XP(p.xv) + dodge(sr), YP(p.med)] : null);
    let d = '', pen = false;
    ps.forEach(p => { if (!p) { pen = false; return; } d += (pen ? 'L' : 'M') + p[0].toFixed(1) + ',' + p[1].toFixed(1); pen = true; });
    if (d) s('path', {d, fill: 'none', stroke: sr.color, 'stroke-width': 2, 'stroke-linejoin': 'round', 'stroke-linecap': 'round', 'stroke-dasharray': DASH[sr.shape] || null}, sv);
  });
  // repeat dots, then the median markers on top
  const dots = [];
  pts.forEach(p => {
    const n = p.reps.length;
    p.reps.forEach((q, j) => {
      const x = XP(p.xv) + dodge(p.sr) + (n > 1 ? (j - (n - 1) / 2) * 2.2 : 0), y = YP(q.v);
      const hollow = !q.rep.included;
      marker(sv, p.sr.shape, x, y, 2.6, hollow ? {fill: 'var(--surface)', stroke: p.sr.color, 'stroke-width': 1.2}
        : {fill: p.sr.color, 'fill-opacity': 0.55, stroke: 'var(--surface)', 'stroke-width': 1});
      dots.push({x, y, q, p});
    });
  });
  pts.forEach(p => { if (isNum(p.med)) marker(sv, p.sr.shape, XP(p.xv) + dodge(p.sr), YP(p.med), 4.2, {fill: p.sr.color, stroke: 'var(--surface)', 'stroke-width': 2}); });
  // crosshair + tooltip + click-through
  const xh = s('line', {class: 'xh', y1: m.t, y2: m.t + ph, x1: -10, x2: -10, visibility: 'hidden'}, sv);
  const hit = s('rect', {x: m.l, y: m.t, width: pw, height: ph, fill: 'transparent', style: 'cursor: crosshair'}, sv);
  function toSvg(ev) { const r = sv.getBoundingClientRect(); return [(ev.clientX - r.left) * W / r.width, (ev.clientY - r.top) * H / r.height]; }
  function nearestDot(ev) {
    const [x, y] = toSvg(ev); let best = null, bd = 1e9;
    dots.forEach(d => { const dd = Math.hypot(d.x - x, d.y - y); if (dd < bd) { bd = dd; best = d; } });
    return bd <= 12 ? best : null;
  }
  hit.addEventListener('pointermove', ev => {
    const [x] = toSvg(ev);
    let xv = xs[0], bd = 1e9; xs.forEach(v => { const dd = Math.abs(XP(v) - x); if (dd < bd) { bd = dd; xv = v; } });
    xh.setAttribute('x1', XP(xv)); xh.setAttribute('x2', XP(xv)); xh.setAttribute('visibility', 'visible');
    const near = nearestDot(ev);
    const box = h('div');
    box.append(h('div', {class: 'th', text: `${X ? varFmt(X, xv) : xv} · ${k.label} ${statLabel(k, stat)}`}));
    pts.filter(p => p.xv === xv).forEach(p => {
      const row = h('div', {class: 'tr'}); row.append(lineKey(p.sr, 24), h('span', {text: p.sr.label}), h('span', {class: 'tv', text: fval(p.med, k.unit)}));
      const reps = p.cb.repeats.map(r => { const v = repVal(r, k, stat, p.cb.codec); return `r${r.n} ${r.has ? fval(v, k.unit) : '—'}${r.included ? '' : ' (' + (r.status !== 'OK' ? r.status : 'not counted') + ')'}`; });
      row.append(h('span'), h('span', {class: 'tn', text: reps.join(' · ')}));
      box.append(row);
    });
    if (near) box.append(h('div', {class: 'tn', text: `click: open ${near.q.rep.label.slice(0, 60)} report`}));
    showTip(ev, box);
    hit.style.cursor = near && near.q.rep.report_pdf ? 'pointer' : 'crosshair';
  });
  hit.addEventListener('pointerleave', () => { xh.setAttribute('visibility', 'hidden'); hideTip(); });
  hit.addEventListener('click', ev => { const d = nearestDot(ev); if (d && d.q.rep.report_pdf) window.open(d.q.rep.report_pdf, '_blank'); });
  return sv;
}

function card(k, stat, host, wide) {
  const c = h('div', {class: 'card' + (wide ? ' wide' : '')});
  const better = k.better === 'higher' ? 'higher is better' : k.better === 'lower' ? 'lower is better' : 'context, not scored';
  c.append(h('h4', {text: `${k.label} · ${statLabel(k, stat)}`}));
  c.append(h('div', {class: 'cs', text: `${k.unit === 'share' ? 'share of frames' : k.unit} · ${better}`, title: k.help}));
  const codecs = [...new Set(D.combos.map(cb => cb.codec))];
  if (k.per_codec && codecs.length > 1) {
    const f = h('div', {class: 'facets'});
    codecs.forEach(cd => {
      const cbs = D.combos.filter(cb => cb.codec === cd);
      const wrap = h('div');
      const path = kpiPath(k, cd);
      const src = (k.id === 'qp' && path === 'encoder.qp') ? ' · per second (A): no per-frame QP' : '';
      wrap.append(chart(k, stat, cbs, {empty: `no ${k.label} for ${cd}`}), h('div', {class: 'ft', text: `${cd} · ${D.qp_scale[cd] || 'QP'}${src}`}));
      f.append(wrap);
    });
    c.append(f);
  } else {
    if (k.per_codec && codecs.length === 1) c.querySelector('.cs').textContent += ` · ${D.qp_scale[codecs[0]] || ''}`;
    c.append(chart(k, stat, D.combos));
  }
  host.append(c);
}

function renderLegend() {
  const L = $('legend'); L.replaceChildren();
  series.forEach(sr => L.append(h('span', {class: 'item'}, lineKey(sr, 30), sr.label)));
  const hol = document.createElementNS(SVGNS, 'svg'); hol.setAttribute('width', 12); hol.setAttribute('height', 12);
  s('circle', {cx: 6, cy: 6, r: 3.5, fill: 'var(--surface)', stroke: 'var(--ink2)', 'stroke-width': 1.2}, hol);
  const sol = document.createElementNS(SVGNS, 'svg'); sol.setAttribute('width', 12); sol.setAttribute('height', 12);
  s('circle', {cx: 6, cy: 6, r: 3, fill: 'var(--ink2)', 'fill-opacity': 0.55}, sol);
  L.append(h('span', {class: 'item'}, sol, 'one repeat'), h('span', {class: 'item'}, hol, 'repeat not counted'));
  $('xname').textContent = X ? `against ${X}` : '(no numeric axis: one point per combination)';
}

function renderCharts() {
  const host = $('charts'), more = $('charts-more');
  host.replaceChildren(); more.replaceChildren();
  D.kpis.forEach(k => card(k, ST.stat, k.primary ? host : more, k.per_codec && new Set(D.combos.map(cb => cb.codec)).size > 1));
  renderExplore();
}

// ---- explore any metric
function renderExplore() {
  const host = $('charts-explore'); host.replaceChildren();
  const p = $('xp-path').value; if (!p) return;
  const meta = D.paths.find(q => q.path === p);
  const k = {id: 'xp', path: p, label: meta && meta.name ? meta.name : p, unit: '', kind: meta ? meta.kind : 'value', better: null, per_codec: /qp/.test(p), help: 'metrics key ' + p};
  const st = $('xp-stat').value || 'p99';
  card(k, k.kind === 'summary' ? st : 'value', host, k.per_codec && new Set(D.combos.map(cb => cb.codec)).size > 1);
}
(function () {
  const ps = $('xp-path'), ss = $('xp-stat');
  D.paths.forEach(q => ps.append(new Option((q.name || q.path) + (q.kind === 'summary' ? '' : ' (value)'), q.path)));
  ['mean', 'p50', 'p95', 'p99', 'max', 'min', 'n'].forEach(x => ss.append(new Option(x, x)));
  ps.value = D.paths.some(q => q.path === 'latency.in_flight') ? 'latency.in_flight' : (D.paths[0] || {}).path || '';
  ss.value = 'p99';
  ps.addEventListener('change', renderExplore); ss.addEventListener('change', renderExplore);
})();

// ---- radar
const RAMP_LIGHT = ['#86b6ef', '#6da7ec', '#5598e7', '#3987e5', '#2a78d6', '#256abf', '#1c5cab', '#184f95', '#104281'];
const RAMP_DARK = ['#184f95', '#1c5cab', '#256abf', '#2a78d6', '#3987e5', '#5598e7', '#6da7ec', '#86b6ef', '#9ec5f4'];
function isDark() {
  const t = document.documentElement.getAttribute('data-theme');
  if (t === 'dark') return true; if (t === 'light') return false;
  return window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;
}
function rampColor(i, n) {
  const R = isDark() ? RAMP_DARK : RAMP_LIGHT;
  if (n <= 1) return R[4];
  const lo = n <= 5 ? 1 : 0, hi = n <= 5 ? 7 : 8;
  return R[Math.round(lo + (hi - lo) * i / (n - 1))];
}
function renderRadar() {
  const sel = $('radar-series');
  const sr = series.find(q => q.key === sel.value) || series[0];
  const host = $('radar-svg'), leg = $('radar-legend'), tab = $('radar-table');
  host.replaceChildren(); leg.replaceChildren(); tab.replaceChildren();
  if (!sr) { host.append(h('p', {class: 'note', text: 'no combination'})); return; }
  const scale = $('radar-scale').value;
  const spokes = D.radar.map(sp => Object.assign({}, sp, {k: KPI[sp.kpi]}));
  const cbs = X ? xVals.map(xv => sr.combos.find(cb => cb.vars[X] === xv)).filter(Boolean) : sr.combos.slice();
  const vals = cbs.map(cb => spokes.map(sp => comboVal(cb, sp.k, sp.stat)));
  const W = 520, Hh = 440, cx = W / 2, cy = Hh / 2 + 6, R = 158;
  const sv = document.createElementNS(SVGNS, 'svg'); sv.setAttribute('viewBox', `0 0 ${W} ${Hh}`); sv.setAttribute('width', '100%');
  sv.setAttribute('role', 'img'); sv.setAttribute('aria-label', 'radar of seven latency and quality KPIs, one polygon per bpp');
  const ang = (i) => -Math.PI / 2 + 2 * Math.PI * i / spokes.length;
  [0.25, 0.5, 0.75, 1].forEach(f => {
    const d = spokes.map((_, i) => `${i ? 'L' : 'M'}${cx + R * f * Math.cos(ang(i))},${cy + R * f * Math.sin(ang(i))}`).join('') + 'Z';
    s('path', {d, class: 'ring'}, sv);
  });
  const worst = spokes.map((_, j) => Math.max(...vals.map(v => v[j]).filter(isNum)));
  const best = spokes.map((_, j) => Math.min(...vals.map(v => v[j]).filter(isNum)));
  spokes.forEach((sp, i) => {
    s('line', {x1: cx, y1: cy, x2: cx + R * Math.cos(ang(i)), y2: cy + R * Math.sin(ang(i)), class: 'spoke'}, sv);
    const lx = cx + (R + 16) * Math.cos(ang(i)), ly = cy + (R + 16) * Math.sin(ang(i));
    const anchor = Math.abs(Math.cos(ang(i))) < 0.2 ? 'middle' : (Math.cos(ang(i)) > 0 ? 'start' : 'end');
    const noData = !isFinite(worst[i]);
    const up = Math.sin(ang(i)) < -0.2;          // upper labels grow upward, away from the rings
    const y1 = up ? ly - 12 : ly + 4, y2 = up ? ly + 1 : ly + 17;
    stxt(sv, lx, y1, sp.label + (noData ? ' (no data)' : ''), {'text-anchor': anchor});
    if (!noData) stxt(sv, lx, y2, `edge ${fval(worst[i], sp.k.unit)}`, {'text-anchor': anchor, style: 'font-size:10px;fill:var(--muted)'});
  });
  const rOf = (v, j) => {
    if (!isNum(v) || !isFinite(worst[j])) return null;
    if (scale === 'ratio') return worst[j] > 0 ? Math.max(0, v / worst[j]) : 0;
    const span = worst[j] - best[j];
    return span > 0 ? 0.1 + 0.9 * (v - best[j]) / span : 1;
  };
  cbs.forEach((cb, i) => {
    const col = rampColor(i, cbs.length);
    const rs = vals[i].map((v, j) => rOf(v, j));
    const complete = rs.every(r => r != null);
    let d = '', pen = false;
    rs.forEach((r, j) => { if (r == null) { pen = false; return; } d += (pen ? 'L' : 'M') + (cx + R * r * Math.cos(ang(j))).toFixed(1) + ',' + (cy + R * r * Math.sin(ang(j))).toFixed(1); pen = true; });
    if (complete) d += 'Z';
    if (d) s('path', {d, fill: complete ? col : 'none', 'fill-opacity': 0.10, stroke: col, 'stroke-width': 2, 'stroke-linejoin': 'round'}, sv);
    rs.forEach((r, j) => {
      if (r == null) return;
      const c = s('circle', {cx: cx + R * r * Math.cos(ang(j)), cy: cy + R * r * Math.sin(ang(j)), r: 4, fill: col, stroke: 'var(--surface)', 'stroke-width': 2}, sv);
      c.addEventListener('pointermove', ev => showTip(ev, h('div', {}, h('div', {class: 'th', text: `${cb.label}`}), h('div', {text: `${spokes[j].label}: ${fval(vals[i][j], spokes[j].k.unit)}`}))));
      c.addEventListener('pointerleave', hideTip);
    });
    const sw = document.createElementNS(SVGNS, 'svg'); sw.setAttribute('width', 26); sw.setAttribute('height', 12);
    s('rect', {x: 1, y: 2, width: 24, height: 8, rx: 2, fill: col, 'fill-opacity': 0.25, stroke: col, 'stroke-width': 2}, sw);
    leg.append(h('span', {class: 'item'}, sw, X ? varFmt(X, cb.vars[X]) : cb.label));
  });
  host.append(sv);
  if (cbs.length === 1) leg.append(h('span', {class: 'note', text: 'one setting only: every spoke is at the edge.'}));
  leg.append(h('span', {class: 'note', text: scale === 'range' ? 'centre ring = best shown, edge = worst shown' : 'centre = 0, edge = worst shown'}));
  // numbers
  const t = h('table', {class: 't'});
  const hr = h('tr', {}, h('th', {text: X || 'combination'}));
  spokes.forEach(sp => hr.append(h('th', {class: 'num', text: sp.label})));
  t.append(h('thead', {}, hr));
  const tb = h('tbody');
  cbs.forEach((cb, i) => {
    const tr = h('tr', {}, h('td', {text: X ? varFmt(X, cb.vars[X]) : cb.label}));
    vals[i].forEach((v, j) => tr.append(h('td', {class: 'num', text: fval(v, spokes[j].k.unit)})));
    tb.append(tr);
  });
  t.append(tb); tab.append(t);
}
(function () {
  const sel = $('radar-series');
  series.forEach(sr => sel.append(new Option(sr.label, sr.key)));
  if (prefs.radar && series.some(q => q.key === prefs.radar)) sel.value = prefs.radar;
  $('radar-scale').value = prefs.radarScale || 'range';
  sel.addEventListener('change', () => { prefs.radar = sel.value; savePrefs(); renderRadar(); });
  $('radar-scale').addEventListener('change', () => { prefs.radarScale = $('radar-scale').value; savePrefs(); renderRadar(); });
})();

// ---- matrix
const MX = {sort: null, dir: 1};
function flagSpan(rep) {
  const serious = rep.flags.filter(f => D.serious_flags.includes(f));
  if (!serious.length) return null;
  const why = serious.map(f => D.flag_text[f] || f).join(', ') + (rep.reasons.length ? ' — ' + rep.reasons.slice(0, 3).join('; ') : '');
  return h('span', {class: 'flag', title: why, 'aria-label': why, text: '⚠'});
}
function repLinks(cb) {
  const w = h('span', {class: 'reps'});
  cb.repeats.forEach(r => {
    const el = r.report_pdf ? h('a', {href: r.report_pdf, text: 'r' + r.n, title: r.label}) : h('span', {class: 'muted', text: 'r' + r.n, title: r.label + ' (no report.pdf)'});
    if (!r.included) { el.style.textDecoration = 'line-through'; el.title += ' — not counted (' + r.status + ')'; }
    w.append(el);
    const f = flagSpan(r); if (f) w.append(f);
  });
  (cb.missing || []).forEach(n => w.append(h('span', {class: 'muted', text: 'r' + n + '?', title: 'planned, not run yet'})));
  return w;
}
function renderMatrix() {
  const more = $('mx-more').checked;
  const cols = D.kpis.filter(k => k.primary || more);
  const rows = D.combos.map(cb => ({cb, v: cols.map(k => comboVal(cb, k, statOf(k))), n: cb.repeats.filter(counted).length}));
  // colour scale per column (QP-like columns within codec)
  const rng = cols.map((k, j) => {
    const groups = {};
    rows.forEach(r => { if (!isNum(r.v[j])) return; const g = k.per_codec ? r.cb.codec : '_'; (groups[g] = groups[g] || []).push(r.v[j]); });
    const out = {}; Object.entries(groups).forEach(([g, vs]) => { out[g] = [Math.min(...vs), Math.max(...vs)]; }); return out;
  });
  // tint = position between the column's best and worst, damped when the column barely varies:
  // full strength only when worst is >= 20% off best (for "% of target" columns: the shortfall
  // from 100), so a 1% spread never paints like a 2x one
  const bad = (k, j, r) => {
    const v = r.v[j]; if (!isNum(v) || !k.better) return null;
    const g = rng[j][k.per_codec ? r.cb.codec : '_']; if (!g || g[1] === g[0]) return 0;
    const t = (v - g[0]) / (g[1] - g[0]);
    const pct = k.unit === '%' && k.better === 'higher';
    const rel = pct ? (g[1] - g[0]) / Math.max(100 - g[0], 1e-9) : (g[1] - g[0]) / Math.max(Math.abs(g[1]), Math.abs(g[0]), 1e-9);
    const gain = Math.min(1, rel / 0.2);
    return (k.better === 'higher' ? 1 - t : t) * gain;
  };
  if (MX.sort != null) {
    const j = MX.sort;
    rows.sort((a, b) => {
      if (j === -1) return 0;
      const va = a.v[j], vb = b.v[j];
      if (!isNum(va) && !isNum(vb)) return 0; if (!isNum(va)) return 1; if (!isNum(vb)) return -1;
      return (va - vb) * MX.dir;
    });
  }
  const t = h('table', {class: 'mx'});
  const hr = h('tr');
  const th0 = h('th', {class: 'l', text: 'combination'}); th0.addEventListener('click', () => { MX.sort = null; renderMatrix(); });
  hr.append(th0, h('th', {class: 'l', text: 'repeats'}));
  cols.forEach((k, j) => {
    const th = h('th', {title: k.help}, k.label, h('span', {class: 'u', text: `${statLabel(k)} · ${k.unit === 'share' ? '%' : k.unit}`}));
    if (MX.sort === j) th.setAttribute('aria-sort', MX.dir > 0 ? 'ascending' : 'descending');
    th.addEventListener('click', () => { if (MX.sort === j) MX.dir = -MX.dir; else { MX.sort = j; MX.dir = (k.better === 'higher') ? -1 : 1; } renderMatrix(); });
    hr.append(th);
  });
  t.append(h('thead', {}, hr));
  const tb = h('tbody');
  rows.forEach(r => {
    const tr = h('tr');
    const name = r.cb.summary_pdf ? h('a', {href: r.cb.summary_pdf, text: r.cb.label, title: r.cb.name + ' summary.pdf'})
      : r.cb.summary_html ? h('a', {href: r.cb.summary_html, text: r.cb.label}) : h('span', {text: r.cb.label, title: r.cb.name});
    tr.append(h('td', {class: 'l'}, name), h('td', {class: 'l'}, repLinks(r.cb)));
    cols.forEach((k, j) => {
      const b = bad(k, j, r);
      const td = h('td', {class: 'v', text: fval(r.v[j], k.unit), title: `${r.cb.label} · ${k.label} ${statLabel(k)} · median of ${r.n} repeat(s)`});
      if (b != null && b > 0.001) {
        const a = 0.62 * b;
        td.style.background = `rgba(${isDark() ? '57,135,229' : '42,120,214'},${a.toFixed(3)})`;
        if (a > 0.42) td.classList.add('hot');
      }
      tr.append(td);
    });
    tb.append(tr);
  });
  t.append(tb);
  $('mx').replaceChildren(t);
  $('mx-key').replaceChildren(h('span', {text: 'better'}), h('i'), h('span', {text: 'worse (within column)'}));
}
$('mx-more').addEventListener('change', renderMatrix);

// ---- every repeat
function renderRepeats() {
  const t = h('table', {class: 't'});
  t.append(h('thead', {}, h('tr', {}, ...['combination', 'repeat', 'status', 'flags', 'e2e p99', 'network p99', 'control network p99', 'encoder', 'report', 'label'].map((x, i) => h('th', {class: (i >= 4 && i <= 6) ? 'num' : null, text: x})))));
  const tb = h('tbody');
  const all = D.combos.flatMap(cb => cb.repeats.map(r => ({cb, r}))).concat(D.controls.map(r => ({cb: {label: 'control ' + r.name, codec: ''}, r})));
  all.forEach(({cb, r}) => {
    const fl = r.flags.map(f => D.flag_text[f] || f).join(', ');
    const serious = r.flags.some(f => D.serious_flags.includes(f));
    const links = h('span', {class: 'reps'});
    if (r.report_pdf) links.append(h('a', {href: r.report_pdf, text: 'pdf'}));
    if (r.report_html) links.append(h('a', {href: r.report_html, text: 'html'}));
    const tr = h('tr', {class: r.included ? null : 'excluded'},
      h('td', {text: cb.label}), h('td', {text: r.n == null ? '' : 'r' + r.n}), h('td', {class: r.status === 'OK' ? null : 'bad', text: r.status}),
      h('td', {class: serious ? 'bad' : null, text: fl || '–', title: r.reasons.join('; ')}),
      h('td', {class: 'num', text: fval(repVal(r, KPI.e2e, 'p99', cb.codec), 'ms')}),
      h('td', {class: 'num', text: fval(repVal(r, KPI.owd, 'p99', cb.codec), 'ms')}),
      h('td', {class: 'num', text: fval(repVal(r, KPI.c_owd, 'p99', cb.codec), 'ms')}),
      h('td', {class: /nvenc|nvidia/i.test(r.encoder) ? null : 'bad', text: r.encoder}), h('td', {}, links), h('td', {class: 'muted', text: r.label}));
    tb.append(tr);
  });
  t.append(tb);
  $('rep-table').replaceChildren(t);
}

// ---- toolbar
(function () {
  const ss = $('stat');
  D.stats.forEach(x => ss.append(new Option(x, x)));
  ss.value = D.stats.includes(ST.stat) ? ST.stat : 'p99';
  ss.addEventListener('change', () => { ST.stat = ss.value; prefs.stat = ST.stat; savePrefs(); renderAll(); });
  const inc = $('incl'); inc.checked = ST.incl;
  inc.addEventListener('change', () => { ST.incl = inc.checked; prefs.incl = ST.incl; savePrefs(); renderAll(); });
  const th = $('theme'); th.value = prefs.theme || '';
  const apply = () => { if (th.value) document.documentElement.setAttribute('data-theme', th.value); else document.documentElement.removeAttribute('data-theme'); };
  apply();
  th.addEventListener('change', () => { prefs.theme = th.value; savePrefs(); apply(); renderRadar(); renderMatrix(); });
  if (window.matchMedia) window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => { renderRadar(); renderMatrix(); });
})();
function renderAll() { renderLegend(); renderCharts(); renderRadar(); renderMatrix(); renderRepeats(); }
renderAll();
})();
"""
