"""Grid comparison: comparison/metrics.csv, comparison.html and comparison.pdf.

Reads every cells/*/manifest.json and cells/*/metrics.json under a grid directory. A cell
with no metrics.json, or with a KPI missing, is skipped for that chart and listed, never
silently dropped. Repeats are never averaged away: each repeat is a point; the median
across repeats is a line and a summary row.

Only writes into <grid_dir>/comparison/.
"""
from __future__ import annotations

import csv
import json
import math
import os
from dataclasses import dataclass, field
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.backends.backend_pdf import PdfPages  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402
from matplotlib.patches import Rectangle  # noqa: E402

from .. import stats  # noqa: E402
from . import html as H  # noqa: E402
from . import style as S  # noqa: E402

SUMMARY_STATS = ("mean", "p50", "p95", "p99", "max", "min", "n")
TAIL = ("p95", "p99", "max")
TABLE_STATS = ("mean", "p50", "p95", "p99", "max")

# (page title, [(metric_path, statistics to plot)], unit)
KPIS_OF_RECORD = [
    ("Quality — encoder QP (higher is worse)", [("encoder.qp", TAIL)], "QP"),
    ("Quality — QP per frame, B's decoder (higher is worse)", [("encoder.qp_per_frame", TAIL)], "QP"),
    ("Latency — one-way (packetize → receive)", [("latency.owd", TAIL)], "ms"),
    ("Latency — end to end", [("latency.e2e", TAIL)], "ms"),
    ("Jitter — one-way sd and RFC 3550 interarrival", [("jitter.owd_sd_ms", ("value",)),
                                                      ("jitter.interarrival_rfc3550_ms", ("p99",))], "ms"),
    ("Frame size", [("frame.size_kb", TAIL)], "kB"),
    ("Delivered frame rate", [("frame.fps_delivered", ("value",))], "fps"),
    ("Loss — packets lost at B", [("network.packets_lost", ("value",))], "packets"),
    ("Tail — share of frames over 100 ms one-way", [("tail.owd_over_100.share", ("value",))], "share"),
]

SKIP_GROUPS = {"config"}
MAX_AXIS_ROWS = 3   # chart rows per KPI page; further axes are in comparison.html


# ======================================================================================
# loading
# ======================================================================================

def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def flatten(metrics: dict) -> dict[str, dict[str, float | None]]:
    """metrics.json -> {metric_path: {statistic: value}}. Summaries give 7 stats, scalars 'value'."""
    out: dict[str, dict[str, float | None]] = {}

    def walk(prefix: str, node) -> None:
        if isinstance(node, dict):
            if "p50" in node and "n" in node:
                out[prefix] = {k: node.get(k) for k in SUMMARY_STATS}
                return
            for k, v in node.items():
                walk(f"{prefix}.{k}" if prefix else str(k), v)
        elif isinstance(node, bool):
            out[prefix] = {"value": 1.0 if node else 0.0}
        elif isinstance(node, (int, float)):
            out[prefix] = {"value": node if math.isfinite(node) else None}
        elif node is None and prefix.count(".") >= 1:
            out[prefix] = {"value": None}
        # strings and lists (episodes, reasons) are not flattened

    for group, node in (metrics or {}).items():
        if group in SKIP_GROUPS:
            continue
        walk(group, node)
    # A metrics.json from before per-frame QP still gets its (empty) rows in metrics.csv, so
    # the column set does not depend on which cells were rebuilt.
    if metrics and "encoder" in metrics:
        out.setdefault("encoder.qp_per_frame", {k: (0 if k == "n" else None) for k in SUMMARY_STATS})
    return out


@dataclass
class GridCell:
    dir: Path
    manifest: dict
    metrics: dict | None
    flat: dict = field(default_factory=dict)

    @property
    def label(self) -> str:
        return str(self.manifest.get("label") or self.dir.name)

    @property
    def variables(self) -> dict:
        v = self.manifest.get("variables") or (self.metrics or {}).get("config", {}).get("variables") or {}
        return dict(v)

    @property
    def status(self) -> str:
        return str(self.manifest.get("status") or ("UNKNOWN" if self.manifest else "NO MANIFEST"))

    @property
    def kind(self) -> str:
        return str(self.manifest.get("kind") or "cell")

    def reasons(self) -> list[str]:
        r = []
        if self.manifest.get("exclusion_reason"):
            r.append(str(self.manifest["exclusion_reason"]))
        if self.status != "OK":
            r.append(f"status {self.status}" + (f": {self.manifest.get('status_reason')}" if self.manifest.get("status_reason") else ""))
        integ = (self.metrics or {}).get("integrity") or {}
        r += [str(x) for x in integ.get("reasons") or [] if not any(str(x) in y for y in r)]
        if self.metrics is None:
            r.append("no metrics.json")
        return r

    @property
    def excluded(self) -> bool:
        integ = (self.metrics or {}).get("integrity") or {}
        return bool(self.manifest.get("excluded_from_comparison") or integ.get("excluded")
                    or self.status != "OK" or self.metrics is None)

    @property
    def hollow(self) -> bool:
        return self.excluded or self.status == "INCOMPLETE"

    def cfg(self, path: str):
        for src in (self.manifest, (self.metrics or {}).get("config") or {}):
            cur = src
            for part in path.split("."):
                cur = cur.get(part) if isinstance(cur, dict) else None
            if cur is not None:
                return cur
        return None

    @property
    def encoder(self) -> str:
        return str(self.cfg("negotiated.encoder_implementation") or "unknown")

    @property
    def nvenc(self) -> bool:
        f = ((self.metrics or {}).get("config") or {}).get("encoder_is_nvenc")
        return f if isinstance(f, bool) else "nvenc" in self.encoder.lower()

    def band(self, h: str) -> str:
        b = self.cfg(f"band.{h}") or {}
        return f"{b.get('band', '?')}/{b.get('arfcn', '?')}/{b.get('pci', '?')}" if b else "–"

    def gates(self) -> tuple[str, list[str]]:
        g = self.cfg("gates") or {}
        allg = [x for h in ("a", "b") for x in (g.get(h) or [])]
        failed = [str(x.get("name", "?")) for x in allg if not x.get("pass")]
        return (f"{len(allg) - len(failed)}/{len(allg)}" if allg else "–"), failed

    def ptp(self) -> str:
        p = self.cfg("ptp") or {}
        integ = ((self.metrics or {}).get("integrity") or {}).get("ptp_locked")
        if isinstance(integ, bool):
            return "locked" if integ else "NOT locked"
        b = p.get("b") or {}
        return str(b.get("state") or "–")

    def value(self, path: str, stat: str):
        v = self.flat.get(path, {}).get(stat)
        return v if S.is_num(v) else None


