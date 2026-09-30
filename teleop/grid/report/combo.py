"""One combination's repeats side by side: <combo>/summary.pdf and <combo>/summary.html.

    render(combo_dir) -> Path      # summary.pdf (and summary.html) in combo_dir; returns the PDF

Reads <combo>/r<n>/{manifest.json, metrics.json, reduced/frames.csv, reduced/control.csv} for
every repeat directory present (layout v2, CONTRACT.md). Works with 1..N repeats, with a repeat
that has no metrics.json or no reduced tables, and with repeats that grid.yaml planned but that
have not run yet (listed as "not run").

Page 1: flags per repeat (INCOMPLETE, not NVENC, PTP not locked, codec fallback, ...) and every
KPI with mean/p50/p95/p99/max per repeat and the median across the counted repeats (status OK,
not excluded -- the same rule as analysis.html). Page 2: small multiples over time, one column
per repeat, one row each for network, glass-to-glass, QP per frame and the control
path's network, the y axis shared along a row.
"""
from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from matplotlib.backends.backend_pdf import PdfPages  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402
from matplotlib.patches import Rectangle  # noqa: E402

from .. import stats  # noqa: E402
from . import analysis as A  # noqa: E402
from . import grid as G  # noqa: E402
from . import html as H  # noqa: E402
from . import style as S  # noqa: E402
from .cell import read_columns  # noqa: E402

STATS = ("mean", "p50", "p95", "p99", "max")
QP_SCALE = A.QP_SCALE


class Repeat:
    """One r<n>/ directory (or a planned one that has not run: cell is None)."""

    def __init__(self, n: int, cell: G.GridCell | None, d: Path):
        self.n, self.cell, self.dir = n, cell, d
        self.flags = A.repeat_flags(cell) if cell is not None else ["not_run"]
        self.counted = bool(cell is not None and cell.metrics is not None and not cell.excluded and cell.status == "OK")
        self._frames = self._control = None

    @property
    def status(self) -> str:
        return self.cell.status if self.cell is not None else "not run"

    def value(self, path: str, stat: str):
        if path.startswith("derived."):
            return A.derived_value(self.cell, path) if stat == "value" else None
        return self.cell.value(path, stat) if self.cell is not None else None

    def frames(self):
        if self._frames is None and self.cell is not None:
            self._frames = read_columns(self.dir / "reduced" / "frames.csv", ("t_s", "owd", "e2e", "qp"))
        return self._frames

    def control(self):
        if self._control is None and self.cell is not None:
            self._control = read_columns(self.dir / "reduced" / "control.csv", ("t_s", "owd_ms", "received"))
        return self._control


def load(combo_dir: Path) -> tuple[list[Repeat], dict]:
    cd = Path(combo_dir)
    reps: dict[int, Repeat] = {}
    for d in cd.iterdir():
        if d.is_dir() and G.REPEAT_DIR.match(d.name):
            n = int(d.name[1:])
            reps[n] = Repeat(n, G._load_cell(d, cd.name, n), d)
    doc = G.read_grid_yaml(cd.parent)
    planned = (doc.get("defaults") or {}).get("repeats") if isinstance(doc.get("defaults"), dict) else None
    if isinstance(planned, int):
        for n in range(1, planned + 1):
            reps.setdefault(n, Repeat(n, None, cd / f"r{n}"))
    return [reps[k] for k in sorted(reps)], doc


def _codec(reps: list[Repeat]) -> str:
    for r in reps:
        if r.cell is not None and r.cell.variables.get("codec"):
            return str(r.cell.variables["codec"])
    return ""


def _qp_path(reps: list[Repeat]) -> str:
    """Per-frame QP when any repeat has it, else A's per-second QP (as analysis.html does)."""
    if any(r.value("encoder.qp_per_frame", "n") for r in reps):
        return "encoder.qp_per_frame"
    if any(r.value("encoder.qp", "n") for r in reps):
        return "encoder.qp"
    return "encoder.qp_per_frame"


def _kpi_path(k, qp_path: str) -> str:
    return qp_path if k[0] == "qp" else k[1]


def _med(vals):
    v = [x for x in vals if S.is_num(x)]
    return stats.summ(v)["p50"] if v else None


def _fmt(v, unit: str) -> str:
    if not S.is_num(v):
        return "–"
    if unit == "share":
        return f"{100 * v:.2f}%"
    if unit in ("packets", "samples") and float(v).is_integer():
        return f"{int(v):,}"
    return S.fmt(v)


