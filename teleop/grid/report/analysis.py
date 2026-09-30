"""comparison/analysis.html: the grid's main analysis page (self-contained: inline CSS, JS, data).

    build(grid_dir, cells, axes) -> str        # the page; report.grid.render writes it

Sections, in the order a reader needs them: Summary (status facts, what was swept, a ranking of
every combination for one KPI with a bar per row, an effects table: best and worst, the change
along the bpp sweep, paired differences for two-valued variables), Trends (one chart per KPI
against bpp, one line per codec x fps = median across repeats, each repeat a dot; with "clip
outliers" the axis fits the medians and a dot beyond it is drawn at the edge with its value), a
radar per codec x fps, the combination x KPI matrix (sortable, tinted per column, linking every
combination's summary.pdf and every repeat's report.pdf), and every repeat with its flags. The
statistic (mean, p50, p95, p99, max) is chosen once in the sticky bar and drives every section.
Each section has a "?" popover explaining how to read it.

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


def build_model(grid_dir: Path, cells, axes: list[str], plan: dict | None = None,
                out_dir: Path | None = None) -> dict:
    """The page's data. Every link in it is relative to out_dir (the directory the page is
    written to: comparison/ by default, the grid root for index.html)."""
    gd = Path(grid_dir)
    out_dir = Path(out_dir) if out_dir is not None else gd / "comparison"
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
        "name": gd.name,
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

def _how(text: str) -> str:
    return f'<details class="how"><summary>?</summary><div>{text}</div></details>'


def _sec(id_: str, eyebrow: str, title: str, lede: str, how: str) -> str:
    return (f'<section id="{id_}"><div class="sec-head"><div class="sec-title"><div class="eyebrow">{eyebrow}</div>'
            f'<h2>{title}</h2></div><div class="lede">{lede}</div>{_how(how)}</div>')


def _fact(label: str, value: str, sub: str = "", cls: str = "") -> str:
    return (f'<div class="fact {cls}"><div class="fl">{H.esc(label)}</div><div class="fv">{value}</div>'
            + (f'<div class="fs">{sub}</div>' if sub else "") + "</div>")


def overview_html(model: dict, cells) -> str:
    ov = model["overview"]
    st = ov["status"]
    plan = model.get("plan") or {}
    planned = plan.get("cells")
    n_rep = ov["repeats"]
    reps = [r for cb in model["combos"] for r in cb["repeats"]]
    counted = sum(1 for r in reps if r["included"])
    flagged = sum(1 for r in reps if set(r["flags"]) & SERIOUS_FLAGS)
    status_bits = " · ".join(f"{v} {k}" for k, v in sorted(st.items(), key=lambda t: (t[0] != "OK", t[0]))) or "none"
    if planned and planned > n_rep:
        status_bits += f" · {planned - n_rep} not run yet"
    flag_items = [f"{n} {H.esc(FLAG_TEXT.get(f, f))}" for f, n in sorted(ov["flags"].items()) if f in SERIOUS_FLAGS]
    enc_all_nvenc = all("nvenc" in name.lower() or "nvidia" in name.lower()
                        for e in ov["encoders"].values() for name in e) if ov["encoders"] else False
    enc_sub = " · ".join(f"{H.esc(codec or '?')}: " + ", ".join(
        f"{H.esc(name)} ×{n}" for name, n in sorted(e.items(), key=lambda t: -t[1]))
        for codec, e in sorted(ov["encoders"].items())) or "–"
    bands = {"a": Counter(), "b": Counter()}
    for c in cells:
        if c.kind == "control":
            continue
        for hh in ("a", "b"):
            b = c.cfg(f"band.{hh}") or {}
            if b:
                bands[hh][f"{b.get('band', '?')} · ARFCN {b.get('arfcn', '?')} · PCI {b.get('pci', '?')}"] += 1
            elif c.metrics is not None:
                bands[hh]["not recorded"] += 1
    band_a = bands["a"].most_common(1)[0][0] if bands["a"] else None
    band_b = bands["b"].most_common(1)[0][0] if bands["b"] else None
    same_band = band_a and band_a == band_b and len(bands["a"]) == 1 and len(bands["b"]) == 1
    band_value = H.esc(band_a.split(" · ")[0]) if same_band else ("A ≠ B" if band_a and band_b else "–")
    band_sub = (H.esc(band_a) + " · both hosts" if same_band else
                " · ".join(f"Host {hh.upper()}: " + ", ".join(f"{H.esc(k)} ×{n}" for k, n in bands[hh].most_common())
                           for hh in ("a", "b") if bands[hh]) or "not recorded")
    facts = [
        _fact("Repeats", f"{n_rep}" + (f"<small> of {planned}</small>" if planned else ""), H.esc(status_bits),
              "" if st.get("OK", 0) == n_rep else "warn"),
        _fact("Counted in medians", f"{counted}", "status OK, not excluded, with metrics"),
        _fact("Flagged", f"{flagged}", " · ".join(flag_items) or "none", "warn" if flagged else "good"),
        _fact("PTP locked", f"{ov['ptp_locked']}<small> of {ov['with_metrics']}</small>",
              ("<b class='bad'>%d not locked</b>" % ov["flags"]["ptp_unlocked"]) if ov["flags"].get("ptp_unlocked")
              else "repeats with metrics", "warn" if ov["flags"].get("ptp_unlocked") else ""),
        _fact("Encoder", "NVENC" if enc_all_nvenc else ("mixed" if ov["encoders"] else "–"), enc_sub,
              "" if enc_all_nvenc else "warn"),
        _fact("Serving band", band_value, band_sub, "" if same_band else "warn"),
    ]
    swept = []
    for a in model["axes"]:
        chips = "".join(f'<span class="chip{" codec" if a == "codec" else ""}" data-codec="{H.esc(str(v))}">'
                        f"{H.esc(_var_fmt(a, v))}</span>" for v in model["values"].get(a, []))
        swept.append(f'<span class="ax"><span class="axn">{H.esc(a)}</span>{chips}</span>')
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
            const.append(f"{k} = {Path(str(v)).name if k == 'clip' else v}")
        else:
            nums = [v for v in vs if isinstance(v, (int, float)) and not isinstance(v, bool)]
            derived.append(f"{k} {min(nums):g}–{max(nums):g}" if len(nums) == len(vs) else f"{k} ({len(vs)} values)")
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
        ctl = ("<h3>Control cells</h3><div class='tbl'><table class='t'><thead><tr><th>cell</th><th>status</th>"
               "<th class='num'>network p99 ms</th><th class='num'>packets lost</th></tr></thead><tbody>"
               + "".join(rows) + "</tbody></table></div>")
    return _sec(
        "overview", "Summary", "What won, and what each variable did",
        "Medians across the counted repeats of every combination, at the statistic chosen in the bar above.",
        "A repeat is <b>counted</b> in every median on this page only when its status is OK, it is not excluded and "
        "it has metrics.json; <i>count INCOMPLETE</i> in the bar adds the others. The <b>ranking</b> orders the "
        "combinations best first for the chosen KPI (a shorter bar is better: the bar is the value, or the shortfall "
        "from 100 % for the delivered-share KPIs). The <b>effects</b> table is computed from the same medians: the "
        "best and worst combination, the change from the lowest to the highest bpp along each codec × fps line, and "
        "for a two-valued variable the median paired difference over matched settings of every other variable, with "
        "how many of those pairs it won. Numbers, not causes. QP is on each codec's own scale and is never compared "
        "across codecs.") + f"""