def load_cells(grid_dir: Path) -> list[GridCell]:
    cells = []
    root = grid_dir / "cells"
    if not root.is_dir():
        return cells
    for d in sorted(p for p in root.iterdir() if p.is_dir()):
        man = _read_json(d / "manifest.json") or {}
        met = _read_json(d / "metrics.json")
        c = GridCell(d, man, met)
        c.flat = flatten(met) if met else {}
        cells.append(c)

    def key(c: GridCell):
        i = c.manifest.get("index")
        return (0 if isinstance(i, int) else 1, i if isinstance(i, int) else 0, c.label)

    return sorted(cells, key=key)


def _sort_values(vals):
    vals = list(dict.fromkeys(vals))
    if all(S.is_num(v) for v in vals):
        return sorted(vals)
    return sorted(vals, key=lambda v: str(v))


def grid_axes(grid_dir: Path, cells: list[GridCell]) -> list[str]:
    """Variables that vary across the grid's (non-control) cells, in grid-file order when known."""
    order: list[str] = []
    gy = grid_dir / "grid.yaml"
    if gy.is_file():
        try:
            import yaml  # Host A only; the report runs on A
            doc = yaml.safe_load(gy.read_text(encoding="utf-8")) or {}
            if isinstance(doc.get("axes"), dict):
                order = list(doc["axes"])
            elif isinstance(doc.get("pairs"), list):
                order = list(dict.fromkeys(k for p in doc["pairs"] if isinstance(p, dict) for k in p))
        except Exception:  # noqa: BLE001 — a broken grid.yaml must not stop the report
            order = []
    pool = ([c for c in cells if c.kind != "control" and c.metrics is not None]
            or [c for c in cells if c.metrics is not None] or cells)
    varying = []
    names = list(dict.fromkeys(k for c in pool for k in c.variables))
    for k in names:
        vals = {json.dumps(c.variables.get(k), sort_keys=True, default=str) for c in pool}
        if len(vals) > 1 and k not in ("repeats",):
            varying.append(k)
    axes = [k for k in order if k in varying] + [k for k in varying if k not in order]
    return axes


def short_label(c: GridCell) -> str:
    gid = str(c.manifest.get("grid_id") or "")
    lab = c.label
    return lab[len(gid) + 1:] if gid and lab.startswith(gid + "-") else lab


# ======================================================================================
# metrics.csv
# ======================================================================================

def write_metrics_csv(path: Path, cells: list[GridCell]) -> int:
    var_names = sorted({k for c in cells for k in c.variables})
    head = ["grid_id", "label", "index", "repeat", "kind", "status", "excluded"] + var_names + \
           ["metric_path", "statistic", "value"]
    n = 0

    def write(tmp: Path) -> None:
        nonlocal n
        with tmp.open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(head)
            for c in cells:
                base = [c.manifest.get("grid_id", ""), c.label, c.manifest.get("index", ""),
                        c.manifest.get("repeat", ""), c.kind, c.status, int(c.excluded)]
                vs = [_csv_val(c.variables.get(k)) for k in var_names]
                for mp in sorted(c.flat):
                    for st, v in c.flat[mp].items():
                        w.writerow(base + vs + [mp, st, "" if v is None else v])
                        n += 1

    H.atomic_write(path, write)
    return n


def _csv_val(v):
    if v is None:
        return ""
    if isinstance(v, bool):
        return "on" if v else "off"
    return v


# ======================================================================================
# comparison.pdf
# ======================================================================================

def _xvals(cells: list[GridCell], axis: str | None):
    if axis is None:
        return [short_label(c) for c in cells]
    return _sort_values([_csv_val(c.variables.get(axis)) for c in cells])