def _flag_rows(reps: list[Repeat]) -> list[list[str]]:
    rows = []
    for r in reps:
        c = r.cell
        if c is None:
            rows.append([f"r{r.n}", "not run", "–", "–", "–", "planned in grid.yaml; no directory yet"])
            continue
        req = str(c.variables.get("codec") or "–")
        neg = c.cfg("negotiated.codec")
        fl = [A.FLAG_TEXT.get(f, f) for f in r.flags]
        rows.append([f"r{r.n}", c.status + ("" if r.counted else "  (not counted)"),
                     c.encoder + ("" if c.nvenc else "  NOT NVENC"),
                     c.ptp(), f"{req} → {neg or '?'}", ", ".join(fl) or "–"])
    return rows


def _flag_banner(reps: list[Repeat]) -> list[str]:
    out = []
    for r in reps:
        bad = [A.FLAG_TEXT.get(f, f) for f in r.flags if f in A.SERIOUS_FLAGS]
        if bad:
            why = "; ".join(r.cell.reasons()[:2]) if r.cell is not None else ""
            out.append(f"r{r.n}: {', '.join(bad)}" + (f" — {why}" if why else ""))
    return out


# ======================================================================================
# page 1: flags and the KPI tables
# ======================================================================================

def _band(fig, title: str, sub: str) -> None:
    fig.patches.append(Rectangle((0, 0.925), 1, 0.075, transform=fig.transFigure, color=S.BAND, zorder=-1))
    size = 15.0
    while size > 9 and len(title) * size * 0.0072 > 10.2:
        size -= 0.5
    fig.text(0.03, 0.968, title, color=S.BAND_INK, fontsize=size, weight="bold", va="center")
    fig.text(0.03, 0.940, sub, color=S.BAND_INK_2, fontsize=8, va="center")


def _table(fig, x, y_top, w, head, rows, widths, *, num_from=1, fs=6.6, lh=0.0205, bold_cols=(), group_head=None,
           colours=None):
    """Figure-coordinate table; returns the y below it. group_head: [(label, first col, last col)]."""
    tot = sum(widths)
    xs, cx = [], x
    for wd in widths:
        xs.append(cx)
        cx += w * wd / tot
    rights = [xs[i] + w * widths[i] / tot - 0.003 for i in range(len(widths))]
    y = y_top
    if group_head:
        for lab, a, b in group_head:
            fig.text((xs[a] + rights[b]) / 2, y - lh * 0.4, lab, fontsize=fs, weight="bold", color=S.INK, ha="center",
                     va="center")
            fig.add_artist(Line2D([xs[a] + 0.004, rights[b]], [y - lh * 0.85] * 2, transform=fig.transFigure,
                                  color=S.BASELINE, lw=0.6))
        y -= lh
    for i, hd in enumerate(head):
        fig.text(rights[i] if i >= num_from else xs[i] + 0.003, y - lh * 0.4, hd, fontsize=fs - 0.3, color=S.INK_2,
                 weight="bold", ha="right" if i >= num_from else "left", va="center")
    fig.add_artist(Line2D([x, x + w], [y - lh * 0.8] * 2, transform=fig.transFigure, color=S.BASELINE, lw=0.7))
    y -= lh
    for r_i, row in enumerate(rows):
        if r_i % 2 == 0:
            fig.patches.append(Rectangle((x, y - lh * 0.95), w, lh, transform=fig.transFigure, color=S.PANEL, zorder=0,
                                         lw=0))
        for i, v in enumerate(row):
            colr = (colours[r_i][i] if colours and colours[r_i] and colours[r_i][i] else S.INK)
            kw = dict(fontsize=fs, color=colr, va="center", weight="bold" if i in bold_cols else "normal")
            if i >= num_from:
                fig.text(rights[i], y - lh * 0.45, str(v), ha="right", **kw)
            else:
                fig.text(xs[i] + 0.003, y - lh * 0.45, str(v)[:70], **kw)
        y -= lh
    return y


def kpi_tables(reps: list[Repeat]) -> tuple[list, list]:
    """(summary rows, scalar rows). A summary row: [label, unit, {stat: [per repeat..., median]}];
    a scalar row: [label, unit, [per repeat..., median]]."""
    qp_path = _qp_path(reps)
    counted = [r for r in reps if r.counted]
    summ_rows, scal_rows = [], []
    for k in A.KPIS:
        kid, _, label, unit, kind = k[0], k[1], k[2], k[3], k[4]
        path = _kpi_path(k, qp_path)
        if kid == "qp" and path == "encoder.qp":
            label = "QP per second (A; no per-frame QP)"
        if kid == "qp_s" and qp_path == "encoder.qp":
            continue
        if kind == "summary":
            per = {st: [r.value(path, st) for r in reps] + [_med([r.value(path, st) for r in counted])] for st in STATS}
            summ_rows.append([label, unit, per])
        else:
            vals = [r.value(path, "value") for r in reps]
            scal_rows.append([label, unit, vals + [_med([r.value(path, "value") for r in counted])]])
    return summ_rows, scal_rows