<div class="facts">{''.join(facts)}</div>
<div class="swept">{''.join(swept)}</div>
<p class="note">held constant: {H.esc(' · '.join(const) or '–')}
{('<br>follow from the axes: ' + H.esc(', '.join(derived))) if derived else ''}</p>
<div class="sub-head"><h3>Ranking</h3><span class="note" id="rk-note"></span></div>
<div class="tabs" id="rk-tabs" role="tablist" aria-label="ranking KPI"></div>
<div id="rk" class="rank-grid"></div>
<div class="sub-head"><h3>Effects of each variable</h3><span class="badge">computed from metrics.json</span></div>
<div id="effects" class="tbl"></div>
<details class="more"><summary>Plain-language summary at p99, for a write-up</summary>
<div class="klines">{''.join(lines)}</div></details>
{ctl}
</section>"""


def build(grid_dir, cells, axes, plan: dict | None = None, out_dir: Path | None = None) -> str:
    """The page for out_dir (default comparison/). Links are relative, so the grid folder can be
    moved or uploaded whole and the page still opens every summary.pdf and report.pdf."""
    gd = Path(grid_dir)
    out_dir = Path(out_dir) if out_dir is not None else gd / "comparison"
    model = build_model(gd, cells, axes, plan, out_dir)
    n_rep = model["overview"]["repeats"]
    def axis_text(a):
        vs = model["values"].get(a, [])
        nums = [v for v in vs if isinstance(v, (int, float)) and not isinstance(v, bool)]
        if len(vs) > 3 and len(nums) == len(vs):
            return f"{a} {min(nums):g}–{max(nums):g} ({len(vs)} values)"
        return f"{a} {', '.join(str(v) for v in vs)}"
    swept = " × ".join(axis_text(a) for a in axes)
    sub = (f"{swept or 'a single combination'} · {n_rep} repeat{'s' if n_rep != 1 else ''} in "
           f"{len(model['combos'])} combination{'s' if len(model['combos']) != 1 else ''}"
           + (f" · grid id {model['grid_id']}" if model["grid_id"] != model["name"] else "")
           + f" · generated {model['generated']}")
    cmp_dir = _rel(gd / "comparison", out_dir)
    body = _toolbar("" if cmp_dir == "." else cmp_dir + "/") + overview_html(model, cells) + SECTIONS \
        + '<div id="tip" role="tooltip"></div>'
    return H.page(f"{model['name']} · analysis", sub, body, extra_css=CSS,
                  script=H.json_script(model, "analysis-data") + f"<script>{JS}</script>")


def _toolbar(cmp: str) -> str:
    """The sticky bar; cmp is the relative prefix of the comparison/ directory from the page."""
    return TOOLBAR.replace("{cmp}", H.esc(cmp))


TOOLBAR = """
<nav class="bar" aria-label="sections and controls">
  <div class="links"><a href="#overview">Summary</a><a href="#curves">Trends</a><a href="#radar">Radar</a>
    <a href="#matrix">Matrix</a><a href="#repeats">Repeats</a>
    <span class="dl"><a href="{cmp}comparison.pdf">PDF</a><a href="{cmp}metrics.csv">CSV</a><a href="{cmp}comparison.html">explorer</a></span></div>
  <div class="ctl">
    <span class="lbl">statistic</span><span class="seg" id="stat" role="group" aria-label="statistic"></span>
    <label class="chk"><input type="checkbox" id="incl"> count INCOMPLETE</label>
    <label class="chk"><input type="checkbox" id="clip"> clip outliers</label>
    <label class="lbl">theme <select id="theme"><option value="">auto</option><option value="light">light</option>
      <option value="dark">dark</option></select></label>
  </div>
</nav>
"""

SECTIONS = _sec(
    "curves", "Trends", "How each KPI moves with bpp", '<span id="xname"></span>',
    "One chart per KPI. The x axis is the swept bpp (other variables fixed per line); the y axis starts at zero "
    "except for the delivered-share KPIs, which zoom on the shortfall from 100 %. Each <b>line</b> joins the median "
    "across the counted repeats of one codec × fps combination (the larger marker); every <b>small dot</b> is one "
    "repeat, hollow when it is not counted. Hue is the codec, marker shape and dash the frame rate. With <i>clip "
    "outliers</i> on, the axis fits the medians and a dot beyond it is drawn at the edge as a small triangle with "
    "its value, so one bad repeat cannot flatten every line; switch it off to see the full range. The "
    "<b>statistic</b> in the bar chooses which per-frame summary is plotted; scalar KPIs (jitter sd, spread, "
    "delivered %, packets lost) have one value per repeat and ignore it. QP is drawn per codec, never on one axis. "
    "Hover for every line's value at the nearest bpp; click near a dot to open that repeat's report.pdf.") + """
<div id="legend" class="legend"></div>
<div id="charts" class="charts"></div>
<details class="more"><summary>More KPIs (frame size, bitrate, spread, tails, packets lost, control round trip, codec timing)</summary>
<div id="charts-more" class="charts"></div></details>
<details class="more"><summary>Explore any metric in metrics.json</summary>
<div class="ctl-row"><label>metric <select id="xp-path"></select></label>
<label>statistic <select id="xp-stat"></select></label></div>
<div id="charts-explore" class="charts"></div></details>
</section>
""" + _sec(
    "radar", "Radar", "Compare combinations across seven KPIs",
    "Choose one variable to compare; the others are held at the values you pick. A smaller polygon is better.",
    "<b>Compare</b> picks the variable whose values become the polygons; every other swept variable is held at "
    "the value chosen on its row, so the polygons differ only in the compared variable (for example av1 vs h265 "
    "at 30 fps and bpp 0.08). Each vertex is the median across counted repeats of a lower-is-better KPI. When the "
    "polygons span codecs the QP spokes are left out: AV1's q-index (0–255) and H.265's QP (0–51) are different "
    "scales. <b>Colours</b>: a codec comparison uses each codec's colour from the rest of the page; any other "
    "comparison uses the held codec's hue, lighter for the lower value and darker for the higher, with dashes for "
    "frame rate. Every spoke is scaled on its own: <b>range</b> puts the best polygon at the centre ring and the "
    "worst at the edge when the worst is at least 20 % off the best (a smaller spread stays near the centre, as "
    "in the matrix tint); <b>ratio</b> puts 0 at the centre and the worst at "
    "the edge, so a spoke's length is proportional to its value. Click a legend entry to hide or show a polygon; "
    "hover it to pick it out. The table has the numbers, the best in each column in bold, and for two polygons "
    "their difference.") + """
<div id="radar-ctl" class="radar-ctl"></div>
<div class="ctl-row"><label class="lbl">scaling <select id="radar-scale"><option value="range">range: best at centre, worst at edge</option>
<option value="ratio">ratio: 0 at centre, worst at edge</option></select></label></div>
<div class="radar-wrap"><div id="radar-svg"></div><div class="radar-side"><div id="radar-cap" class="radar-cap"></div>
<div id="radar-legend" class="legend col"></div>
<div id="radar-table" class="tbl"></div></div></div>
</section>
""" + _sec(
    "matrix", "Matrix", "Every combination × every KPI", "Click a column header to sort; tint marks the worse end of each column.",
    "One row per combination; each cell is the median across the counted repeats of the selected statistic (the "
    "value, for scalar KPIs). Tint marks the worse end within each column (darker = worse; QP within each codec; "
    "frame size and bitrate are context and untinted), at full strength only where the column's worst is at least "
    "20 % off its best (for the delivered-share KPIs: 20 % more shortfall from 100), so a column that barely varies "
    "stays pale. The combination links to its summary.pdf (all its repeats side by side); r1, r2, r3 link to each "
    "repeat's report.pdf; ⚠ marks a repeat with a flag (hover it for the reason).") + """
<div class="ctl-row"><label class="chk"><input type="checkbox" id="mx-more"> more columns</label>
<span class="scale-key" id="mx-key"></span></div>
<div id="mx" class="tbl"></div>
</section>
""" + _sec(
    "repeats", "Repeats", "Every repeat, with its flags", "One row per repeat directory found under the grid.",
    "Flags: <b>not NVENC</b> (the encoder that ran is not NVIDIA's), <b>PTP not locked</b> (cross-host network "
    "numbers are not trustworthy), <b>codec fallback</b> (the negotiated codec differs from the requested one), "
    "<b>INCOMPLETE</b>/<b>SKIPPED</b> (not counted in medians). A planned repeat whose directory does not exist "
    "yet is not listed. Hover a flag for the recorded reason.") + """
<div class="ctl-row"><label class="lbl">show <select id="rep-filter"><option value="all">all repeats</option>
<option value="flagged">flagged only</option><option value="excluded">not counted only</option></select></label></div>
<div id="rep-table" class="tbl"></div>
</section>
"""

CSS = """
main { max-width: 1280px; padding: 0 16px 56px; }
main > section { background: transparent; border: 0; border-radius: 0; padding: 0; margin: 0 0 48px; overflow: visible;
  scroll-margin-top: 60px; }
.sec-head { display: flex; align-items: flex-end; gap: 10px 24px; flex-wrap: wrap; border-bottom: 1px solid var(--base);
  padding: 0 0 10px; margin: 0 0 16px; }
.eyebrow { font-size: 11px; font-weight: 600; letter-spacing: .08em; text-transform: uppercase; color: var(--muted); }
h2 { font-size: 19px; font-weight: 650; margin: 2px 0 0; letter-spacing: -.01em; text-wrap: balance; }
.sec-head .lede { color: var(--ink2); font-size: 12.5px; flex: 1 1 260px; max-width: 640px; padding-bottom: 2px; }
h3 { font-size: 13px; font-weight: 650; margin: 0; color: var(--ink); }
.sub-head { display: flex; align-items: baseline; gap: 10px; margin: 22px 0 8px; }
details.how { margin-left: auto; position: relative; font-size: 12.5px; color: var(--ink2); }
details.how summary { cursor: pointer; list-style: none; width: 22px; height: 22px; border: 1px solid var(--base);
  border-radius: 50%; display: inline-flex; align-items: center; justify-content: center; font-weight: 700;
  color: var(--ink2); background: var(--surface); }
details.how summary::-webkit-details-marker { display: none; }
details.how[open] summary { background: var(--ink); color: var(--surface); border-color: var(--ink); }
details.how > div { position: absolute; right: 0; top: 28px; z-index: 7; width: min(600px, 88vw); background: var(--surface);
  border: 1px solid var(--base); border-radius: 8px; padding: 12px 14px; box-shadow: 0 8px 28px rgba(0,0,0,.16); line-height: 1.5; }