def _panel(ax, cells: list[GridCell], axis: str | None, facet: str | None, path: str, stat: str, unit: str):
    """One chart: KPI statistic against one axis, one point per cell, median line per facet."""
    xs = _xvals(cells, axis)
    pos = {v: i for i, v in enumerate(xs)}
    facet_vals = _sort_values([_csv_val(c.variables.get(facet)) for c in cells]) if facet else [None]
    nf = len(facet_vals)
    missing = 0
    handles = []
    for fi, fv in enumerate(facet_vals):
        color = S.slot(fi)
        members = [c for c in cells if facet is None or _csv_val(c.variables.get(facet)) == fv]
        off = (fi - (nf - 1) / 2) * min(0.18, 0.6 / max(nf, 1))
        by_x: dict = {}
        for c in members:
            v = c.value(path, stat)
            if v is None:
                missing += 1
                continue
            xv = short_label(c) if axis is None else _csv_val(c.variables.get(axis))
            rep = c.manifest.get("repeat") or 0
            jitter = ((rep if isinstance(rep, int) else 0) % 5 - 2) * 0.025
            x = pos[xv] + off + jitter
            if c.hollow:
                ax.scatter([x], [v], s=26, facecolors="none", edgecolors=color, linewidths=1.0, zorder=3)
            else:
                ax.scatter([x], [v], s=26, color=color, edgecolors=S.SURFACE, linewidths=0.8, zorder=3)
                by_x.setdefault(xv, []).append(v)
        line = [(pos[x] + off, stats.summ(vs)["p50"]) for x, vs in by_x.items()]
        line.sort()
        if len(line) > 1:
            ax.plot([p[0] for p in line], [p[1] for p in line], color=color, lw=1.4, zorder=2)
        elif line:
            ax.plot([line[0][0] - 0.12, line[0][0] + 0.12], [line[0][1]] * 2, color=color, lw=1.8, zorder=2)
        if facet:
            handles.append(Line2D([], [], color=color, marker="o", lw=1.4, label=f"{facet}={fv}"))
    ax.set_xticks(range(len(xs)))
    ax.set_xticklabels([str(x) for x in xs], rotation=0 if len(xs) <= 8 else 60, fontsize=6.5,
                       ha="center" if len(xs) <= 8 else "right")
    ax.set_xlim(-0.6, len(xs) - 0.4)
    lo, hi = ax.get_ylim()
    ax.set_ylim(min(0, lo), hi if hi > 0 else 1)
    if unit == "share":
        ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{100 * v:.1f}%"))
    ax.set_xlabel(axis or "cell", fontsize=7)
    return handles, missing


SHORT_NAMES = {"jitter.owd_sd_ms": "sd", "jitter.interarrival_rfc3550_ms": "IA"}


def _kpi_table_spec(kpi):
    """Columns for the per-cell table on a KPI page."""
    title, series, unit = kpi
    cols = []
    for path, sts in series:
        short = SHORT_NAMES.get(path, path.split(".")[-1][:8])
        if sts == ("value",):
            cols.append((path, "value", short if len(series) > 1 else path.split(".")[-1]))
        else:
            for st in TABLE_STATS:
                cols.append((path, st, st if len(series) == 1 else f"{short} {st}"))
    return cols


def _draw_cell_table(fig, x, y_top, w, cells, axes, cols, rows_cap, lh, fs):
    head = ["cell"] + [a for a in axes[:2]] + ["status"] + [c[2] for c in cols]
    widths = [3.6] + [0.8] * len(axes[:2]) + [1.05] + [0.8] * len(cols)
    tot = sum(widths)
    xs, cx = [], x
    for wd in widths:
        xs.append(cx)
        cx += w * wd / tot
    num_from = 1 + len(axes[:2]) + 1
    rights = [xs[i] + w * widths[i] / tot - 0.003 for i in range(len(widths))]
    for i, h in enumerate(head):
        if i >= num_from:
            fig.text(rights[i], y_top, h, fontsize=fs, weight="bold", color=S.INK_2, ha="right", va="center")
        else:
            fig.text(xs[i] + 0.002, y_top, h, fontsize=fs, weight="bold", color=S.INK_2, va="center")
    fig.add_artist(Line2D([x, x + w], [y_top - lh * 0.55] * 2, transform=fig.transFigure, color=S.BASELINE, lw=0.6))
    y = y_top - lh
    label_chars = max(12, int(w * 11.0 * widths[0] / tot / (fs * 0.0086)))
    for r_i, c in enumerate(cells[:rows_cap]):
        if r_i % 2 == 0:
            fig.patches.append(Rectangle((x, y - lh / 2), w, lh, transform=fig.transFigure, color=S.PANEL, lw=0, zorder=0))
        colr = S.MUTED if c.hollow else S.INK
        lab = short_label(c)
        vals = [lab if len(lab) <= label_chars else lab[:label_chars - 6] + "…" + lab[-5:]] + [str(_csv_val(c.variables.get(a))) for a in axes[:2]] + \
               [c.status if not c.excluded or c.status != "OK" else "excluded"]
        for i, v in enumerate(vals):
            fig.text(xs[i] + 0.002, y, v, fontsize=fs, color=colr if i else colr, va="center")
        for j, (path, st, _) in enumerate(cols):
            v = c.value(path, st)
            txt = (S.pct(v) if path.endswith(".share") else S.fmt(v)) if v is not None else "–"
            fig.text(rights[num_from + j], y, txt, fontsize=fs, color=colr, ha="right", va="center")
        y -= lh
    return cells[rows_cap:]