def page_summary(combo_dir: Path, reps: list[Repeat], title: str, sub: str):
    fig = plt.figure(figsize=S.PAGE_SIZE)
    _band(fig, title, sub)
    y = 0.905
    for line in _flag_banner(reps)[:4]:
        fig.patches.append(Rectangle((0.03, y - 0.027), 0.94, 0.025, transform=fig.transFigure, color=S.CRITICAL,
                                     alpha=0.13, zorder=0))
        fig.patches.append(Rectangle((0.03, y - 0.027), 0.004, 0.025, transform=fig.transFigure, color=S.CRITICAL))
        fig.text(0.042, y - 0.0145, line[:170], fontsize=7.6, weight="bold", color=S.INK, va="center")
        y -= 0.03
    fr = _flag_rows(reps)
    cols = [[None, S.CRITICAL if r.status != "OK" else None, None if (r.cell is None or r.cell.nvenc) else S.CRITICAL,
             S.CRITICAL if "ptp_unlocked" in r.flags else None, S.CRITICAL if "codec_fallback" in r.flags else None,
             S.CRITICAL if set(r.flags) & A.SERIOUS_FLAGS else None] for r in reps]
    y = _table(fig, 0.03, y - 0.005, 0.94, ["repeat", "status", "encoder", "PTP", "codec requested → negotiated", "flags"],
               fr, [0.6, 1.6, 2.2, 0.9, 1.8, 3.4], num_from=99, fs=6.8, lh=0.021, colours=cols)
    counted = [r for r in reps if r.counted]
    srows, vrows = kpi_tables(reps)
    names = [f"r{r.n}" for r in reps]
    ncol = len(reps) + 1
    groups_all = [STATS[:3], STATS[3:]] if 5 * ncol > 26 else [STATS]
    y -= 0.012
    fig.text(0.03, y, f"Distributions — per repeat and the median of the {len(counted)} counted repeat"
             f"{'s' if len(counted) != 1 else ''} (bold)", fontsize=8.5, weight="bold", va="top")
    y -= 0.022
    for grp in groups_all:
        head = ["KPI"] + [n for _ in grp for n in names + ["med"]]
        rows = []
        for label, unit, per in srows:
            rows.append([f"{label} ({'%' if unit == 'share' else unit})"] +
                        [_fmt(v, unit) for st in grp for v in per[st]])
        gh = [(st, 1 + i * ncol, (i + 1) * ncol) for i, st in enumerate(grp)]
        bold = {1 + i * ncol + len(reps) for i in range(len(grp))}
        widths = [3.3] + [0.62] * (len(grp) * ncol)
        y = _table(fig, 0.03, y, 0.94, head, rows, widths, fs=6.2 if ncol > 4 else 6.6, lh=0.0192, bold_cols=bold,
                   group_head=gh)
        y -= 0.01
    fig.text(0.03, y, "Scalars", fontsize=8.5, weight="bold", va="top")
    y -= 0.02
    rows = [[f"{label} ({'%' if unit == 'share' else unit})"] + [_fmt(v, unit) for v in vals] for label, unit, vals in vrows]
    _table(fig, 0.03, y, 0.62, ["KPI"] + names + ["median"], rows, [3.3] + [0.8] * ncol, fs=6.6, lh=0.0192,
           bold_cols={ncol})
    return fig, srows, vrows


# ======================================================================================
# page 2: small multiples over time
# ======================================================================================

def _series(r: Repeat, what: str):
    """(t, y) arrays for one repeat, or None."""
    if what == "control":
        t = r.control()
        if t is None or not len(t):
            return None
        rec = t.col("received") == 1
        return t.col("t_s")[rec], t.col("owd_ms")[rec]
    t = r.frames()
    if t is None or not len(t) or not t.resolve(what):
        return None
    return t.col("t_s"), t.col(what)


ROWS = [("owd", "Network", "ms", "latency.owd"),
        ("e2e", "Glass-to-glass (e2e)", "ms", "latency.e2e"),
        ("qp", "QP per frame", "QP", "encoder.qp_per_frame"),
        ("control", "Control path network", "ms", "control.owd")]