details.more { margin-top: 16px; font-size: 13px; }
details.more summary { cursor: pointer; font-weight: 600; color: var(--ink2); }
details.more[open] summary { margin-bottom: 10px; }
nav.bar { position: sticky; top: 0; z-index: 6; background: var(--surface); border-bottom: 1px solid var(--grid);
  margin: 0 -16px 28px; padding: 8px 16px; display: flex; flex-wrap: wrap; gap: 8px 20px; align-items: center; }
nav.bar .links { display: flex; flex-wrap: wrap; gap: 4px 14px; align-items: center; }
nav.bar .links a { text-decoration: none; font-weight: 600; font-size: 13px; color: var(--ink2); }
nav.bar .links a:hover { color: var(--ink); }
nav.bar .dl { display: inline-flex; gap: 10px; margin-left: 8px; padding-left: 14px; border-left: 1px solid var(--grid); }
nav.bar .dl a { font-weight: 500; color: var(--s1); }
nav.bar .ctl, .ctl-row { display: flex; flex-wrap: wrap; gap: 8px 16px; align-items: center; font-size: 12px; color: var(--ink2); }
nav.bar .ctl { margin-left: auto; }
.ctl-row { margin: 0 0 12px; }
.lbl { font-size: 12px; color: var(--ink2); }
select { font: inherit; font-size: 12px; padding: 3px 6px; background: var(--surface); color: var(--ink);
  border: 1px solid var(--base); border-radius: 5px; max-width: 320px; }
label.chk { display: inline-flex; gap: 5px; align-items: center; cursor: pointer; }
.seg { display: inline-flex; border: 1px solid var(--base); border-radius: 6px; overflow: hidden; background: var(--surface); }
.seg button { font: inherit; font-size: 12px; padding: 4px 10px; border: 0; background: transparent; color: var(--ink2);
  cursor: pointer; font-variant-numeric: tabular-nums; }
.seg button + button { border-left: 1px solid var(--grid); }
.seg button[aria-pressed="true"] { background: var(--ink); color: var(--surface); }
.seg button:focus-visible, .tabs button:focus-visible { outline: 2px solid var(--s1); outline-offset: -2px; }
.badge { font-size: 11px; font-weight: 600; color: var(--muted); border: 1px solid var(--grid); border-radius: 10px; padding: 1px 8px; }
.facts { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 12px 20px; margin: 0 0 16px; }
.fact { border-left: 3px solid var(--grid); padding: 2px 0 2px 10px; min-width: 0; }
.fact.warn { border-left-color: var(--warn); }
.fact.good { border-left-color: var(--good); }
.fact .fl { font-size: 11px; font-weight: 600; letter-spacing: .06em; text-transform: uppercase; color: var(--muted); }
.fact .fv { font-size: 22px; font-weight: 650; font-variant-numeric: tabular-nums; margin: 1px 0; line-height: 1.2; }
.fact .fv small { font-size: 13px; font-weight: 500; color: var(--ink2); }
.fact .fs { font-size: 11.5px; color: var(--ink2); overflow-wrap: anywhere; }
.swept { display: flex; flex-wrap: wrap; gap: 8px 22px; font-size: 12.5px; margin: 0 0 4px; }
.swept .ax { display: inline-flex; align-items: center; gap: 5px; flex-wrap: wrap; }
.swept .axn { color: var(--muted); font-weight: 600; margin-right: 2px; }
.chip { display: inline-block; border: 1px solid var(--base); border-radius: 999px; padding: 0 8px; font-size: 11.5px;
  line-height: 18px; color: var(--ink); background: var(--surface); white-space: nowrap; font-variant-numeric: tabular-nums; }
.chip.codec { font-weight: 650; border-color: currentColor; }
.chips { display: inline-flex; gap: 4px; align-items: center; flex-wrap: nowrap; white-space: nowrap; }
.tabs { display: flex; flex-wrap: wrap; gap: 6px; margin: 0 0 12px; }
.tabs button { font: inherit; font-size: 12px; border: 1px solid var(--base); background: var(--surface); color: var(--ink2);
  border-radius: 999px; padding: 3px 11px; cursor: pointer; }
.tabs button[aria-pressed="true"] { background: var(--ink); color: var(--surface); border-color: var(--ink); }
.rank-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(440px, 1fr)); gap: 8px 32px; }
.rank-grid > div { min-width: 0; overflow-x: auto; }
.rank-grid h4 { margin: 0 0 4px; font-size: 12.5px; color: var(--ink2); font-weight: 600; }
table.rank { width: 100%; border-collapse: collapse; font-size: 12.5px; font-variant-numeric: tabular-nums; }
table.rank td { padding: 5px 8px; border-bottom: 1px solid var(--grid); vertical-align: middle; white-space: nowrap; }
table.rank td.n { color: var(--muted); width: 1%; text-align: right; }
table.rank td.c { width: 1%; }
table.rank td.b { width: 32%; min-width: 120px; }
.rbar { height: 9px; background: var(--grid); border-radius: 2px; overflow: hidden; }
.rbar i { display: block; height: 100%; border-radius: 2px; background: var(--s1); }
table.rank td.v { text-align: right; font-weight: 650; width: 1%; padding-left: 12px; }
table.rank td.r { color: var(--muted); font-size: 11.5px; }
table.rank td.r a { color: var(--ink2); text-decoration: none; border-bottom: 1px dotted var(--base); }
table.rank td.r a:hover { color: var(--s1); border-bottom-color: var(--s1); }
table.rank .nc { text-decoration: line-through; color: var(--muted); }
table.rank tr.best td.v { color: var(--good); }
table.rank td.n .pdf { color: var(--s1); text-decoration: none; font-size: 11px; }
table.fx { border-collapse: collapse; width: 100%; font-size: 12.5px; font-variant-numeric: tabular-nums; }
table.fx th { text-align: left; font-weight: 600; color: var(--ink2); font-size: 11.5px; padding: 6px 10px 6px 0;
  border-bottom: 1px solid var(--base); vertical-align: bottom; white-space: nowrap; }
table.fx th small, table.fx td.k small { display: block; font-weight: 400; color: var(--muted); font-size: 11px; }
table.fx td { padding: 8px 10px 8px 0; border-bottom: 1px solid var(--grid); vertical-align: top; white-space: nowrap; }
table.fx td.k { font-weight: 650; }
table.fx .v { font-weight: 650; }
table.fx .sub { color: var(--ink2); font-size: 11.5px; display: block; margin-top: 2px; }
.wins { display: inline-flex; align-items: center; gap: 6px; font-size: 11px; color: var(--ink2); margin-top: 3px; }
.wins i { display: inline-block; width: 64px; height: 6px; border-radius: 3px; background: var(--grid); position: relative; overflow: hidden; }
.wins i b { position: absolute; left: 0; top: 0; bottom: 0; background: var(--ink2); }
.tbl { overflow-x: auto; }
.legend { display: flex; flex-wrap: wrap; gap: 6px 18px; font-size: 12px; color: var(--ink2); margin: 0 0 12px; }
.legend.col { flex-direction: column; gap: 4px; }
.legend .item { display: inline-flex; align-items: center; gap: 6px; }
.charts { display: grid; grid-template-columns: repeat(auto-fill, minmax(360px, 1fr)); gap: 14px; }
.card { border: 1px solid var(--grid); border-radius: 8px; padding: 10px 12px 6px; background: var(--surface); min-width: 0; }
.card h4 { margin: 0; font-size: 13px; font-weight: 650; }
.card .cs { font-size: 11.5px; color: var(--ink2); margin: 1px 0 6px; }
.card .facets { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 6px; }
.card .ft { font-size: 11.5px; color: var(--ink2); text-align: center; margin-top: 2px; }
.card.wide { grid-column: span 2; }
@media (max-width: 800px) { .card.wide { grid-column: auto; } }
.card .clipnote { font-size: 11px; color: var(--muted); margin: 2px 0 0; }
svg.ch { width: 100%; height: auto; display: block; overflow: visible; touch-action: none; }
svg.ch text { fill: var(--muted); font-size: 10px; font-variant-numeric: tabular-nums; }
svg.ch text.lab { fill: var(--ink2); font-size: 10px; }
svg.ch text.clipv { fill: var(--ink2); font-size: 9px; }
svg.ch .gl { stroke: var(--grid); stroke-width: 1; }
svg.ch .ax { stroke: var(--base); stroke-width: 1; }
svg.ch .xh { stroke: var(--ink2); stroke-width: 1; opacity: .6; }
svg.ch .nodata { fill: var(--muted); font-size: 12px; }
#tip { position: fixed; pointer-events: none; background: var(--surface); color: var(--ink); border: 1px solid var(--base);
  border-radius: 6px; padding: 8px 10px; font-size: 12px; box-shadow: 0 2px 10px rgba(0,0,0,.18); display: none;
  font-variant-numeric: tabular-nums; max-width: 440px; z-index: 10; }