def _page_band(fig, title: str, sub: str) -> None:
    fig.patches.append(Rectangle((0, 0.93), 1, 0.07, transform=fig.transFigure, color=S.BAND, zorder=-1))
    fig.text(0.03, 0.97, title, color=S.BAND_INK, fontsize=13, weight="bold", va="center")
    fig.text(0.03, 0.943, sub, color=S.BAND_INK_2, fontsize=7.5, va="center")


def _footer(fig, i, n, note):
    fig.add_artist(Line2D([0.03, 0.97], [0.035, 0.035], transform=fig.transFigure, color=S.GRIDLINE, lw=0.6))
    fig.text(0.03, 0.018, note[:200], fontsize=6.3, color=S.MUTED)
    fig.text(0.97, 0.018, f"Page {i} of {n}", fontsize=6.3, color=S.MUTED, ha="right")


def build_pdf_figures(grid_id: str, cells: list[GridCell], axes: list[str]) -> list:
    S.apply_rc()
    figs = []
    plotted = [c for c in cells if c.kind != "control" and c.metrics is not None]
    # cover
    fig = plt.figure(figsize=S.PAGE_SIZE)
    _page_band(fig, f"Grid {grid_id} — comparison", f"{len(cells)} cells · axes: {', '.join(axes) or 'none (single combination)'}")
    counts: dict[str, int] = {}
    for c in cells:
        counts[c.status] = counts.get(c.status, 0) + 1
    y = 0.88
    fig.text(0.03, y, "Status: " + "   ".join(f"{k} {v}" for k, v in sorted(counts.items())), fontsize=9, va="top")
    y -= 0.035
    fig.text(0.03, y, "Filled points are included cells; hollow points are excluded or INCOMPLETE and are not in the "
             "median line. Control cells are listed but not plotted.", fontsize=7.5, color=S.INK_2, va="top")
    y -= 0.04
    bad = [c for c in cells if c.excluded or not c.nvenc]
    fig.text(0.03, y, f"Excluded, missing metrics or non-NVENC ({len(bad)})", fontsize=9, weight="bold", va="top")
    y -= 0.03
    for c in bad[:34]:
        why = "; ".join(c.reasons()) or "–"
        if not c.nvenc:
            why = f"encoder {c.encoder} (NOT NVENC); " + why
        fig.text(0.03, y, short_label(c)[:48], fontsize=7, va="top", color=S.INK)
        fig.text(0.33, y, why[:120], fontsize=7, va="top", color=S.CRITICAL if c.excluded or not c.nvenc else S.INK_2)
        y -= 0.022
    if len(bad) > 34:
        fig.text(0.03, y, f"… {len(bad) - 34} more in comparison.html", fontsize=7, color=S.INK_2, va="top")
    if not bad:
        fig.text(0.03, y, "none", fontsize=7.5, color=S.GOOD_TEXT, va="top")
    figs.append((fig, "cover"))

    ax_rows = axes[:MAX_AXIS_ROWS] or [None]
    for kpi in KPIS_OF_RECORD:
        title, series, unit = kpi
        panels = [(path, st) for path, sts in series for st in sts]
        fig = plt.figure(figsize=S.PAGE_SIZE)
        _page_band(fig, title, " · ".join(f"{p} {st}" for p, st in panels) + f" · {unit}")
        chart_top, chart_bot = 0.885, 0.46
        nr = len(ax_rows)
        rh = (chart_top - chart_bot) / nr
        nc = len(panels)
        cw = (0.80 if len(axes) >= 2 else 0.90) / nc   # right margin holds the per-row legend
        missing_total = 0
        for r, axis in enumerate(ax_rows):
            facet = next((a for a in axes if a != axis), None) if axis is not None else None
            row_handles = []
            for k, (path, st) in enumerate(panels):
                ax = fig.add_axes([0.06 + k * cw, chart_top - (r + 1) * rh + 0.05, cw - 0.05, rh - 0.08])
                h, miss = _panel(ax, plotted, axis, facet, path, st, unit)
                missing_total = max(missing_total, miss)
                ax.set_title(f"{st} {path}" if len(series) > 1 else st, fontsize=8, pad=3)
                if k == 0:
                    ax.set_ylabel(unit)
                row_handles = row_handles or h
            if row_handles:
                # one legend per row: the colour means a different variable on each row
                fig.legend(handles=row_handles[:8], loc="center left", ncol=1, fontsize=6.6, frameon=False,
                           bbox_to_anchor=(0.865, chart_top - (r + 0.5) * rh + 0.01),
                           title=f"colour = {facet}", title_fontsize=6.6, alignment="left")
        # table
        cols = _kpi_table_spec(kpi)
        lh, fs = 0.0148, 6.2
        cap_col = int((0.43 - 0.05) / lh) - 1
        rest = _draw_cell_table(fig, 0.03, 0.43, 0.455, cells, axes, cols, cap_col, lh, fs)
        if rest:
            rest = _draw_cell_table(fig, 0.515, 0.43, 0.455, rest, axes, cols, cap_col, lh, fs)
        note = (f"{missing_total} cell(s) lack this metric and are not plotted. " if missing_total else "") + \
               "Points: one per cell (repeats side by side); line: median across included repeats."
        figs.append((fig, note))
        cont = 0
        while rest:
            cont += 1
            fig = plt.figure(figsize=S.PAGE_SIZE)
            _page_band(fig, f"{title} — table continued ({cont})", "")
            cap_full = int((0.89 - 0.05) / lh) - 1
            rest = _draw_cell_table(fig, 0.03, 0.89, 0.455, rest, axes, cols, cap_full, lh, fs)
            if rest:
                rest = _draw_cell_table(fig, 0.515, 0.89, 0.455, rest, axes, cols, cap_full, lh, fs)
            figs.append((fig, "table continued"))
    return figs