def page_multiples(reps: list[Repeat], title: str, codec: str):
    fig = plt.figure(figsize=S.PAGE_SIZE)
    _band(fig, title, "over time, one column per repeat · y shared along a row · lines: p50 (dotted) and p99 "
                      "(dash-dot) of that repeat")
    n = max(1, len(reps))
    left, right, top, bot = 0.075, 0.985, 0.885, 0.06
    cw = (right - left) / n
    rh = (top - bot) / len(ROWS)
    for ri, (what, lab, unit, path) in enumerate(ROWS):
        data = [_series(r, what) for r in reps]
        finite = [y[np.isfinite(y)] for d in data if d is not None for y in [d[1]] if np.isfinite(d[1]).any()]
        if finite:
            allv = np.concatenate(finite)
            p99s = [r.value(path, "p99") for r in reps if S.is_num(r.value(path, "p99"))]
            if what == "qp":
                lo, hi = float(allv.min()) - 2, float(allv.max()) + 2
            else:
                cap = max(p99s) * 1.5 if p99s else float(np.percentile(allv, 99)) * 1.5
                lo, hi = 0.0, max(cap, 1.0)
        for ci, (r, d) in enumerate(zip(reps, data)):
            ax = fig.add_axes([left + ci * cw + 0.012, top - (ri + 1) * rh + 0.035, cw - 0.03, rh - 0.065])
            p99 = r.value(path, "p99")
            head = f"r{r.n} · {lab}" if ri == 0 else lab
            ax.set_title(head + (f" · p99 {S.fmt(p99)}" if S.is_num(p99) else ""), fontsize=7.2, pad=2)
            if d is None or not np.isfinite(d[1]).any():
                why = ("not run" if r.cell is None else "no metrics" if r.cell.metrics is None else
                       "no per-frame QP" if what == "qp" else "no control log" if what == "control" else
                       "no reduced/frames.csv")
                ax.text(0.5, 0.5, why, transform=ax.transAxes, ha="center", va="center", color=S.MUTED, fontsize=7.5)
                ax.set_xticks([])
                ax.set_yticks([])
                ax.grid(False)
                continue
            t, y = d
            m = np.isfinite(t) & np.isfinite(y)
            t, y = t[m], y[m]
            over = int((y > hi).sum())
            colr = S.SLOTS[0] if what != "e2e" else S.SLOTS[1]
            if what == "control":
                colr = S.SLOTS[2]
            ax.scatter(t, np.minimum(y, hi), s=1.2 if len(t) > 3000 else 2.5, color=colr, linewidths=0, rasterized=True)
            for st, ls in (("p50", ":"), ("p99", "-.")):
                v = r.value(path, st)
                if S.is_num(v):
                    ax.axhline(min(v, hi), color=S.INK_2, lw=0.7, ls=ls)
            ax.set_ylim(lo, hi)
            if len(t):
                ax.set_xlim(float(t.min()), float(t.max()))
            if over:
                ax.text(0.99, 0.97, f"{over:,} above {hi:.0f}", transform=ax.transAxes, ha="right", va="top",
                        fontsize=6, color=S.INK_2)
            if not r.counted:
                ax.text(0.01, 0.97, r.status if r.status != "OK" else "not counted", transform=ax.transAxes, va="top",
                        fontsize=6.5, color=S.CRITICAL, weight="bold")
            if ci == 0:
                ax.set_ylabel(unit if what != "qp" else (QP_SCALE.get(codec, "QP")), fontsize=6.5)
            if ri == len(ROWS) - 1:
                ax.set_xlabel("seconds since epoch", fontsize=6.5)
            ax.tick_params(labelsize=6)
    return fig


# ======================================================================================
# html + entry point
# ======================================================================================

def _html_tables(reps, srows, vrows) -> str:
    names = [f"r{r.n}" for r in reps]
    th1 = "<tr><th rowspan=2>KPI</th>" + "".join(f'<th colspan="{len(names) + 1}" class="grp">{st}</th>' for st in STATS) + "</tr>"
    th2 = "<tr>" + "".join("".join(f'<th class="num">{n}</th>' for n in names) + '<th class="num">med</th>'
                           for _ in STATS) + "</tr>"
    body = []
    for label, unit, per in srows:
        tds = "".join("".join(f'<td class="num">{H.esc(_fmt(v, unit))}</td>' for v in per[st][:-1])
                      + f'<td class="num med">{H.esc(_fmt(per[st][-1], unit))}</td>' for st in STATS)
        body.append(f"<tr><td>{H.esc(label)} ({H.esc('%' if unit == 'share' else unit)})</td>{tds}</tr>")
    t1 = f'<table class="t mx"><thead>{th1}{th2}</thead><tbody>{"".join(body)}</tbody></table>'
    rows = [[f"{label} ({'%' if unit == 'share' else unit})"] + [_fmt(v, unit) for v in vals] for label, unit, vals in vrows]
    t2 = H.table(["KPI"] + names + ["median"], rows, num_cols=set(range(1, len(names) + 2)))
    return t1, t2