#tip .th { font-weight: 600; margin-bottom: 4px; color: var(--ink2); }
#tip .tr { display: grid; grid-template-columns: 26px 1fr auto; gap: 2px 6px; align-items: center; }
#tip .tv { font-weight: 700; text-align: right; }
#tip .tn { grid-column: 2 / 4; color: var(--ink2); font-size: 11px; margin-bottom: 3px; }
.radar-wrap { display: flex; flex-wrap: wrap; gap: 16px 28px; align-items: flex-start; }
#radar-svg { flex: 1 1 400px; max-width: 600px; }
.radar-side { flex: 1 1 300px; min-width: 0; }
#radar-svg svg text { fill: var(--ink2); font-size: 11px; }
#radar-svg svg .ring { fill: none; stroke: var(--grid); }
#radar-svg svg .spoke { stroke: var(--base); }
#radar-svg g.poly { transition: opacity .15s ease; }
#radar-svg g.poly.fade { opacity: .14; }
.radar-ctl { display: grid; gap: 8px; margin: 0 0 12px; }
.radar-ctl .row { display: flex; flex-wrap: wrap; align-items: center; gap: 6px 12px; }
.radar-ctl .rl { min-width: 88px; font-size: 12px; font-weight: 600; color: var(--ink2); }
.dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 6px; flex: none; }
.radar-cap { font-size: 13px; font-weight: 650; margin: 0 0 8px; }
.legend button.item { font: inherit; font-size: 12px; background: none; border: 1px solid transparent; border-radius: 5px;
  color: var(--ink2); cursor: pointer; padding: 3px 6px; margin-left: -6px; text-align: left; }