# ======================================================================================
# comparison.html
# ======================================================================================

def _html_data(grid_id: str, cells: list[GridCell], axes: list[str]) -> dict:
    var_names = list(dict.fromkeys(k for c in cells for k in c.variables))
    paths: dict[str, list[str]] = {}
    for c in cells:
        for p, sts in c.flat.items():
            paths.setdefault(p, list(sts))
    out_cells = []
    for c in cells:
        gates, failed = c.gates()
        rel = os.path.relpath(c.dir / "report.html", c.dir.parent.parent / "comparison")
        out_cells.append({
            "label": c.label, "short": short_label(c), "index": c.manifest.get("index"),
            "repeat": c.manifest.get("repeat"), "kind": c.kind, "status": c.status,
            "excluded": c.excluded, "hollow": c.hollow, "reasons": c.reasons(),
            "vars": {k: _csv_val(c.variables.get(k)) for k in var_names},
            "encoder": c.encoder, "nvenc": c.nvenc, "band_a": c.band("a"), "band_b": c.band("b"),
            "gates": gates, "gates_failed": failed, "ptp": c.ptp(),
            "report": rel if (c.dir / "report.html").is_file() else None,
            "has_metrics": c.metrics is not None,
            "m": {p: {k: v for k, v in sts.items() if S.is_num(v)} for p, sts in c.flat.items()},
        })
    kpi_record = [p for _, series, _ in KPIS_OF_RECORD for p, _ in series]
    ordered = [p for p in kpi_record if p in paths] + sorted(p for p in paths if p not in kpi_record)
    return {"grid_id": grid_id, "axes": axes, "variables": var_names,
            "kpis": [{"path": p, "stats": paths[p]} for p in ordered],
            "kpis_of_record": kpi_record, "cells": out_cells,
            "slots": list(S.SLOTS)}


HTML_CSS = """
.tabs { display: flex; gap: 4px; margin: 0 0 12px; }
.tabs button { font: inherit; padding: 6px 14px; border: 1px solid var(--grid); background: var(--surface);
  color: var(--ink); border-radius: 6px; cursor: pointer; }
.tabs button[aria-selected="true"] { border-color: var(--s1); box-shadow: inset 0 -2px 0 var(--s1); font-weight: 600; }
.controls { display: flex; flex-wrap: wrap; gap: 12px; margin: 0 0 12px; align-items: end; }
.controls label { display: flex; flex-direction: column; font-size: 12px; color: var(--ink2); gap: 2px; }
.controls select { font: inherit; padding: 4px 6px; background: var(--surface); color: var(--ink);
  border: 1px solid var(--base); border-radius: 4px; max-width: 280px; }
.panels { display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap: 12px; }
.panel { border: 1px solid var(--grid); border-radius: 6px; padding: 8px; background: var(--surface); }
.panel h4 { margin: 0 0 4px; font-size: 12px; color: var(--ink2); }
.panel svg { width: 100%; height: auto; display: block; overflow: visible; }
.panel svg text { fill: var(--muted); font-size: 10px; font-variant-numeric: tabular-nums; }
.panel svg .gl { stroke: var(--grid); stroke-width: 1; }
.panel svg .ax { stroke: var(--base); stroke-width: 1; }
.panel svg .med { stroke: var(--ink2); stroke-width: 2; fill: none; }
.panel svg circle.pt { stroke: var(--surface); stroke-width: 2; cursor: pointer; }
.panel svg circle.pt.hollow { fill: none !important; stroke: var(--s1); stroke-width: 1.5; }
.panel svg circle.pt:hover { stroke: var(--ink); }
#tip { position: fixed; pointer-events: none; background: var(--surface); color: var(--ink); border: 1px solid var(--base);
  border-radius: 4px; padding: 6px 8px; font-size: 12px; box-shadow: 0 2px 8px rgba(0,0,0,.15); display: none;
  font-variant-numeric: tabular-nums; max-width: 360px; z-index: 5; }
.hidden { display: none; }
.legend { font-size: 12px; color: var(--ink2); margin: 6px 0; }
.legend .dot { display: inline-block; width: 9px; height: 9px; border-radius: 50%; background: var(--s1); vertical-align: middle; margin-right: 4px; }
.legend .dot.h { background: none; border: 1.5px solid var(--s1); }
"""