CSS = """
table.mx th.grp { text-align: center; border-bottom: 1px solid var(--base); }
table.mx td.med { font-weight: 700; background: var(--panel); }
"""


def render(combo_dir) -> Path:
    """Write summary.pdf and summary.html into combo_dir; returns the PDF path."""
    cd = Path(combo_dir)
    reps, doc = load(cd)
    S.apply_rc()
    codec = _codec(reps)
    gid = next((str(r.cell.manifest.get("grid_id")) for r in reps if r.cell is not None and r.cell.manifest.get("grid_id")),
               cd.parent.name)
    axes = G.declared_axes(doc)
    v0 = next((r.cell.variables for r in reps if r.cell is not None and r.cell.variables), {})
    swept = " · ".join(A._var_fmt(a, v0.get(a)) for a in axes if a in v0)
    fmt = {"resolution": "{}", "kbps": "{} kbps", "duration_s": "{} s", "control_transport": "{}"}
    fixed = " · ".join(f.format(v0[k]) for k, f in fmt.items() if k in v0 and k not in axes)
    title = f"{gid} · {cd.name}"
    counted = [r for r in reps if r.counted]
    sub = (f"{swept or cd.name}  ·  {len([r for r in reps if r.cell is not None])} repeat(s) present, "
           f"{len(counted)} counted" + (f"  ·  {fixed}" if len(fixed) < 90 else ""))
    f1, srows, vrows = page_summary(cd, reps, title, sub)
    f2 = page_multiples(reps, title, codec)
    figs = [(f1, "Summary"), (f2, "Over time")]
    for i, (f, name) in enumerate(figs, 1):
        f.add_artist(Line2D([0.03, 0.97], [0.035, 0.035], transform=f.transFigure, color=S.GRIDLINE, lw=0.6))
        f.text(0.03, 0.018, "Median across repeats: nearest-rank p50 (stats.percentile) of the counted repeats "
               "(status OK, not excluded). QP: H.264/H.265 0–51, AV1 q-index 0–255.", fontsize=6.3, color=S.MUTED)
        f.text(0.97, 0.018, f"Page {i} of {len(figs)} · {name}", fontsize=6.3, color=S.MUTED, ha="right")

    def write_pdf(tmp: Path) -> None:
        with PdfPages(tmp) as pdf:
            for f, _ in figs:
                pdf.savefig(f)
            pdf.infodict()["Title"] = title
            pdf.infodict()["Subject"] = "teleop combination summary"

    H.atomic_write(cd / "summary.pdf", write_pdf)
    t1, t2 = _html_tables(reps, srows, vrows)
    banners = "".join(f'<div class="banner">{H.esc(b)}</div>' for b in _flag_banner(reps))
    links = " · ".join((f'<a href="r{r.n}/report.pdf">r{r.n} report.pdf</a>' if (r.dir / "report.pdf").is_file()
                        else f"r{r.n} (no report)") for r in reps)
    flag_t = H.table(["repeat", "status", "encoder", "PTP", "codec requested → negotiated", "flags"], _flag_rows(reps))
    body = (f'<p class="note">{links} · <a href="../comparison/analysis.html">grid analysis</a></p>{banners}'
            f"<section><h2>Repeats and flags</h2>{flag_t}</section>"
            f"<section><h2>Distributions</h2><p class=\"note\">Per repeat, and the median of the {len(counted)} counted "
            f"repeat(s) (bold, med).</p>{t1}</section>"
            f"<section><h2>Scalars</h2>{t2}</section>"
            f"<section><h2>Over time</h2>{H.fig_img(f2, 'small multiples over time')}</section>")
    page = H.page(title, sub, body, extra_css=CSS)
    H.atomic_write(cd / "summary.html", lambda tmp: tmp.write_text(page, encoding="utf-8"))
    for f, _ in figs:
        plt.close(f)
    return cd / "summary.pdf"


if __name__ == "__main__":
    import sys

    for arg in sys.argv[1:]:
        print(render(arg))