.legend button.item:hover, .legend button.item.hot { border-color: var(--base); color: var(--ink); }
.legend button.item[aria-pressed="false"] { opacity: .5; text-decoration: line-through; }
.legend button.item:disabled { cursor: default; }
.legend button.item:focus-visible { outline: 2px solid var(--s1); outline-offset: 1px; }
table.t td.best { font-weight: 700; }
table.t tr.diff td { border-top: 1px solid var(--base); color: var(--ink2); font-weight: 600; }
table.t td.gd, table.t tr.diff td.gd { color: var(--good); }
table.t td.bd, table.t tr.diff td.bd { color: var(--crit); }
@media (prefers-reduced-motion: reduce) { #radar-svg g.poly { transition: none; } }
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
table.mx tr.grp td { background: var(--panel); color: var(--ink2); font-weight: 600; font-size: 11px; letter-spacing: .04em;
  text-transform: uppercase; padding: 3px 8px; }
.reps a, .reps span { margin-right: 6px; }
.flag { color: var(--crit); font-weight: 700; cursor: help; }
.scale-key { display: inline-flex; align-items: center; gap: 6px; }
.scale-key i { display: inline-block; width: 90px; height: 10px; border-radius: 2px;
  background: linear-gradient(90deg, rgba(42,120,214,0), rgba(42,120,214,.62)); }
.klines { display: grid; grid-template-columns: repeat(auto-fill, minmax(420px, 1fr)); gap: 8px 16px; }
.kline { border-left: 3px solid var(--grid); padding: 2px 0 2px 10px; }
.kline.empty { opacity: .7; }
.kline .kh { font-weight: 600; font-size: 12.5px; }
.kline ul { margin: 2px 0 0; padding-left: 18px; font-size: 12px; color: var(--ink2); }
.muted { color: var(--muted); }
@media (max-width: 720px) {
  .rank-grid { grid-template-columns: 1fr; }
  nav.bar .ctl { margin-left: 0; }
  details.how > div { right: auto; left: 0; }
  .klines { grid-template-columns: 1fr; }
}
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
function fdiff(d, unit) {
  if (!isNum(d)) return '–';
  const sign = d >= 0 ? '+' : '−', a = Math.abs(d);
  if (unit === 'share') return sign + (100 * a).toFixed(2) + ' pp';
  if (unit === '%') return sign + a.toFixed(2) + ' pp';
  return sign + fnum(a) + (unit && unit !== 'QP' ? ' ' + unit : '');
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
const ST = {stat: prefs.stat || 'p99', incl: !!prefs.incl, clip: prefs.clip !== false};

function kpiPath(k, codec) { return (k.id === 'qp') ? (D.qp_path[codec] || k.path) : k.path; }
function repVal(rep, k, stat, codec) {
  const m = rep.mx[kpiPath(k, codec)]; if (!m) return null;
  const v = m[k.kind === 'summary' ? stat : 'value']; return isNum(v) ? v : null;
}
function counted(rep) { return rep.has && (ST.incl || rep.included); }
function comboVal(cb, k, stat) { return median(cb.repeats.filter(counted).map(r => repVal(r, k, stat, cb.codec))); }
function statOf(k) { return k.kind === 'summary' ? ST.stat : 'value'; }
function statLabel(k, stat) { return k.kind === 'summary' ? (stat || ST.stat) : 'value'; }
const codecs = [...new Set(D.combos.map(cb => cb.codec))];
const better = (k) => k.better === 'higher' ? 'higher is better' : k.better === 'lower' ? 'lower is better' : 'context, not scored';

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
function seriesOf(cb) { return series.find(sr => sr.key === serKey(cb.vars)); }
function codecColor(codec) {
  if (D.codec_axis) { const i = (D.values[D.codec_axis] || []).indexOf(codec); if (i >= 0 && i < 8 && SA[0] === D.codec_axis) return `var(--s${i + 1})`; }
  const sr = series.find(q => q.combos.some(cb => cb.codec === codec)); return sr ? sr.color : 'var(--s1)';
}
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
// a combination as chips: the codec coloured by its hue, the other axes plain
function chips(cb) {
  const w = h('span', {class: 'chips'});
  D.axes.forEach(a => {
    const v = cb.vars[a]; if (v == null) return;
    const c = h('span', {class: 'chip' + (a === D.codec_axis ? ' codec' : ''), text: varFmt(a, v)});
    if (a === D.codec_axis) c.style.color = codecColor(String(v));
    w.append(c);
  });
  if (!w.childElementCount) w.append(h('span', {class: 'chip', text: cb.label}));
  return w;
}
document.querySelectorAll('.swept .chip.codec').forEach(c => { c.style.color = codecColor(c.dataset.codec); });

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
// with "clip outliers": the axis fits the medians (plus 60 % of their span, at least half their
// size) and any dot beyond it is drawn at the edge, so one bad repeat cannot flatten every line
function clipWindow(meds, k) {
  if (!ST.clip || !meds.length) return null;
  const lo = Math.min(...meds), hi = Math.max(...meds);
  const pctOfTarget = k.unit === '%' && k.better === 'higher';
  const room = 0.6 * Math.max(hi - lo, pctOfTarget ? (100 - lo) : Math.abs(hi) * 0.5, 1e-9);
  return {hi: pctOfTarget ? Infinity : hi + room, lo: pctOfTarget ? lo - room : -Infinity};
}

// ---- one chart: KPI statistic against x, a line per series, a dot per repeat
function chart(k, stat, combos, opts) {
  opts = opts || {};
  const W = 420, H = 230, m = {l: 46, r: 12, t: 14, b: 30};
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
  const all = [], meds = [];
  sers.forEach(sr => {
    xs.forEach(xv => {
      const cb = sr.combos.find(c => combos.includes(c) && (X ? c.vars[X] === xv : c.name === xv));
      if (!cb) return;
      const reps = cb.repeats.filter(r => r.has).map(r => ({rep: r, cb, v: repVal(r, k, stat, cb.codec)})).filter(q => isNum(q.v));
      const med = median(reps.filter(q => counted(q.rep)).map(q => q.v));
      reps.forEach(q => all.push(q.v)); if (isNum(med)) { all.push(med); meds.push(med); }
      pts.push({sr, xv, cb, med, reps});
    });
  });
  if (!all.length) {
    stxt(sv, W / 2, H / 2, opts.empty || 'no data', {'text-anchor': 'middle', class: 'nodata'});
    return sv;
  }
  const win = clipWindow(meds, k);
  const fit = win ? all.filter(v => v <= win.hi && v >= win.lo).concat(meds) : all;
  const ticks = yDomain(fit, k), lo = ticks[0], hi = ticks[ticks.length - 1];
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
  // repeat dots (a dot beyond the axis sits at the edge as a triangle with its value), then the medians on top
  const dots = [];
  let clipped = 0;
  const clipLabels = [];        // [x, up] of every value label drawn at an edge
  pts.forEach(p => {
    const n = p.reps.length;
    p.reps.forEach((q, j) => {
      const x = XP(p.xv) + dodge(p.sr) + (n > 1 ? (j - (n - 1) / 2) * 2.2 : 0);
      const hollow = !q.rep.included;
      const style = hollow ? {fill: 'var(--surface)', stroke: p.sr.color, 'stroke-width': 1.2}
        : {fill: p.sr.color, 'fill-opacity': 0.55, stroke: 'var(--surface)', 'stroke-width': 1};
      let y = YP(q.v);
      if (q.v > hi + 1e-9 || q.v < lo - 1e-9) {
        clipped++;
        const up = q.v > hi;
        y = up ? m.t + 4 : m.t + ph - 4;
        const r = 3.4;
        s('path', Object.assign({d: up ? `M${x},${y - r}L${x + r},${y + r}L${x - r},${y + r}Z` : `M${x},${y + r}L${x + r},${y - r}L${x - r},${y - r}Z`}, style), sv);
        const k2 = clipLabels.filter(c => c[1] === up && Math.abs(c[0] - x) < 36).length;
        clipLabels.push([x, up]);
        stxt(sv, x + 5, y + 3 + k2 * 9 * (up ? 1 : -1), fnum(q.v), {class: 'clipv'});
      } else {
        marker(sv, p.sr.shape, x, y, 2.6, style);
      }
      dots.push({x, y, q, p});
    });
  });
  pts.forEach(p => { if (isNum(p.med)) marker(sv, p.sr.shape, XP(p.xv) + dodge(p.sr), YP(Math.min(hi, Math.max(lo, p.med))), 4.2, {fill: p.sr.color, stroke: 'var(--surface)', 'stroke-width': 2}); });
  sv.dataset.clipped = clipped;
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
  c.append(h('h4', {text: `${k.label} · ${statLabel(k, stat)}`}));
  c.append(h('div', {class: 'cs', text: `${k.unit === 'share' ? 'share of frames' : k.unit} · ${better(k)}`, title: k.help}));
  let clipped = 0;
  if (k.per_codec && codecs.length > 1) {
    const f = h('div', {class: 'facets'});
    codecs.forEach(cd => {
      const cbs = D.combos.filter(cb => cb.codec === cd);
      const wrap = h('div');
      const path = kpiPath(k, cd);
      const src = (k.id === 'qp' && path === 'encoder.qp') ? ' · per second (A): no per-frame QP' : '';
      const sv = chart(k, stat, cbs, {empty: `no ${k.label} for ${cd}`});
      clipped += +(sv.dataset.clipped || 0);
      wrap.append(sv, h('div', {class: 'ft', text: `${cd} · ${D.qp_scale[cd] || 'QP'}${src}`}));
      f.append(wrap);
    });
    c.append(f);
  } else {
    if (k.per_codec && codecs.length === 1) c.querySelector('.cs').textContent += ` · ${D.qp_scale[codecs[0]] || ''}`;
    const sv = chart(k, stat, D.combos);
    clipped += +(sv.dataset.clipped || 0);
    c.append(sv);
  }
  if (clipped) c.append(h('div', {class: 'clipnote', text: `${clipped} repeat${clipped > 1 ? 's' : ''} beyond the axis, drawn at the edge with the value (hover for detail)`}));
  host.append(c);
}

const FEATURED = ['e2e', 'owd', 'jit_ia', 'qp', 'c_owd', 'fps'];
function renderLegend() {
  const L = $('legend'); L.replaceChildren();
  series.forEach(sr => L.append(h('span', {class: 'item'}, lineKey(sr, 30), sr.label)));
  const hol = document.createElementNS(SVGNS, 'svg'); hol.setAttribute('width', 12); hol.setAttribute('height', 12);
  s('circle', {cx: 6, cy: 6, r: 3.5, fill: 'var(--surface)', stroke: 'var(--ink2)', 'stroke-width': 1.2}, hol);
  const sol = document.createElementNS(SVGNS, 'svg'); sol.setAttribute('width', 12); sol.setAttribute('height', 12);
  s('circle', {cx: 6, cy: 6, r: 3, fill: 'var(--ink2)', 'fill-opacity': 0.55}, sol);
  const big = document.createElementNS(SVGNS, 'svg'); big.setAttribute('width', 12); big.setAttribute('height', 12);
  s('circle', {cx: 6, cy: 6, r: 4.2, fill: 'var(--ink2)', stroke: 'var(--surface)', 'stroke-width': 2}, big);
  L.append(h('span', {class: 'item'}, big, 'median of counted repeats'), h('span', {class: 'item'}, sol, 'one repeat'),
           h('span', {class: 'item'}, hol, 'repeat not counted'));
  $('xname').textContent = X ? `One line per ${SA.join(' × ') || 'combination'} against ${X}. Lower is better unless a card says otherwise.`
    : 'No numeric axis: one point per combination.';
}
function renderCharts() {
  const host = $('charts'), more = $('charts-more');
  host.replaceChildren(); more.replaceChildren();
  const order = FEATURED.filter(id => KPI[id]).concat(D.kpis.map(k => k.id).filter(id => !FEATURED.includes(id)));
  order.forEach(id => { const k = KPI[id]; card(k, ST.stat, FEATURED.includes(id) ? host : more, k.per_codec && codecs.length > 1); });
  renderExplore();
}

// ---- explore any metric
function renderExplore() {
  const host = $('charts-explore'); host.replaceChildren();
  const p = $('xp-path').value; if (!p) return;
  const meta = D.paths.find(q => q.path === p);
  const k = {id: 'xp', path: p, label: meta && meta.name ? meta.name : p, unit: '', kind: meta ? meta.kind : 'value', better: null, per_codec: /qp/.test(p), help: 'metrics key ' + p};
  const st = $('xp-stat').value || 'p99';
  card(k, k.kind === 'summary' ? st : 'value', host, k.per_codec && codecs.length > 1);
}
(function () {
  const ps = $('xp-path'), ss = $('xp-stat');
  D.paths.forEach(q => ps.append(new Option((q.name || q.path) + (q.kind === 'summary' ? '' : ' (value)'), q.path)));
  ['mean', 'p50', 'p95', 'p99', 'max', 'min', 'n'].forEach(x => ss.append(new Option(x, x)));
  ps.value = D.paths.some(q => q.path === 'latency.in_flight') ? 'latency.in_flight' : (D.paths[0] || {}).path || '';
  ss.value = 'p99';
  ps.addEventListener('change', renderExplore); ss.addEventListener('change', renderExplore);
})();

// ---- effects: best/worst, the x-axis sweep per line, paired differences for two-valued axes
// (the same arithmetic as the Python kpi_sentences, at the statistic the viewer chose)
function effects(k) {
  const stat = statOf(k);
  const groups = (k.per_codec && D.codec_axis) ? codecs : [null];
  return groups.map(grp => {
    const cbs = D.combos.filter(cb => grp == null || cb.codec === grp);
    const vals = cbs.map(cb => ({cb, v: comboVal(cb, k, stat)})).filter(q => isNum(q.v));
    const out = {group: grp, n: vals.length, best: null, worst: null, x: null, pairs: []};
    if (!vals.length) return out;
    const sign = k.better === 'higher' ? -1 : 1;
    const sorted = vals.slice().sort((a, b) => sign * (a.v - b.v));
    out.best = sorted[0]; out.worst = sorted[sorted.length - 1];
    if (X && xVals.length > 1) {
      const deltas = [];
      [...new Set(vals.map(q => serKey(q.cb.vars)))].forEach(key => {
        const pts = new Map(); vals.filter(q => serKey(q.cb.vars) === key).forEach(q => pts.set(q.cb.vars[X], q.v));
        const have = xVals.filter(xv => pts.has(xv));
        if (have.length >= 2) deltas.push({x0: have[0], x1: have[have.length - 1], d: pts.get(have[have.length - 1]) - pts.get(have[0])});
      });
      if (deltas.length) out.x = {x0: deltas[0].x0, x1: deltas[0].x1, lo: Math.min(...deltas.map(d => d.d)), hi: Math.max(...deltas.map(d => d.d)), n: deltas.length};
    }
    SA.forEach(a => {
      if (k.per_codec && a === D.codec_axis) return;
      const av = (D.values[a] || []).filter(v => vals.some(q => q.cb.vars[a] === v));
      if (av.length !== 2) return;
      const others = D.axes.filter(b => b !== a);
      const keyOf = cb => JSON.stringify(others.map(b => cb.vars[b]));
      const pa = new Map(), pb = new Map();
      vals.forEach(q => { if (q.cb.vars[a] === av[0]) pa.set(keyOf(q.cb), q.v); else if (q.cb.vars[a] === av[1]) pb.set(keyOf(q.cb), q.v); });
      const ds = []; pa.forEach((v, key) => { if (pb.has(key)) ds.push(pb.get(key) - v); });
      if (!ds.length) return;
      const wins = ds.filter(d => k.better === 'higher' ? d > 0 : d < 0).length;
      out.pairs.push({axis: a, a: av[0], b: av[1], med: median(ds), wins, n: ds.length});
    });
    return out;
  });
}
function pairAxes() {
  return SA.filter(a => (D.values[a] || []).length === 2).map(a => ({axis: a, a: D.values[a][0], b: D.values[a][1]}));
}
function winsBar(wins, n, who) {
  const w = h('span', {class: 'wins'});
  const i = h('i'); const b = h('b'); b.style.width = (100 * wins / n).toFixed(0) + '%'; i.append(b);
  w.append(i, `${who} better in ${wins} of ${n}`);
  return w;
}
function renderEffects() {
  const host = $('effects'); host.replaceChildren();
  const pax = pairAxes();
  const t = h('table', {class: 'fx'});
  const hr = h('tr', {}, h('th', {text: 'KPI'}), h('th', {}, 'Best', h('small', {text: 'combination · value'})),
    h('th', {}, 'Worst', h('small', {text: 'and how far off the best'})));
  if (X && xVals.length > 1) hr.append(h('th', {}, `${X} ${xVals[0]} → ${xVals[xVals.length - 1]}`, h('small', {text: 'change along each line'})));
  pax.forEach(p => hr.append(h('th', {}, `${varFmt(p.axis, p.b)} vs ${varFmt(p.axis, p.a)}`, h('small', {text: 'median over matched settings'}))));
  t.append(h('thead', {}, hr));
  const tb = h('tbody');
  D.kpis.filter(k => k.primary).forEach(k => {
    effects(k).forEach(e => {
      const tr = h('tr');
      const name = k.label + (e.group != null ? ` · ${e.group}` : '');
      const sub = `${statLabel(k)} · ${better(k)}` + (e.group != null ? ` · ${D.qp_scale[e.group] || ''}` : '');
      tr.append(h('td', {class: 'k', title: k.help}, name, h('small', {text: sub})));
      if (!e.best) {
        tr.append(h('td', {class: 'muted', colspan: 2 + (X && xVals.length > 1 ? 1 : 0) + pax.length, text: 'no data in any counted repeat'}));
        tb.append(tr); return;
      }
      const bestTd = h('td', {}, chips(e.best.cb), h('span', {class: 'sub'}, h('span', {class: 'v', text: fval(e.best.v, k.unit)})));
      const ratio = (k.better === 'lower' && e.best.v > 0) ? e.worst.v / e.best.v : null;
      const worstTd = h('td', {}, chips(e.worst.cb), h('span', {class: 'sub'}, h('span', {class: 'v', text: fval(e.worst.v, k.unit)}),
        e.worst !== e.best ? ` · ${fdiff(e.worst.v - e.best.v, k.unit)}${ratio && isFinite(ratio) && ratio >= 1.005 ? ', ' + ratio.toFixed(2) + '×' : ''}` : ' · same combination'));
      tr.append(bestTd, worstTd);
      if (X && xVals.length > 1) {
        if (e.x) {
          const same = Math.abs(e.x.hi - e.x.lo) < 1e-12;
          tr.append(h('td', {}, h('span', {class: 'v', text: same ? fdiff(e.x.lo, k.unit) : `${fdiff(e.x.lo, k.unit)} to ${fdiff(e.x.hi, k.unit)}`}),
            h('span', {class: 'sub', text: `${X} ${e.x.x0} → ${e.x.x1}, ${e.x.n} line${e.x.n !== 1 ? 's' : ''}`})));
        } else tr.append(h('td', {class: 'muted', text: '–'}));
      }
      pax.forEach(p => {
        const pr = e.pairs.find(q => q.axis === p.axis);
        if (!pr) { tr.append(h('td', {class: 'muted', text: k.per_codec && p.axis === D.codec_axis ? 'not comparable across codecs' : '–'})); return; }
        tr.append(h('td', {}, h('span', {class: 'v', text: fdiff(pr.med, k.unit)}), h('br'), winsBar(pr.wins, pr.n, varFmt(pr.axis, pr.b))));
      });
      tb.append(tr);
    });
  });
  t.append(tb); host.append(t);
}

// ---- ranking: every combination best first for one KPI, a bar per row (shorter is better)
const RK = {kpi: (prefs.rk && KPI[prefs.rk]) ? prefs.rk : 'e2e'};
function renderRankTabs() {
  const tabs = $('rk-tabs'); tabs.replaceChildren();
  D.kpis.filter(k => k.primary).forEach(k => {
    const b = h('button', {type: 'button', role: 'tab', 'aria-pressed': String(k.id === RK.kpi), text: k.label, title: k.help});
    b.addEventListener('click', () => { RK.kpi = k.id; prefs.rk = k.id; savePrefs(); renderRankTabs(); renderRanking(); });
    tabs.append(b);
  });
}
function renderRanking() {
  const host = $('rk'); host.replaceChildren();
  const k = KPI[RK.kpi] || KPI.e2e; const stat = statOf(k);
  const pct = k.unit === '%' && k.better === 'higher';
  const groups = (k.per_codec && D.codec_axis) ? codecs : [null];
  $('rk-note').textContent = `${k.label}, ${statLabel(k)}: ${better(k)}. Bar = ${pct ? 'shortfall from 100 %' : 'the value'}; shorter is better. Names link to summary.pdf, r1 r2 r3 to each report.pdf.`;
  groups.forEach(grp => {
    const cbs = D.combos.filter(cb => grp == null || cb.codec === grp);
    const rows = cbs.map(cb => ({cb, v: comboVal(cb, k, stat), n: cb.repeats.filter(counted).length}));
    const sign = k.better === 'higher' ? -1 : 1;
    rows.sort((a, b) => { if (!isNum(a.v) && !isNum(b.v)) return 0; if (!isNum(a.v)) return 1; if (!isNum(b.v)) return -1; return sign * (a.v - b.v); });
    const vs = rows.map(r => r.v).filter(isNum);
    const barOf = (v) => {
      if (!isNum(v) || !vs.length) return 0;
      if (pct) { const mx = Math.max(...vs.map(x => 100 - x)); return mx > 0 ? (100 - v) / mx : 0; }
      const mx = Math.max(...vs.map(Math.abs)); return mx > 0 ? Math.abs(v) / mx : 0;
    };
    const box = h('div');
    if (grp != null) box.append(h('h4', {text: `${grp} · ${D.qp_scale[grp] || 'QP'}`}));
    const t = h('table', {class: 'rank'});
    const tb = h('tbody');
    rows.forEach((r, i) => {
      const tr = h('tr', {class: i === 0 && isNum(r.v) ? 'best' : null});
      const name = r.cb.summary_pdf ? h('a', {href: r.cb.summary_pdf, class: 'pdf', title: r.cb.name + ' summary.pdf', text: 'pdf'}) : null;
      tr.append(h('td', {class: 'n'}, isNum(r.v) ? String(i + 1) : '–'), h('td', {class: 'c'}, chips(r.cb)));
      const bar = h('div', {class: 'rbar'}); const fill = h('i'); fill.style.width = (100 * barOf(r.v)).toFixed(1) + '%';
      fill.style.background = codecColor(r.cb.codec); bar.append(fill);
      tr.append(h('td', {class: 'b'}, bar), h('td', {class: 'v', text: fval(r.v, k.unit), title: `median of ${r.n} counted repeat(s)`}));
      const reps = h('td', {class: 'r'});
      r.cb.repeats.forEach((rep, j) => {
        const v = repVal(rep, k, stat, r.cb.codec);
        const txt = `r${rep.n} ${rep.has ? fnum(v) : '—'}`;
        const el = rep.report_pdf ? h('a', {href: rep.report_pdf, text: txt, title: rep.label}) : h('span', {text: txt, title: rep.label});
        if (!rep.included) { el.classList.add('nc'); el.title += ' — not counted (' + rep.status + ')'; }
        if (j) reps.append(' · ');
        reps.append(el);
      });
      (r.cb.missing || []).forEach(n => reps.append(' · ', h('span', {class: 'muted', text: `r${n} ?`, title: 'planned, not run yet'})));
      tr.append(reps, h('td', {class: 'n'}, name || ''));
      tb.append(tr);
    });
    t.append(tb); box.append(t); host.append(box);
  });
}

// ---- radar: compare the values of one variable, every other variable held at a chosen value
// One-hue ordinal ramps, validated with the dataviz validator (--ordinal, light and dark):
// monotone lightness, adjacent gaps >= 0.06, light end >= 2:1 on the surface. Matched step for
// step in lightness; a codec's ramp is its categorical slot's hue, so orange still means h265.
const RAMPS = {
  light: [['#86b6ef', '#5598e7', '#2a78d6', '#1c5cab', '#104281'],     // slot 1 blue (steps 250-650)
          ['#ff8557', '#f26121', '#cb4800', '#9e3601', '#752600'],     // slot 2 orange
          ['#26ca8e', '#03b079', '#048f62', '#026f4b', '#005136']],    // slot 3 aqua
  dark: [['#184f95', '#256abf', '#3987e5', '#6da7ec', '#9ec5f4'],
         ['#8b2d00', '#b53e00', '#e0510d', '#ff7440', '#ffa98b'],
         ['#016042', '#017f58', '#029f6f', '#31bf8a', '#59dea7']],
};
function isDark() {
  const t = document.documentElement.getAttribute('data-theme');
  if (t === 'dark') return true; if (t === 'light') return false;
  return window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;
}
function codecSlot(codec) {
  const i = D.codec_axis ? (D.values[D.codec_axis] || []).indexOf(codec) : -1;
  return i >= 0 && i < 3 ? i : 0;
}
function rampPick(slot, i, n) {
  const R = (isDark() ? RAMPS.dark : RAMPS.light)[slot];
  if (n <= 1) return R[2];
  return R[Math.round(i * (R.length - 1) / (n - 1))];     // light = the lowest value compared
}
const RD = {cmp: null, hold: {}, hidden: new Set(), hot: null};
const multi = (a) => (D.values[a] || []).length > 1;
(function () {
  const axes = D.axes.filter(multi);
  RD.cmp = (prefs.rdCmp && axes.includes(prefs.rdCmp)) ? prefs.rdCmp : (X && axes.includes(X) ? X : axes[0] || null);
  D.axes.forEach(a => {
    const saved = (prefs.rdHold || {})[a];
    RD.hold[a] = (D.values[a] || []).includes(saved) ? saved : (D.values[a] || [])[0];
  });
})();
function dot(color) { const d = h('i', {class: 'dot'}); d.style.background = color; return d; }
function renderRadarCtl() {
  const host = $('radar-ctl'); host.replaceChildren();
  const axes = D.axes.filter(multi);
  if (!axes.length) return;
  const row = h('div', {class: 'row'}, h('span', {class: 'rl', text: 'Compare'}));
  const seg = h('span', {class: 'seg', role: 'group', 'aria-label': 'variable to compare'});
  axes.forEach(a => {
    const b = h('button', {type: 'button', 'aria-pressed': String(a === RD.cmp), text: a});
    b.addEventListener('click', () => { RD.cmp = a; RD.hidden.clear(); prefs.rdCmp = a; savePrefs(); renderRadarCtl(); renderRadar(); });
    seg.append(b);
  });
  row.append(seg); host.append(row);
  axes.filter(a => a !== RD.cmp).forEach(a => {
    const r = h('div', {class: 'row'}, h('span', {class: 'rl', text: 'Hold ' + a}));
    const sg = h('span', {class: 'seg', role: 'group', 'aria-label': 'hold ' + a + ' at'});
    (D.values[a] || []).forEach(v => {
      const b = h('button', {type: 'button', 'aria-pressed': String(v === RD.hold[a])},
        a === D.codec_axis ? dot(codecColor(String(v))) : null, varFmt(a, v));
      b.addEventListener('click', () => {
        RD.hold[a] = v; RD.hidden.clear();
        prefs.rdHold = Object.assign({}, prefs.rdHold || {}, {[a]: v}); savePrefs();
        renderRadarCtl(); renderRadar();
      });
      sg.append(b);
    });
    r.append(sg); host.append(r);
  });
}
function radarSet() {
  const cmp = RD.cmp;
  const held = D.axes.filter(a => a !== cmp);
  const vals = cmp ? (D.values[cmp] || []) : [null];
  const polys = vals.map((v, i) => ({v, i, key: JSON.stringify(v),
    cb: D.combos.find(c => (cmp == null || c.vars[cmp] === v) && held.every(a => c.vars[a] === RD.hold[a]))}));
  const shapeAxis = SA[1];                      // the axis drawn with dashes on the trend charts
  polys.forEach(p => {
    if (cmp && cmp === D.codec_axis) p.color = codecColor(String(p.v));
    else p.color = rampPick(codecSlot(String(D.codec_axis && RD.hold[D.codec_axis] != null ? RD.hold[D.codec_axis] : (p.cb ? p.cb.codec : ''))), p.i, polys.length);
    p.dash = (cmp && cmp === shapeAxis) ? (DASH[p.i % DASH.length] || null) : null;
    p.label = cmp ? varFmt(cmp, p.v) : (p.cb ? p.cb.label : '');
  });
  return {cmp, held, polys};
}
function setHot(key) {
  RD.hot = key;
  document.querySelectorAll('#radar-svg g.poly').forEach(g => g.classList.toggle('fade', key != null && g.dataset.key !== key));
  document.querySelectorAll('#radar-legend button.item').forEach(b => b.classList.toggle('hot', b.dataset.key === key));
}
function renderRadar() {
  const host = $('radar-svg'), leg = $('radar-legend'), tab = $('radar-table'), cap = $('radar-cap');
  host.replaceChildren(); leg.replaceChildren(); tab.replaceChildren(); cap.replaceChildren();
  const set = radarSet();
  const withData = set.polys.filter(p => p.cb);
  const multiCodec = new Set(withData.map(p => p.cb.codec)).size > 1;
  const spokes = D.radar.map(sp => Object.assign({}, sp, {k: KPI[sp.kpi]})).filter(sp => sp.k && !(multiCodec && sp.k.per_codec));
  const heldTxt = set.held.filter(multi).map(a => varFmt(a, RD.hold[a])).join(' · ');
  cap.append(h('span', {text: set.polys.map(p => p.label).join(' vs ')}), heldTxt ? h('span', {class: 'muted', text: ' · held at ' + heldTxt}) : null);
  if (!withData.length) { host.append(h('p', {class: 'note', text: 'No combination has data for this selection.'})); return; }
  const scale = $('radar-scale').value;
  const vals = new Map(withData.map(p => [p.key, spokes.map(sp => comboVal(p.cb, sp.k, sp.stat))]));
  const W = 600, Hh = 440, cx = W / 2, cy = Hh / 2 + 6, R = 158;
  const sv = document.createElementNS(SVGNS, 'svg'); sv.setAttribute('viewBox', `0 0 ${W} ${Hh}`); sv.setAttribute('width', '100%');
  sv.setAttribute('role', 'img'); sv.setAttribute('aria-label', `radar of ${spokes.length} KPIs: ${set.polys.map(p => p.label).join(' vs ')}` + (heldTxt ? `, held at ${heldTxt}` : ''));
  const ang = (i) => -Math.PI / 2 + 2 * Math.PI * i / spokes.length;
  [0.25, 0.5, 0.75, 1].forEach(f => {
    const d = spokes.map((_, i) => `${i ? 'L' : 'M'}${cx + R * f * Math.cos(ang(i))},${cy + R * f * Math.sin(ang(i))}`).join('') + 'Z';
    s('path', {d, class: 'ring'}, sv);
  });
  // each spoke scaled over every polygon with data (hiding one does not rescale the others)
  const all = [...vals.values()];
  const worst = spokes.map((_, j) => Math.max(...all.map(v => v[j]).filter(isNum)));
  const best = spokes.map((_, j) => Math.min(...all.map(v => v[j]).filter(isNum)));
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
  // range: best at the centre ring, worst at the edge, but only at full strength when the worst is
  // >= 20 % off the best (as the matrix tint), so a 0.01 ms jitter difference stays near the centre
  const rOf = (v, j) => {
    if (!isNum(v) || !isFinite(worst[j])) return null;
    if (scale === 'ratio') return worst[j] > 0 ? Math.max(0, v / worst[j]) : 0;
    const span = worst[j] - best[j];
    if (!(span > 0)) return withData.length > 1 ? 0.1 : 1;
    const gain = Math.min(1, (span / Math.max(Math.abs(worst[j]), Math.abs(best[j]), 1e-9)) / 0.2);
    return 0.1 + 0.9 * gain * (v - best[j]) / span;
  };
  withData.forEach(p => {
    if (RD.hidden.has(p.key)) return;
    const g = s('g', {class: 'poly', 'data-key': p.key}, sv);
    const rs = vals.get(p.key).map((v, j) => rOf(v, j));
    const complete = rs.every(r => r != null);
    let d = '', pen = false;
    rs.forEach((r, j) => { if (r == null) { pen = false; return; } d += (pen ? 'L' : 'M') + (cx + R * r * Math.cos(ang(j))).toFixed(1) + ',' + (cy + R * r * Math.sin(ang(j))).toFixed(1); pen = true; });
    if (complete) d += 'Z';
    if (d) {
      const path = s('path', {d, class: 'shape', fill: complete ? p.color : 'none', 'fill-opacity': 0.12, stroke: p.color, 'stroke-width': 2.2, 'stroke-linejoin': 'round', 'stroke-dasharray': p.dash}, g);
      path.addEventListener('pointerenter', () => setHot(p.key)); path.addEventListener('pointerleave', () => setHot(null));
    }
    rs.forEach((r, j) => {
      if (r == null) return;
      const c = s('circle', {cx: cx + R * r * Math.cos(ang(j)), cy: cy + R * r * Math.sin(ang(j)), r: 4, fill: p.color, stroke: 'var(--surface)', 'stroke-width': 2}, g);
      c.addEventListener('pointermove', ev => { setHot(p.key); showTip(ev, h('div', {}, h('div', {class: 'th', text: p.cb.label}), h('div', {text: `${spokes[j].label}: ${fval(vals.get(p.key)[j], spokes[j].k.unit)}`}))); });
      c.addEventListener('pointerleave', () => { hideTip(); setHot(null); });
    });
  });
  host.append(sv);
  // legend: click to hide or show a polygon, hover to pick it out
  set.polys.forEach(p => {
    const sw = document.createElementNS(SVGNS, 'svg'); sw.setAttribute('width', 28); sw.setAttribute('height', 12); sw.setAttribute('aria-hidden', 'true');
    s('rect', {x: 1, y: 2, width: 26, height: 8, rx: 2, fill: p.color, 'fill-opacity': 0.25, stroke: p.color, 'stroke-width': 2, 'stroke-dasharray': p.dash}, sw);
    const on = !RD.hidden.has(p.key);
    const b = h('button', {type: 'button', class: 'item', 'data-key': p.key, 'aria-pressed': String(on && !!p.cb), disabled: p.cb ? null : 'disabled',
      title: p.cb ? (on ? 'click to hide' : 'click to show') : 'no combination with these settings'}, sw, p.label + (p.cb ? '' : ' (no data)'));
    if (p.cb) {
      b.addEventListener('click', () => { if (RD.hidden.has(p.key)) RD.hidden.delete(p.key); else RD.hidden.add(p.key); renderRadar(); });
      b.addEventListener('pointerenter', () => setHot(p.key)); b.addEventListener('pointerleave', () => setHot(null));
      b.addEventListener('focus', () => setHot(p.key)); b.addEventListener('blur', () => setHot(null));
    }
    leg.append(b);
  });
  const notes = [scale === 'range' ? 'centre ring = best shown; the edge = worst shown when it is 20 % or more off the best (smaller spreads stay near the centre)'
    : 'centre = 0, edge = worst shown'];
  if (multiCodec && D.radar.some(sp => KPI[sp.kpi] && KPI[sp.kpi].per_codec)) notes.push('QP is left out: each codec has its own QP scale (' + withData.map(p => p.cb.codec).filter((c, i, a) => a.indexOf(c) === i).map(c => D.qp_scale[c] || c).join(', ') + ')');
  if (withData.length === 1) notes.push('one combination only: every spoke is at the edge');
  notes.forEach(n => leg.append(h('span', {class: 'note', text: n})));
  // numbers: the best in each column in bold; with two polygons, their difference
  const t = h('table', {class: 't'});
  const hr = h('tr', {}, h('th', {text: set.cmp || 'combination'}));
  spokes.forEach(sp => hr.append(h('th', {class: 'num', text: sp.label})));
  t.append(h('thead', {}, hr));
  const tb = h('tbody');
  withData.forEach(p => {
    const lab = p.cb.summary_pdf ? h('a', {href: p.cb.summary_pdf, title: p.cb.name + ' summary.pdf'}, p.label) : h('span', {text: p.label});
    const tr = h('tr', {}, h('td', {}, dot(p.color), lab));
    vals.get(p.key).forEach((v, j) => tr.append(h('td', {class: 'num' + (isNum(v) && withData.length > 1 && v === best[j] ? ' best' : ''), text: fval(v, spokes[j].k.unit)})));
    tb.append(tr);
  });
  if (withData.length === 2) {
    const [a, b] = withData;
    const tr = h('tr', {class: 'diff'}, h('td', {text: `${b.label} − ${a.label}`}));
    spokes.forEach((sp, j) => {
      const va = vals.get(a.key)[j], vb = vals.get(b.key)[j];
      const d = isNum(va) && isNum(vb) ? vb - va : null;
      tr.append(h('td', {class: 'num' + (d == null || Math.abs(d) < 1e-12 ? '' : d < 0 ? ' gd' : ' bd'), text: d == null ? '–' : fdiff(d, sp.k.unit)}));
    });
    tb.append(tr);
  }
  t.append(tb); tab.append(t);
  if (withData.length === 2) tab.append(h('p', {class: 'note', text: `Last row: ${withData[1].label} minus ${withData[0].label}. Green = ${withData[1].label} is lower (better) on that KPI.`}));
}
(function () {
  $('radar-scale').value = prefs.radarScale || 'range';
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
  const th0 = h('th', {class: 'l', text: 'combination', title: 'click to restore the grid order'}); th0.addEventListener('click', () => { MX.sort = null; renderMatrix(); });
  hr.append(th0, h('th', {class: 'l', text: 'repeats'}));
  cols.forEach((k, j) => {
    const th = h('th', {title: k.help}, k.label, h('span', {class: 'u', text: `${statLabel(k)} · ${k.unit === 'share' ? '%' : k.unit}`}));
    if (MX.sort === j) th.setAttribute('aria-sort', MX.dir > 0 ? 'ascending' : 'descending');
    th.addEventListener('click', () => { if (MX.sort === j) MX.dir = -MX.dir; else { MX.sort = j; MX.dir = (k.better === 'higher') ? -1 : 1; } renderMatrix(); });
    hr.append(th);
  });
  t.append(h('thead', {}, hr));
  const tb = h('tbody');
  let lastGrp = null;
  rows.forEach(r => {
    const grp = SA[0] ? varFmt(SA[0], r.cb.vars[SA[0]]) : null;
    if (MX.sort == null && grp != null && grp !== lastGrp) {
      const g = h('tr', {class: 'grp'}, h('td', {colspan: cols.length + 2, text: grp}));
      tb.append(g); lastGrp = grp;
    }
    const tr = h('tr');
    const name = r.cb.summary_pdf ? h('a', {href: r.cb.summary_pdf, title: r.cb.name + ' summary.pdf'}, chips(r.cb))
      : r.cb.summary_html ? h('a', {href: r.cb.summary_html}, chips(r.cb)) : chips(r.cb);
    if (name.tagName === 'A') name.style.textDecoration = 'none';
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
  const filt = $('rep-filter').value;
  const t = h('table', {class: 't'});
  t.append(h('thead', {}, h('tr', {}, ...['combination', 'repeat', 'status', 'flags', 'e2e p99', 'network p99', 'control network p99', 'encoder', 'report', 'label'].map((x, i) => h('th', {class: (i >= 4 && i <= 6) ? 'num' : null, text: x})))));
  const tb = h('tbody');
  const all = D.combos.flatMap(cb => cb.repeats.map(r => ({cb, r}))).concat(D.controls.map(r => ({cb: {label: 'control ' + r.name, codec: '', vars: {}}, r})));
  let shown = 0;
  all.forEach(({cb, r}) => {
    const serious = r.flags.some(f => D.serious_flags.includes(f));
    if (filt === 'flagged' && !serious) return;
    if (filt === 'excluded' && r.included) return;
    shown++;
    const fl = r.flags.map(f => D.flag_text[f] || f).join(', ');
    const links = h('span', {class: 'reps'});
    if (r.report_pdf) links.append(h('a', {href: r.report_pdf, text: 'pdf'}));
    if (r.report_html) links.append(h('a', {href: r.report_html, text: 'html'}));
    const tr = h('tr', {class: r.included ? null : 'excluded'},
      h('td', {}, cb.vars && Object.keys(cb.vars).length ? chips(cb) : cb.label), h('td', {text: r.n == null ? '' : 'r' + r.n}), h('td', {class: r.status === 'OK' ? null : 'bad', text: r.status}),
      h('td', {class: serious ? 'bad' : null, text: fl || '–', title: r.reasons.join('; ')}),
      h('td', {class: 'num', text: fval(repVal(r, KPI.e2e, 'p99', cb.codec), 'ms')}),
      h('td', {class: 'num', text: fval(repVal(r, KPI.owd, 'p99', cb.codec), 'ms')}),
      h('td', {class: 'num', text: fval(repVal(r, KPI.c_owd, 'p99', cb.codec), 'ms')}),
      h('td', {class: /nvenc|nvidia/i.test(r.encoder) ? null : 'bad', text: r.encoder}), h('td', {}, links), h('td', {class: 'muted', text: r.label}));
    tb.append(tr);
  });
  if (!shown) tb.append(h('tr', {}, h('td', {colspan: 10, class: 'muted', text: 'no repeat matches this filter'})));
  t.append(tb);
  $('rep-table').replaceChildren(t);
}
$('rep-filter').addEventListener('change', renderRepeats);

// ---- toolbar
function renderStatSeg() {
  const seg = $('stat'); seg.replaceChildren();
  D.stats.forEach(x => {
    const b = h('button', {type: 'button', 'aria-pressed': String(x === ST.stat), text: x});
    b.addEventListener('click', () => { ST.stat = x; prefs.stat = x; savePrefs(); renderStatSeg(); renderAll(); });
    seg.append(b);
  });
}
(function () {
  if (!D.stats.includes(ST.stat)) ST.stat = 'p99';
  const inc = $('incl'); inc.checked = ST.incl;
  inc.addEventListener('change', () => { ST.incl = inc.checked; prefs.incl = ST.incl; savePrefs(); renderAll(); });
  const cl = $('clip'); cl.checked = ST.clip;
  cl.addEventListener('change', () => { ST.clip = cl.checked; prefs.clip = ST.clip; savePrefs(); renderCharts(); });
  const th = $('theme'); th.value = prefs.theme || '';
  const apply = () => { if (th.value) document.documentElement.setAttribute('data-theme', th.value); else document.documentElement.removeAttribute('data-theme'); };
  apply();
  th.addEventListener('change', () => { prefs.theme = th.value; savePrefs(); apply(); renderRadar(); renderMatrix(); });
  if (window.matchMedia) window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => { renderRadar(); renderMatrix(); });
  // one open "?" at a time
  document.querySelectorAll('details.how').forEach(d => d.addEventListener('toggle', () => {
    if (d.open) document.querySelectorAll('details.how[open]').forEach(o => { if (o !== d) o.open = false; });
  }));
})();
function renderAll() { renderStatSeg(); renderRankTabs(); renderRanking(); renderEffects(); renderLegend(); renderCharts(); renderRadarCtl(); renderRadar(); renderMatrix(); renderRepeats(); }
renderAll();
})();
"""