HTML_JS = r"""
(function(){
const D = JSON.parse(document.getElementById('data').textContent);
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const isNum = (v) => typeof v === 'number' && isFinite(v);
function fmt(v, path) {
  if (!isNum(v)) return '–';
  if (v === 0) return '0';
  if (path && /\.share$/.test(path)) return (100*v).toFixed(2) + '%';
  const a = Math.abs(v);
  if (a >= 1000) return v.toLocaleString('en-US', {maximumFractionDigits: 0});
  if (a >= 100) return v.toFixed(0);
  if (a >= 10) return v.toFixed(1);
  return v.toFixed(2);
}
function median(xs) { const s = xs.filter(isNum).slice().sort((a,b)=>a-b); if (!s.length) return null;
  return s[Math.min(s.length-1, Math.max(0, Math.round(0.5*(s.length-1))))]; }  // nearest rank, as stats.py
function sortVals(vs) { vs = [...new Set(vs.map(v => v == null ? '' : v))];
  return vs.every(v => typeof v === 'number') ? vs.sort((a,b)=>a-b) : vs.sort((a,b)=>String(a).localeCompare(String(b), undefined, {numeric:true})); }

// --- tabs ---
document.querySelectorAll('.tabs button').forEach(b => b.addEventListener('click', () => {
  document.querySelectorAll('.tabs button').forEach(x => x.setAttribute('aria-selected', x === b ? 'true' : 'false'));
  document.querySelectorAll('.tab').forEach(t => t.classList.toggle('hidden', t.id !== b.dataset.tab));
}));

// --- controls ---
const kSel = $('kpi'), sSel = $('stat'), xSel = $('xvar'), fSel = $('facet');
const rec = new Set(D.kpis_of_record);
const g1 = document.createElement('optgroup'); g1.label = 'KPIs of record';
const g2 = document.createElement('optgroup'); g2.label = 'all metrics';
D.kpis.forEach(k => { const o = new Option(k.path, k.path); (rec.has(k.path) ? g1 : g2).appendChild(o); });
kSel.append(g1, g2);
const xOpts = D.axes.length ? D.axes.concat(D.variables.filter(v => !D.axes.includes(v))) : D.variables;
xSel.append(new Option('cell (label)', '__cell__'));
xOpts.forEach(v => xSel.append(new Option(v, v)));
fSel.append(new Option('none', ''));
D.variables.forEach(v => fSel.append(new Option(v, v)));
if (D.kpis.some(k => k.path === 'latency.owd')) kSel.value = 'latency.owd';
xSel.value = D.axes[0] || '__cell__';
fSel.value = D.axes[1] || '';
function fillStats() {
  const k = D.kpis.find(k => k.path === kSel.value); const prev = sSel.value;
  sSel.innerHTML = ''; (k ? k.stats : []).forEach(s => sSel.append(new Option(s, s)));
  sSel.value = (k && k.stats.includes(prev)) ? prev : (k && k.stats.includes('p99') ? 'p99' : (k ? k.stats[0] : ''));
}
fillStats();
kSel.addEventListener('change', () => { fillStats(); render(); });
[sSel, xSel, fSel].forEach(e => e.addEventListener('change', render));

const tip = $('tip');
function showTip(ev, html) { tip.innerHTML = html; tip.style.display = 'block';
  const x = Math.min(ev.clientX + 14, window.innerWidth - tip.offsetWidth - 8);
  tip.style.left = x + 'px'; tip.style.top = (ev.clientY + 14) + 'px'; }
function hideTip() { tip.style.display = 'none'; }

function xOf(c, xv) { return xv === '__cell__' ? c.short : c.vars[xv]; }
function niceTicks(lo, hi, n) {
  if (!(hi > lo)) { hi = lo + 1; }
  const step0 = (hi - lo) / n, mag = Math.pow(10, Math.floor(Math.log10(step0)));
  const step = [1,2,2.5,5,10].map(m => m*mag).find(s => s >= step0) || step0;
  const t = [], top = Math.ceil(hi/step - 1e-9)*step;
  for (let v = Math.floor(lo/step + 1e-9)*step; v <= top + step*1e-9; v += step) t.push(+v.toFixed(10));
  return t;
}

function render() {
  const path = kSel.value, st = sSel.value, xv = xSel.value, fv = fSel.value;
  const cells = D.cells.filter(c => c.kind !== 'control');
  const val = c => (c.m[path] || {})[st];
  const withV = cells.filter(c => isNum(val(c)));
  const missing = cells.filter(c => !isNum(val(c)));
  const facets = fv ? sortVals(withV.map(c => c.vars[fv])) : [null];
  const xs = sortVals(withV.map(c => xOf(c, xv)));
  const all = withV.map(val);
  let lo = Math.min(0, ...all), hi = Math.max(...all, 0);
  const ticks = niceTicks(lo, hi, 5); lo = ticks[0]; hi = ticks[ticks.length-1];
  const W = 420, Hh = 240, m = {l: 48, r: 10, t: 16, b: xs.length > 6 ? 70 : 30};
  const pw = W - m.l - m.r, ph = Hh - m.t - m.b;
  const X = i => m.l + (xs.length === 1 ? pw/2 : pw * (i + 0.5) / xs.length);
  const Y = v => m.t + ph - ph * (v - lo) / (hi - lo || 1);
  const host = $('panels'); host.innerHTML = '';
  if (!withV.length) { host.innerHTML = '<p class="note">No cell has ' + esc(path) + ' ' + esc(st) + '.</p>'; }
  facets.forEach(f => {
    const mem = withV.filter(c => fv === '' || c.vars[fv] === f);
    let s = `<svg viewBox="0 0 ${W} ${Hh}" role="img" aria-label="${esc(path)} ${esc(st)}">`;
    ticks.forEach(t => { s += `<line class="gl" x1="${m.l}" x2="${W-m.r}" y1="${Y(t)}" y2="${Y(t)}"/>` +
      `<text x="${m.l-6}" y="${Y(t)+3}" text-anchor="end">${esc(fmt(t, path))}</text>`; });
    s += `<line class="ax" x1="${m.l}" x2="${W-m.r}" y1="${m.t+ph}" y2="${m.t+ph}"/>`;
    xs.forEach((x, i) => { const rot = xs.length > 6;
      s += `<text x="${X(i)}" y="${m.t+ph+14}" text-anchor="${rot ? 'end' : 'middle'}" ${rot ? `transform="rotate(-40 ${X(i)} ${m.t+ph+14})"` : ''}>${esc(x)}</text>`; });
    const med = [];
    xs.forEach((x, i) => {
      const grp = mem.filter(c => xOf(c, xv) === x);
      const inc = grp.filter(c => !c.hollow).map(val);
      const md = median(inc); if (md != null) med.push([X(i), Y(md)]);
      grp.forEach((c, j) => {
        const dx = (j - (grp.length - 1) / 2) * Math.min(10, 40 / Math.max(grp.length, 1));
        s += `<circle class="pt${c.hollow ? ' hollow' : ''}" data-i="${D.cells.indexOf(c)}" cx="${X(i)+dx}" cy="${Y(val(c))}" r="5" fill="var(--s1)"/>`;
      });
    });
    if (med.length > 1) s += `<polyline class="med" points="${med.map(p => p.join(',')).join(' ')}"/>`;
    else if (med.length === 1) s += `<line class="med" x1="${med[0][0]-14}" x2="${med[0][0]+14}" y1="${med[0][1]}" y2="${med[0][1]}"/>`;
    s += '</svg>';
    const div = document.createElement('div'); div.className = 'panel';
    div.innerHTML = `<h4>${fv ? esc(fv) + ' = ' + esc(f) : 'all cells'} · ${esc(path)} ${esc(st)}</h4>` + s;
    host.appendChild(div);
  });
  host.querySelectorAll('circle.pt').forEach(el => {
    const c = D.cells[+el.dataset.i];
    el.addEventListener('mousemove', ev => showTip(ev, `<b>${esc(c.short)}</b><br>${esc(path)} ${esc(st)}: <b>${esc(fmt(val(c), path))}</b>` +
      `<br>repeat ${esc(c.repeat)} · ${esc(c.status)}${c.excluded ? ' · excluded' : ''}`));
    el.addEventListener('mouseleave', hideTip);
    el.addEventListener('click', () => { if (c.report) window.open(c.report, '_blank'); });
  });
  $('missing').innerHTML = missing.length ? 'Not plotted (no ' + esc(path) + ' ' + esc(st) + '): ' + missing.map(c => esc(c.short)).join(', ') : '';
  renderTable(path, cells, xv, fv);
}

function renderTable(path, cells, xv, fv) {
  const k = D.kpis.find(k => k.path === path);
  const sts = k && k.stats.includes('p50') ? ['mean','p50','p95','p99','max'] : ['value'];
  const keyVars = D.axes.length ? D.axes : [];
  const cols = keyVars.slice();
  const groups = new Map();
  cells.forEach(c => { const key = JSON.stringify(keyVars.map(v => c.vars[v])); if (!groups.has(key)) groups.set(key, []); groups.get(key).push(c); });
  const keys = [...groups.keys()].sort((a, b) => {
    const A = JSON.parse(a), B = JSON.parse(b);
    for (let i = 0; i < A.length; i++) { if (A[i] === B[i]) continue;
      return (typeof A[i] === 'number' && typeof B[i] === 'number') ? A[i] - B[i] : String(A[i]).localeCompare(String(B[i])); }
    return 0; });
  let h = '<table class="t"><thead><tr><th>cell</th>' + cols.map(c => `<th>${esc(c)}</th>`).join('') +
    '<th class="num">repeat</th><th>status</th>' + sts.map(s => `<th class="num">${esc(s)}</th>`).join('') + '</tr></thead><tbody>';
  keys.forEach(key => {
    const grp = groups.get(key).slice().sort((a, b) => (a.repeat || 0) - (b.repeat || 0));
    grp.forEach(c => {
      const m = c.m[path] || {};
      const lab = c.report ? `<a href="${esc(c.report)}">${esc(c.short)}</a>` : esc(c.short);
      h += `<tr class="${c.hollow ? 'excluded' : ''}"><td>${lab}</td>` + cols.map(v => `<td>${esc(c.vars[v])}</td>`).join('') +
        `<td class="num">${esc(c.repeat)}</td><td>${esc(c.status)}${c.excluded ? ' (excluded)' : ''}</td>` +
        sts.map(s => `<td class="num">${esc(fmt(m[s], path))}</td>`).join('') + '</tr>';
    });
    const inc = grp.filter(c => !c.hollow);
    h += `<tr class="summary"><td>median of ${inc.length} included repeat${inc.length === 1 ? '' : 's'}</td>` +
      cols.map(v => `<td>${esc(grp[0].vars[v])}</td>`).join('') + '<td></td><td></td>' +
      sts.map(s => `<td class="num">${esc(fmt(median(inc.map(c => (c.m[path] || {})[s])), path))}</td>`).join('') + '</tr>';
  });
  h += '</tbody></table>';
  $('table').innerHTML = h;
}

// --- cells tab ---
(function(){
  let h = '<table class="t"><thead><tr><th class="num">#</th><th>cell</th><th>kind</th><th>status</th><th>excluded</th>' +
    '<th>gates</th><th>encoder</th><th>band A</th><th>band B</th><th>PTP</th><th>metrics</th></tr></thead><tbody>';
  D.cells.forEach(c => {
    const lab = c.report ? `<a href="${esc(c.report)}">${esc(c.label)}</a>` : esc(c.label);
    h += `<tr class="${c.hollow ? 'excluded' : ''}"><td class="num">${esc(c.index)}</td><td>${lab}</td><td>${esc(c.kind)}</td>` +
      `<td class="${c.status === 'OK' ? '' : 'bad'}">${esc(c.status)}</td><td>${c.excluded ? esc(c.reasons.join('; ') || 'yes') : ''}</td>` +
      `<td class="${c.gates_failed.length ? 'bad' : ''}">${esc(c.gates)}${c.gates_failed.length ? ' failed: ' + esc(c.gates_failed.join(', ')) : ''}</td>` +
      `<td class="${c.nvenc ? '' : 'bad'}">${esc(c.encoder)}${c.nvenc ? '' : ' (NOT NVENC)'}</td>` +
      `<td>${esc(c.band_a)}</td><td>${esc(c.band_b)}</td><td>${esc(c.ptp)}</td><td>${c.has_metrics ? 'yes' : '<span class="bad">missing</span>'}</td></tr>`;
  });
  $('cells-table').innerHTML = h + '</tbody></table>';
})();
render();
})();
"""


def build_html(grid_id: str, cells: list[GridCell], axes: list[str]) -> str:
    data = _html_data(grid_id, cells, axes)
    excl = [c for c in cells if c.excluded]
    excl_html = (H.table(["cell", "status", "reasons"], [[c.label, c.status, "; ".join(c.reasons()) or "–"] for c in excl])
                 if excl else '<p class="note">No cell is excluded.</p>')
    counts: dict[str, int] = {}
    for c in cells:
        counts[c.status] = counts.get(c.status, 0) + 1
    body = f"""
<div class="tabs" role="tablist">
  <button data-tab="tab-compare" aria-selected="true">Compare</button>
  <button data-tab="tab-cells" aria-selected="false">Cells ({len(cells)})</button>
</div>
<div class="tab" id="tab-compare">
<section>
  <div class="controls">
    <label>KPI <select id="kpi"></select></label>
    <label>statistic <select id="stat"></select></label>
    <label>x variable <select id="xvar"></select></label>
    <label>facet by <select id="facet"></select></label>
  </div>
  <div class="legend"><span class="dot"></span>included cell &nbsp; <span class="dot h"></span>excluded or INCOMPLETE
    &nbsp; — line: median of the included cells at each x (nearest rank; pools any axis not shown).
    Click a point to open its report.</div>
  <div id="panels" class="panels"></div>
  <p class="note" id="missing"></p>
</section>
<section><h2>Per cell</h2><div id="table"></div></section>
<section><h2>Excluded cells ({len(excl)})</h2>{excl_html}</section>
</div>
<div class="tab hidden" id="tab-cells"><section><h2>All cells</h2><div id="cells-table"></div></section></div>
<div id="tip"></div>
"""
    sub = f"{len(cells)} cells · " + " · ".join(f"{k} {v}" for k, v in sorted(counts.items())) + \
          f" · axes: {', '.join(axes) or 'none'}"
    return H.page(f"Grid {grid_id} — comparison", sub, body, extra_css=HTML_CSS,
                  script=H.json_script(data, "data") + f"<script>{HTML_JS}</script>")


# ======================================================================================
# entry point
# ======================================================================================

def render(grid_dir) -> Path:
    """Write comparison/metrics.csv, comparison.html, comparison.pdf; returns the comparison dir."""
    gd = Path(grid_dir)
    out = gd / "comparison"
    out.mkdir(parents=True, exist_ok=True)
    cells = load_cells(gd)
    grid_id = next((str(c.manifest["grid_id"]) for c in cells if c.manifest.get("grid_id")), gd.name)
    axes = grid_axes(gd, cells)
    write_metrics_csv(out / "metrics.csv", cells)
    doc = build_html(grid_id, cells, axes)
    H.atomic_write(out / "comparison.html", lambda t: t.write_text(doc, encoding="utf-8"))
    figs = build_pdf_figures(grid_id, cells, axes)

    def write_pdf(tmp: Path) -> None:
        with PdfPages(tmp) as pdf:
            for i, (fig, note) in enumerate(figs, 1):
                _footer(fig, i, len(figs), note)
                pdf.savefig(fig)
                plt.close(fig)
            pdf.infodict()["Title"] = f"Grid {grid_id} comparison"

    H.atomic_write(out / "comparison.pdf", write_pdf)
    return out


if __name__ == "__main__":
    import sys

    for arg in sys.argv[1:]:
        print(render(arg))
