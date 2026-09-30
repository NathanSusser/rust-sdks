"""Per-cell report: report.pdf and report.html from reduced/*.csv, metrics.json and manifest.json.

Ported from archive/local_video-scripts/generate_frame_report.py. Its four pages are kept
(overview with the frame funnel, per-segment breakdown, one-way network latency and
jitter, modem activity on both hosts) and fed from the reduced tables instead of raw CSVs;
four pages are added (frame size, wire rates, QP and codec timing, late-frame breakdown).

Every distribution shown carries mean, p50, p95, p99, max, min and n from stats.summ.
Never writes anywhere but the cell directory.
"""
from __future__ import annotations

import csv
import json
import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from matplotlib.backends.backend_pdf import PdfPages  # noqa: E402
from matplotlib.patches import Rectangle  # noqa: E402

from .. import stats  # noqa: E402
from . import html as H  # noqa: E402
from . import style as S  # noqa: E402

LATE_MS = 100.0
SPIKE_ROWS_FIRST = 22        # rows of the late-frame table on page 8
SPIKE_ROWS_CONT = 2 * 40     # two column groups of 40 on each continuation page
SPIKE_MAX_CONT_PAGES = 5     # beyond this, the full list is in report.html and spikes.csv
SEG_COLS = [f"{name}_ms" for name, _ in S.SEGMENTS]

MODEM_ARTIFACT_CODES = {"0xB881"}
MODEM_NAMED_CODES = {
    "0xB872": "NR L2 UL TB",
    "0xB873": "NR L2 UL BSR",
    "0xB881": "UL TB stats",
    "0xB883": "UL sched report",
    "0xB888": "PDSCH stats",
    "0xB97F": "ML1 meas DB",
}


# ======================================================================================
# loading
# ======================================================================================

def _num(v):
    if v is None:
        return math.nan
    s = str(v).strip()
    if not s or s.lower() in ("nan", "none", "null"):
        return math.nan
    try:
        return float(s)
    except ValueError:
        if s.lower() in ("true", "false"):
            return 1.0 if s.lower() == "true" else 0.0
        return math.nan


# The report's internal column names -> the other spellings accepted for them. reduce/
# writes the second form (see CONTRACT.md "Reduced tables"); the first is kept so a table
# written either way reads the same.
ALIASES: dict[str, tuple[str, ...]] = {
    "t_s": ("t",),
    "size_bytes": ("bytes",),
    "owd_ms": ("owd",), "e2e_ms": ("e2e",), "decode_ms": ("decode",), "render_ms": ("render",),
    **{f"{n}_ms": (n,) for n in ("app_to_wire_a", "emission_a", "in_flight", "arrival_b", "wire_to_app_b")},
    "bytes_a": ("a_bytes",), "packets_a": ("a_packets",), "bytes_b": ("b_bytes",), "packets_b": ("b_packets",),
    "padding_bytes_a": ("a_padding_bytes",),
    "qp_mean": ("qp",), "frames_sent": ("frames",),
    "dominant_segment": ("dominant",),
    "keyframe": ("key_frame", "is_keyframe"),
}


class Table:
    """A CSV as numeric columns (NaN where empty) plus the raw string rows."""

    def __init__(self, rows: list[dict[str, str]], fieldnames: list[str]):
        self.rows = rows
        self.fields = fieldnames
        self._cache: dict[str, np.ndarray] = {}

    def __len__(self) -> int:
        return len(self.rows)

    def resolve(self, name: str) -> str | None:
        if name in self.fields:
            return name
        return next((a for a in ALIASES.get(name, ()) if a in self.fields), None)

    def has(self, name: str) -> bool:
        return self.resolve(name) is not None and len(self.rows) > 0 and bool(np.isfinite(self.col(name)).any())

    def col(self, name: str) -> np.ndarray:
        if name not in self._cache:
            real = self.resolve(name)
            if real is None:
                self._cache[name] = np.full(len(self.rows), np.nan)
            else:
                self._cache[name] = np.array([_num(r.get(real)) for r in self.rows], dtype=float)
        return self._cache[name]

    def text(self, name: str) -> list[str]:
        real = self.resolve(name) or name
        return [(r.get(real) or "").strip() for r in self.rows]


def _read_csv(path: Path) -> Table | None:
    if not path.is_file():
        return None
    with path.open(newline="", encoding="utf-8", errors="replace") as f:
        lines = [ln for ln in f if not ln.startswith("#")]
    reader = csv.DictReader(lines)
    rows = list(reader)
    return Table(rows, list(reader.fieldnames or []))


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def dig(d, path: str, default=None):
    cur = d
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def is_summary(v) -> bool:
    return isinstance(v, dict) and "p50" in v and "n" in v


@dataclass
class ModemRates:
    host: str
    per_second: dict[str, dict[int, int]]     # code -> {t_s: count}
    host_minus_utc_s: float | None
    aligned: bool                              # t_s is relative to the cell epoch

    def totals(self) -> dict[int, int]:
        out: dict[int, int] = {}
        for counts in self.per_second.values():
            for s, c in counts.items():
                out[s] = out.get(s, 0) + c
        return out

    def top_codes(self, n: int) -> list[str]:
        vol = {c: sum(v.values()) for c, v in self.per_second.items()}
        return sorted(vol, key=lambda c: -vol[c])[:n]


def read_modem_rates(path: Path, host: str, epoch: float | None) -> ModemRates | None:
    """dlf-rates CSV (legacy header shape, or a t_s column). Never guesses where zero sits."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    probe_start: float | None = None
    offset: float | None = None
    for line in text[:6]:
        if not line.startswith("#"):
            break
        if (m := re.search(r"probe_start_ms=(\d+)", line)):
            probe_start = int(m.group(1)) / 1000.0
        elif (m := re.search(r"probe_start=([0-9.]+)", line)):
            probe_start = float(m.group(1))
        if (m := re.search(r"host_minus_utc_s=([-+]?[0-9.]+)", line)):
            offset = float(m.group(1))
        elif (m := re.search(r"host=modem([-+]?[0-9.]+)s", line)):
            offset = float(m.group(1))
    reader = csv.DictReader(ln for ln in text if not ln.startswith("#"))
    fields = reader.fieldnames or []
    per: dict[str, dict[int, int]] = {}
    if "t_s" in fields:
        shift, aligned = 0.0, True
        key = "t_s"
    elif "second_rel_probe" in fields and probe_start is not None:
        aligned = epoch is not None
        shift = (probe_start - epoch) if epoch is not None else 0.0
        key = "second_rel_probe"
    else:
        return None
    for row in reader:
        try:
            sec = int(math.floor(float(row[key]) + shift))
            cnt = int(float(row["count"]))
        except (KeyError, TypeError, ValueError):
            continue
        per.setdefault(row.get("code", "?"), {})[sec] = per.get(row.get("code", "?"), {}).get(sec, 0) + cnt
    if not per:
        return None
    return ModemRates(host, per, offset, aligned)


@dataclass
class Cell:
    dir: Path
    manifest: dict
    metrics: dict
    frames: Table | None
    seconds: Table | None
    spikes: Table | None
    modem: dict[str, ModemRates | None] = field(default_factory=dict)
    bands: dict[str, Table | None] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, cell_dir: Path) -> "Cell":
        cd = Path(cell_dir)
        red = cd / "reduced"
        man = _read_json(cd / "manifest.json")
        met = _read_json(cd / "metrics.json")
        c = cls(cd, man, met, _read_csv(red / "frames.csv"), _read_csv(red / "seconds.csv"),
                _read_csv(red / "spikes.csv"))
        epoch = c.epoch
        for h in ("a", "b"):
            p = red / f"dlf-rates-{h}.csv"
            c.modem[h] = read_modem_rates(p, h, epoch) if p.is_file() else None
            c.bands[h] = _read_csv(red / f"band-{h}.csv")
        if not man:
            c.notes.append("manifest.json missing or unreadable")
        if not met:
            c.notes.append("metrics.json missing or unreadable")
        if c.frames is None:
            c.notes.append("reduced/frames.csv missing")
        if c.seconds is None:
            c.notes.append("reduced/seconds.csv missing")
        return c

    # --- config accessors (manifest first, metrics.config second) --------------------
    def cfg(self, path: str, default=None):
        v = dig(self.manifest, path)
        if v is None:
            v = dig(self.metrics, "config." + path)
        return default if v is None else v

    @property
    def label(self) -> str:
        return str(self.cfg("label") or self.dir.name)

    @property
    def epoch(self) -> float | None:
        e = self.cfg("epoch")
        return float(e) if S.is_num(e) else None

    @property
    def variables(self) -> dict:
        return self.cfg("variables", {}) or {}

    @property
    def encoder(self) -> str:
        return str(self.cfg("negotiated.encoder_implementation") or "unknown")

    @property
    def encoder_is_nvenc(self) -> bool:
        flag = dig(self.metrics, "config.encoder_is_nvenc")
        if isinstance(flag, bool):
            return flag
        return "nvenc" in self.encoder.lower()

    def ptp_locked(self) -> tuple[bool | None, str]:
        """(locked, detail). Read from the manifest; never assumed. None = not recorded."""
        ptp = self.cfg("ptp", {}) or {}
        a, b = ptp.get("a") or {}, ptp.get("b") or {}
        explicit = [x.get("locked") for x in (a, b, ptp) if isinstance(x.get("locked"), bool)]
        integ = dig(self.metrics, "integrity.ptp_locked")
        detail = (f"A {a.get('state', '?')}; B {b.get('state', '?')}"
                  + (f", {b['servo_lines_30s']} servo lines/30 s" if "servo_lines_30s" in b else "")
                  + (f", offset {b['offset_ns']:,} ns" if S.is_num(b.get("offset_ns")) else ""))
        if not a and not b and not explicit:
            if isinstance(integ, bool):
                return integ, f"from metrics.integrity (manifest has no ptp block)"
            return None, "manifest has no PTP record"
        if explicit or isinstance(integ, bool):
            vals = explicit + ([integ] if isinstance(integ, bool) else [])
            return all(vals), detail
        b_ok = (str(b.get("state", "")).upper() == "SLAVE"
                and (b.get("servo_lines_30s") or 0) > 0
                and S.is_num(b.get("offset_ns")) and abs(b["offset_ns"]) < 10_000)
        a_ok = str(a.get("state", "")).upper() == "MASTER"
        return (a_ok and b_ok), detail

    def gates(self) -> dict[str, list[dict]]:
        g = self.cfg("gates", {}) or {}
        return {h: list(g.get(h) or []) for h in ("a", "b")}

    @property
    def excluded(self) -> bool:
        return bool(self.manifest.get("excluded_from_comparison") or dig(self.metrics, "integrity.excluded"))

    # --- data accessors --------------------------------------------------------------
    def summ(self, metric_path: str, frame_col: str | None = None, *, table: str = "frames") -> dict:
        """metrics.json Summary if present and non-empty; else computed from the reduced table."""
        m = dig(self.metrics, metric_path)
        if is_summary(m) and m.get("n"):
            return m
        t = self.frames if table == "frames" else self.seconds
        if frame_col and t is not None and t.resolve(frame_col):
            return stats.summ(t.col(frame_col).tolist())
        return m if is_summary(m) else stats.summ([])

    def fcol(self, name: str) -> np.ndarray:
        return self.frames.col(name) if self.frames is not None else np.array([])

    def scol(self, name: str) -> np.ndarray:
        return self.seconds.col(name) if self.seconds is not None else np.array([])

    def kbps(self) -> float | None:
        v = self.variables.get("kbps")
        if S.is_num(v):
            return float(v)
        v = dig(self.metrics, "rate.kbps_target")
        return float(v) if S.is_num(v) else None

    def fps(self) -> float | None:
        for v in (self.variables.get("fps"), self.cfg("negotiated.fps")):
            if S.is_num(v) and v > 0:
                return float(v)
        return None

    def spike_rows(self) -> list[dict]:
        """Every frame over LATE_MS one-way, from spikes.csv or derived from frames.csv."""
        out = []
        if self.spikes is not None and len(self.spikes):
            t = self.spikes
            cols = {k: t.col(k) for k in ("frame_id", "t_s", "owd_ms", "e2e_ms", "size_bytes", "size_ratio", "keyframe")}
            dom, kind = t.text("dominant_segment"), t.text("kind")
            for i in range(len(t)):
                out.append({k: cols[k][i] for k in cols} | {"dominant": dom[i] or "–", "kind": kind[i] or "–"})
            return sorted(out, key=lambda r: r["t_s"] if math.isfinite(r["t_s"]) else 1e18)
        if self.frames is None or not len(self.frames):
            return out
        owd = self.fcol("owd_ms")
        size = self.fcol("size_bytes")
        med = stats.summ(size.tolist())["p50"]
        segs = np.vstack([self.fcol(c) for c in SEG_COLS]) if SEG_COLS else None
        for i in np.nonzero(np.nan_to_num(owd, nan=-1) > LATE_MS)[0]:
            col = segs[:, i]
            dom = S.SEGMENTS[int(np.nanargmax(col))][0] if np.isfinite(col).any() else "–"
            ratio = size[i] / med if (med and math.isfinite(size[i])) else math.nan
            key = self.fcol("keyframe")[i] == 1
            kind = "key" if key else "large" if ratio >= 2 else "path" if dom == "in_flight" else "host"
            out.append({"frame_id": self.fcol("frame_id")[i], "t_s": self.fcol("t_s")[i], "owd_ms": owd[i],
                        "e2e_ms": self.fcol("e2e_ms")[i], "size_bytes": size[i], "size_ratio": ratio,
                        "keyframe": 1.0 if key else 0.0, "dominant": dom, "kind": kind})
        return out


# ======================================================================================
# drawing helpers
# ======================================================================================

class Page:
    """One landscape page with the title band; footer is added once the page count is known."""

    def __init__(self, cell: Cell, name: str, subtitle: str = ""):
        self.fig = plt.figure(figsize=S.PAGE_SIZE)
        self.name = name
        self.html_blocks: list[str] = []
        f = self.fig
        f.patches.append(Rectangle((0, 0.925), 1, 0.075, transform=f.transFigure, color=S.BAND, zorder=-1))
        title = cell.label
        size = 15.0
        while size > 9 and len(title) * size * 0.0072 > 10.2:
            size -= 0.5
        f.text(0.03, 0.968, title, color=S.BAND_INK, fontsize=size, weight="bold", va="center")
        f.text(0.03, 0.940, f"{name}" + (f"  ·  {subtitle}" if subtitle else ""), color=S.BAND_INK_2,
               fontsize=8, va="center")

    def footer(self, i: int, n: int, note: str = "") -> None:
        f = self.fig
        f.add_artist(plt.Line2D([0.03, 0.97], [0.035, 0.035], transform=f.transFigure, color=S.GRIDLINE, lw=0.6))
        f.text(0.03, 0.018, note[:180], fontsize=6.5, color=S.MUTED)
        f.text(0.97, 0.018, f"Page {i} of {n} · {self.name}", fontsize=6.5, color=S.MUTED, ha="right")

    def axes(self, rect, title: str = "", ylabel: str = "", xlabel: str = ""):
        ax = self.fig.add_axes(rect)
        if title:
            ax.set_title(title, pad=4)
        if ylabel:
            ax.set_ylabel(ylabel)
        if xlabel:
            ax.set_xlabel(xlabel)
        return ax

    def text(self, x, y, s, **kw):
        kw.setdefault("fontsize", 7.5)
        kw.setdefault("color", S.INK)
        return self.fig.text(x, y, s, **kw)

    def banner(self, y: float, text: str, color: str = S.CRITICAL, h: float = 0.032) -> None:
        f = self.fig
        f.patches.append(Rectangle((0.03, y), 0.94, h, transform=f.transFigure, color=color, alpha=0.14, zorder=0))
        f.patches.append(Rectangle((0.03, y), 0.004, h, transform=f.transFigure, color=color, zorder=1))
        f.text(0.042, y + h / 2, text, fontsize=8.5, weight="bold", color=S.INK, va="center")

    def table(self, x: float, y_top: float, w: float, headers, rows, widths, *, num_from: int = 1,
              fs: float = 7.2, lh: float = 0.024, highlight: list[bool] | None = None) -> float:
        """Draw a table in figure coordinates; returns the y below it. Numbers right-aligned."""
        f = self.fig
        tot = sum(widths)
        xs, cx = [], x
        for wd in widths:
            xs.append(cx)
            cx += w * wd / tot
        rights = [xs[i] + w * widths[i] / tot - 0.004 for i in range(len(widths))]
        f.add_artist(plt.Line2D([x, x + w], [y_top - lh * 0.75] * 2, transform=f.transFigure, color=S.BASELINE, lw=0.7))
        for i, h in enumerate(headers):
            if i >= num_from:
                f.text(rights[i], y_top - lh * 0.4, h, fontsize=fs - 0.4, color=S.INK_2, weight="bold", ha="right", va="center")
            else:
                f.text(xs[i] + 0.003, y_top - lh * 0.4, h, fontsize=fs - 0.4, color=S.INK_2, weight="bold", va="center")
        y = y_top - lh
        for r_i, row in enumerate(rows):
            if r_i % 2 == 0:
                f.patches.append(Rectangle((x, y - lh * 0.95), w, lh, transform=f.transFigure, color=S.PANEL, zorder=0, lw=0))
            bold = bool(highlight and r_i < len(highlight) and highlight[r_i])
            for i, v in enumerate(row):
                s = S.fmt(v) if (S.is_num(v) or v is None) else str(v)
                kw = dict(fontsize=fs, color=S.INK, va="center", weight="bold" if bold else "normal")
                if i >= num_from:
                    f.text(rights[i], y - lh * 0.45, s, ha="right", **kw)
                else:
                    f.text(xs[i] + 0.003, y - lh * 0.45, s[:60], **kw)
            y -= lh
        return y


def stat_row(name: str, s: dict, unit: str = "") -> list:
    s = s or {}
    return [f"{name}" + (f" ({unit})" if unit else "")] + [s.get(k) for k in S.STATS]


STAT_HEAD = ["", "mean", "p50", "p95", "p99", "max", "min", "n"]
STAT_W = [3.2, 1, 1, 1, 1, 1, 1, 1.1]


def stat_html(rows: list[list]) -> str:
    return H.table(STAT_HEAD, rows, num_cols=set(range(1, 8)))


def _no_data(ax, msg: str) -> None:
    ax.text(0.5, 0.5, msg, transform=ax.transAxes, ha="center", va="center", color=S.MUTED, fontsize=8)
    ax.set_xticks([])
    ax.set_yticks([])
    ax.grid(False)


def _finite(*arrs):
    m = np.ones(len(arrs[0]), dtype=bool)
    for a in arrs:
        m &= np.isfinite(a)
    return [a[m] for a in arrs]


def _series(ax, t, y, color, label, *, ycap=None, marker=False, rasterize=True, size=3.0):
    t, y = _finite(t, y)
    if not len(t):
        return 0
    over = 0
    if ycap is not None:
        over = int((y > ycap).sum())
        y = np.minimum(y, ycap)
    if marker:
        ax.scatter(t, y, s=size, color=color, label=label, linewidths=0, rasterized=rasterize, zorder=3 if size > 3 else 2)
    else:
        ax.plot(t, y, color=color, lw=0.8, label=label, rasterized=rasterize)
    return over


def _stat_lines(ax, s: dict, color=S.INK_2, keys=("p50", "p95", "p99", "max"), fmtu="{:.0f}"):
    """Horizontal reference lines for the tail statistics, labelled inside the axes."""
    styles = {"p50": ":", "p95": "--", "p99": "-.", "max": "-"}
    for k in keys:
        v = s.get(k)
        if not S.is_num(v):
            continue
        ax.axhline(v, color=color, lw=0.7, ls=styles.get(k, "-"), alpha=0.8)
        ax.text(0.995, v, f"{k} {fmtu.format(v)}", transform=ax.get_yaxis_transform(), ha="right",
                va="bottom", fontsize=6.5, color=S.INK_2, clip_on=True)


# ======================================================================================
# pages
# ======================================================================================

def _header_lines(c: Cell) -> tuple[list[tuple[str, str, str]], list[tuple[str, str, str]]]:
    """(key, value, colour) rows for the two header columns."""
    v = c.variables
    left: list[tuple[str, str, str]] = []
    right: list[tuple[str, str, str]] = []
    idx = c.cfg("index")
    left.append(("grid / cell", f"{c.cfg('grid_id', '–')}  ·  #{idx if idx is not None else '–'}  ·  "
                 f"{c.cfg('kind', '–')}  ·  repeat {c.cfg('repeat', '–')}", S.INK))
    order = ["codec", "kbps", "fps", "resolution", "width", "height", "bpp", "vbv_frames", "padding", "target_quality",
             "intra_refresh", "pin_bitrate", "duration_s"]
    shown = [f"{k}={v[k]}" for k in order if k in v and v[k] is not None]
    unset = [k for k in order if k in v and v[k] is None]
    line, lines = "", []
    for bit in shown:
        if line and len(line) + len(bit) + 2 > 70:
            lines.append(line)
            line = bit
        else:
            line = f"{line}  {bit}".strip()
    if line:
        lines.append(line)
    for i, ln in enumerate(lines or ["–"]):
        left.append(("variables" if i == 0 else "", ln, S.INK))
    if unset:
        left.append(("not recorded", ", ".join(unset), S.INK_2))
    extra = [k for k in v if k not in order and k not in ("clip", "lead_s")]
    if extra:
        left.append(("", "  ".join(f"{k}={v[k]}" for k in extra), S.INK))
    if v.get("clip"):
        left.append(("clip", os.path.basename(str(v["clip"])), S.INK))
    rq = c.cfg("requested", {}) or {}
    ng = c.cfg("negotiated", {}) or {}
    rqs = f"{rq.get('width', '?')}x{rq.get('height', '?')}"
    ngs = f"{ng.get('width', '?')}x{ng.get('height', '?')}"
    geo_ok = rqs == ngs and "?" not in ngs
    left.append(("geometry", f"requested {rqs}  →  negotiated {ngs}" + ("" if geo_ok else "   MISMATCH / UNKNOWN"),
                 S.INK if geo_ok else S.CRITICAL))
    enc = c.encoder
    left.append(("encoder", enc + ("" if c.encoder_is_nvenc else "   ⚠ NOT NVENC"),
                 S.INK if c.encoder_is_nvenc else S.CRITICAL))
    left.append(("epoch", f"{c.cfg('epoch_iso', '–')}  ({c.cfg('epoch', '–')})", S.INK))
    status = str(c.cfg("status", "–"))
    reason = c.cfg("status_reason") or ""
    left.append(("status", status + (f" — {reason}" if reason else "")
                 + ("   EXCLUDED from comparison" + (f": {c.manifest.get('exclusion_reason')}"
                                                     if c.manifest.get("exclusion_reason") else "")
                    if c.excluded else ""),
                 S.INK if status == "OK" and not c.excluded else S.CRITICAL))
    left.append(("commit", str(c.cfg("commit", "–")), S.INK))

    locked, detail = c.ptp_locked()
    right.append(("PTP", ("LOCKED  " if locked else "NOT LOCKED  " if locked is False else "NOT RECORDED  ") + detail,
                  S.INK if locked else S.CRITICAL))
    for h in ("a", "b"):
        ck = c.cfg(f"clock.{h}", {}) or {}
        off = ck.get("host_minus_utc_s")
        right.append((f"clock {h.upper()}", (f"host − UTC {off:+.3f} s" if S.is_num(off) else "not recorded")
                      + (f"  (spread {ck['spread_ms']:.1f} ms, {ck.get('servers', '?')} servers)"
                         if S.is_num(ck.get("spread_ms")) else ""), S.INK if S.is_num(off) else S.CRITICAL))
    for h in ("a", "b"):
        b = c.cfg(f"band.{h}", {}) or {}
        if b:
            sh = b.get("share")
            right.append((f"band {h.upper()}", f"{b.get('band', '?')}  ARFCN {b.get('arfcn', '?')}  PCI {b.get('pci', '?')}"
                          + (f"  ({100 * sh:.0f}% of records)" if S.is_num(sh) else ""), S.INK))
        else:
            right.append((f"band {h.upper()}", "not recorded", S.CRITICAL))
    for h, gl in c.gates().items():
        if not gl:
            right.append((f"gates {h.upper()}", "none recorded", S.CRITICAL))
            continue
        failed = [g.get("name", "?") for g in gl if not g.get("pass")]
        right.append((f"gates {h.upper()}", f"{len(gl) - len(failed)}/{len(gl)} passed"
                      + (f"  FAILED: {', '.join(failed)}" if failed else ""), S.CRITICAL if failed else S.INK))
    integ = c.metrics.get("integrity") or {}
    if integ:
        bits = [f"captures {'complete' if integ.get('captures_complete') else 'INCOMPLETE'}",
                f"mirror {'verified' if integ.get('mirror_verified') else 'NOT verified'}",
                f"resyncs {integ.get('reduce_resyncs', '–')}"]
        bad = not (integ.get("captures_complete") and integ.get("mirror_verified"))
        right.append(("integrity", "  ·  ".join(bits), S.CRITICAL if bad else S.INK))
        for r in (integ.get("reasons") or [])[:2]:
            right.append(("", str(r), S.CRITICAL))
    for n in c.notes:
        right.append(("missing", n, S.CRITICAL))
    return left, right


def page_overview(c: Cell) -> Page:
    p = Page(c, "Overview", "header, tail summary, latency over the cell, frame funnel")
    left, right = _header_lines(c)
    y0, lh = 0.895, 0.0215
    for col_x, rows in ((0.03, left), (0.51, right)):
        y = y0
        for k, v, colr in rows[:14]:
            p.text(col_x, y, k, color=S.INK_2, fontsize=7.2, va="top")
            p.text(col_x + 0.075, y, v if len(v) <= 72 else v[:71] + "…", color=colr, fontsize=7.2, va="top",
                   weight="bold" if colr == S.CRITICAL else "normal")
            y -= lh
    y_tab = y0 - lh * max(len(left[:14]), len(right[:14])) - 0.012
    rows = [
        stat_row("one-way (packetize → receive)", c.summ("latency.owd", "owd_ms"), "ms"),
        stat_row("end to end (to GPU complete)", c.summ("latency.e2e", "e2e_ms"), "ms"),
        stat_row("frame size", c.summ("frame.size_kb") if dig(c.metrics, "frame.size_kb.n")
                 else stats.summ((c.fcol("size_bytes") / 1000).tolist()), "kB"),
        stat_row("QP (higher is worse)", c.summ("encoder.qp", "qp_mean", table="seconds")),
    ]
    y_after = p.table(0.03, y_tab, 0.94, STAT_HEAD, rows, STAT_W, fs=7.2, lh=0.021)

    # latency over time
    top = y_after - 0.035
    ax = p.axes([0.07, 0.25, 0.9, max(0.12, top - 0.25)], "Latency over the cell", "ms")
    t, owd, e2e = c.fcol("t_s"), c.fcol("owd_ms"), c.fcol("e2e_ms")
    if len(t) and (np.isfinite(owd).any() or np.isfinite(e2e).any()):
        s_all = stats.summ(np.concatenate([owd[np.isfinite(owd)], e2e[np.isfinite(e2e)]]).tolist())
        cap = max(1.0, (s_all["p99"] or 1) * 1.4)
        o1 = _series(ax, t, e2e, S.E2E, "end to end", ycap=cap)
        o2 = _series(ax, t, owd, S.OWD, "one-way", ycap=cap)
        rec = c.fcol("received")
        lost_t = t[(rec == 0) & np.isfinite(t)]
        if len(lost_t):
            ax.vlines(lost_t, 0, cap * 0.05, color=S.CRITICAL, lw=0.6, label=f"not received ({len(lost_t):,})")
        ax.axhline(LATE_MS, color=S.MUTED, lw=0.6, ls="--")
        ax.set_ylim(0, cap * 1.05)
        ax.set_xlim(max(0.0, float(np.nanmin(t))), float(np.nanmax(t)))
        ax.set_xlabel("seconds since epoch")
        ax.legend(loc="upper left", ncol=3)
        if o1 or o2:
            ax.text(0.995, 0.98, f"{o1 + o2:,} points above {cap:.0f} ms drawn at the top", transform=ax.transAxes,
                    ha="right", va="top", fontsize=6.5, color=S.INK_2)
    else:
        _no_data(ax, "no per-frame latency in reduced/frames.csv")

    # frame funnel
    fr = c.metrics.get("frame") or {}
    stages = [("captured", fr.get("frames_captured"), None),
              ("encoded", fr.get("frames_encoded"), "dropped before encode"),
              ("sent", fr.get("frames_sent"), "lost in sender"),
              ("received", fr.get("frames_received"), "lost in network"),
              ("rendered", fr.get("frames_rendered"), "arrived, never drawn")]
    if not any(S.is_num(v) for _, v, _ in stages) and c.frames is not None:
        stages = [("sent", len(c.frames), None),
                  ("received", int(np.nansum(c.fcol("received"))), "lost in network"),
                  ("rendered", int(np.nansum(c.fcol("rendered"))), "arrived, never drawn")]
    stages = [s for s in stages if S.is_num(s[1])]
    ax = p.axes([0.12, 0.05, 0.85, 0.14], "Where frames were lost")
    ax.grid(False)
    if stages:
        top_v = max(v for _, v, _ in stages) or 1
        ys = np.arange(len(stages))[::-1]
        for yy, (name, v, note), prev in zip(ys, stages, [None] + stages[:-1]):
            ax.barh(yy, v / top_v, color=S.SLOTS[0] if prev else S.BAND, height=0.7)
            ax.text(-0.01, yy, name, ha="right", va="center", fontsize=7.2, color=S.INK_2)
            txt = f"{int(v):,}"
            if prev is not None and S.is_num(prev[1]):
                lost = int(prev[1]) - int(v)
                share = 100.0 * lost / prev[1] if prev[1] else 0.0
                txt += f"    {note}: {lost:,} ({share:.2f}%)"
            ax.text(v / top_v + 0.01, yy, txt, va="center", fontsize=7.2,
                    color=S.INK if not (prev and int(prev[1]) - int(v) > 0) else S.CRITICAL)
        drops = [f"{k.replace('_', ' ')} {fr[k]:,}" for k in ("dropped_pre_encode", "dropped_post_encode", "keyframes")
                 if S.is_num(fr.get(k))]
        if S.is_num(fr.get("fps_delivered")):
            drops.append(f"fps delivered {fr['fps_delivered']:.2f}")
        if drops:
            ax.text(1.62, ys[-1] - 0.9, "   ".join(drops), ha="right", va="center", fontsize=6.8, color=S.INK_2)
        ax.set_xlim(0, 1.62)
        ax.set_ylim(-1.3, len(stages) - 0.4)
    else:
        _no_data(ax, "no frame counts")
    ax.set_xticks([])
    ax.set_yticks([])
    for sp in ax.spines.values():
        sp.set_visible(False)

    p.html_blocks.append(_html_header(c))
    p.html_blocks.append("<h3>Tail summary</h3>" + stat_html(rows))
    return p


def page_segments(c: Cell) -> Page:
    p = Page(c, "Latency by segment", "per frame, joined by RTP timestamp and frame order")
    names = [("capture → packetize (A app)", "latency.capture_to_packetize", "capture_to_packetize_ms"),
             ("encode", "encoder.encode_ms", "encode_ms")]
    names += [(S.SEGMENT_LABEL[n], f"latency.{n}", f"{n}_ms") for n, _ in S.SEGMENTS]
    names += [("one-way (packetize → receive)", "latency.owd", "owd_ms"),
              ("end to end", "latency.e2e", "e2e_ms")]
    rows, seg_means = [], []
    for label, mp, colname in names:
        s = c.summ(mp, colname)
        if not s.get("n"):
            continue
        rows.append(stat_row(label, s, "ms"))
    for n, lab in S.SEGMENTS:
        s = c.summ(f"latency.{n}", f"{n}_ms")
        if S.is_num(s.get("mean")):
            seg_means.append((n, lab, s["mean"], s.get("p50")))
    y = p.table(0.03, 0.895, 0.94, STAT_HEAD, rows, STAT_W, fs=7.4, lh=0.027,
                highlight=[r[0].startswith(("one-way", "end to end")) for r in rows])
    if not rows:
        p.text(0.03, 0.86, "no segment data in metrics.json or frames.csv", color=S.MUTED)
    bar_h = max(0.10, min(0.22, y - 0.13))
    ax = p.axes([0.03, y - 0.06 - bar_h, 0.94, bar_h], "Mean time in each segment (bar width ∝ mean)")
    ax.grid(False)
    total = sum(m for _, _, m, _ in seg_means if m > 0)
    if seg_means and total > 0:
        x = 0.0
        for n, lab, m, _ in seg_means:
            w = max(m, 0) / total
            ax.barh(0, w, left=x, color=S.SEGMENT_COLOR[n], height=0.6, edgecolor=S.SURFACE, linewidth=1.5)
            if w > 0.06:
                ax.text(x + w / 2, 0, f"{m:.1f}", ha="center", va="center", fontsize=7, color="#ffffff", weight="bold")
            x += w
        ax.set_xlim(0, 1)
        ax.set_ylim(-1.25, 0.5)
        per_row = 4
        for i, (n, lab, m, med) in enumerate(seg_means):
            lx, ly = (i % per_row) / per_row, -0.7 - 0.35 * (i // per_row)
            ax.add_patch(Rectangle((lx, ly - 0.08), 0.012, 0.16, color=S.SEGMENT_COLOR[n]))
            ax.text(lx + 0.018, ly, f"{lab}  mean {m:.1f} · p50 {S.fmt(med)} ms", va="center", fontsize=7, color=S.INK)
        ax.set_ylim(-0.95 - 0.35 * ((len(seg_means) - 1) // per_row), 0.5)
        ax.text(1.0, 0.45, f"sum of segment means {total:.1f} ms", ha="right", va="top", fontsize=7, color=S.INK_2)
    else:
        _no_data(ax, "no segment means")
    ax.set_xticks([])
    ax.set_yticks([])
    for sp in ax.spines.values():
        sp.set_visible(False)
    p.html_blocks.append(stat_html(rows))
    return p


def page_network(c: Cell) -> Page:
    p = Page(c, "One-way network latency and jitter", "Host A packetize → Host B webrtc receive")
    locked, detail = c.ptp_locked()
    if locked is True:
        p.text(0.03, 0.895, f"Manifest records PTP as locked ({detail}); the hosts' common offset from UTC cancels "
                            "in the difference.", color=S.INK_2, fontsize=7.8, va="top")
        y = 0.865
        p.html_blocks.append(f'<p class="note">Manifest records PTP as locked ({H.esc(detail)}).</p>')
    else:
        msg = ("PTP NOT LOCKED per manifest" if locked is False else "PTP STATE NOT RECORDED in manifest") + \
              f" ({detail}) — cross-host one-way numbers on this page are not trustworthy."
        p.banner(0.862, msg)
        y = 0.845
        p.html_blocks.append(f'<div class="banner">{H.esc(msg)}</div>')
    owd_s = c.summ("latency.owd", "owd_ms")
    ia = c.summ("jitter.interarrival_rfc3550_ms")
    fib = c.summ("jitter.frame_interval_b_ms")
    sd = dig(c.metrics, "jitter.owd_sd_ms")
    if not S.is_num(sd):
        sd = stats.sd(c.fcol("owd_ms").tolist())
    rows = [stat_row("one-way", owd_s, "ms"), stat_row("interarrival jitter RFC 3550", ia, "ms"),
            stat_row("frame interval at B", fib, "ms")]
    y = p.table(0.03, y, 0.94, STAT_HEAD, rows, STAT_W, fs=7.4, lh=0.025)
    tl = c.metrics.get("tail") or {}
    bits = [f"one-way sd {S.fmt(sd)} ms"]
    for k in ("owd_over_100", "owd_over_150", "e2e_over_100", "e2e_over_150"):
        v = tl.get(k) or {}
        if S.is_num(v.get("count")):
            bits.append(f"{k.replace('_', ' ')}: {v['count']:,} ({S.pct(v.get('share'))})")
    p.text(0.03, y - 0.008, "   ·   ".join(bits), color=S.INK, fontsize=7.4, va="top")
    top = y - 0.05
    owd = c.fcol("owd_ms")
    owd_f = owd[np.isfinite(owd)]
    ax = p.axes([0.07, top - 0.30 + 0.0, 0.40, 0.27], f"Distribution ({len(owd_f):,} frames)", "frames", "ms")
    if len(owd_f):
        lo, hi = float(owd_f.min()), float(max(owd_s.get("p99") or owd_f.max(), owd_f.min() + 1)) * 1.3
        over = int((owd_f > hi).sum())
        ax.hist(np.clip(owd_f, lo, hi), bins=60, range=(lo, hi), color=S.OWD, edgecolor=S.SURFACE, linewidth=0.4)
        for k, ls in (("p50", ":"), ("p95", "--"), ("p99", "-.")):
            if S.is_num(owd_s.get(k)):
                ax.axvline(owd_s[k], color=S.INK_2, lw=0.7, ls=ls)
                ax.text(owd_s[k], 0.98, f" {k} {owd_s[k]:.0f}", transform=ax.get_xaxis_transform(), fontsize=6.5,
                        va="top", color=S.INK_2, clip_on=True)
        ax.set_xlim(lo, hi)
        if over:
            ax.text(0.99, 0.80, f"{over:,} frames > {hi:.0f} ms in last bin\nmax {owd_f.max():.0f} ms",
                    transform=ax.transAxes, ha="right", va="top", fontsize=6.5, color=S.INK_2)
    else:
        _no_data(ax, "no one-way latency")
    ax = p.axes([0.55, top - 0.30, 0.42, 0.27], "CCDF (share of frames above x)", "share", "ms")
    if len(owd_f):
        xs = np.sort(owd_f)
        ccdf = 1.0 - np.arange(len(xs)) / len(xs)
        ax.step(xs, ccdf, color=S.OWD, where="post", lw=1.0)
        ax.set_yscale("log")
        ax.set_ylim(max(1.0 / len(xs) / 2, 1e-5), 1.05)
        ax.axvline(LATE_MS, color=S.MUTED, lw=0.6, ls="--")
    else:
        _no_data(ax, "no one-way latency")
    ax = p.axes([0.07, 0.08, 0.90, top - 0.43], "Over the cell", "ms", "seconds since epoch")
    t = c.fcol("t_s")
    if len(owd_f):
        cap = max(1.0, (owd_s.get("p99") or 1) * 1.4)
        over = _series(ax, t, owd, S.OWD, "one-way", ycap=cap)
        ax.axhline(LATE_MS, color=S.MUTED, lw=0.6, ls="--")
        ax.set_ylim(0, cap * 1.05)
        if over:
            ax.text(0.995, 0.97, f"{over:,} frames above {cap:.0f} ms drawn at the top", transform=ax.transAxes,
                    ha="right", va="top", fontsize=6.5, color=S.INK_2)
    else:
        _no_data(ax, "no one-way latency")
    p.html_blocks.append(stat_html(rows) + f'<p class="note">{H.esc("   ·   ".join(bits))}</p>')
    return p


def page_modem(c: Cell) -> Page:
    p = Page(c, "Modem activity, both hosts", "per-second DIAG record counts (not grant sizes)")
    p.text(0.03, 0.895, "Per-second DIAG RECORD COUNTS, not grant sizes: this shows WHEN the modem's scheduling "
                        "activity changed, never the grant in bytes. Red ticks mark seconds holding a frame over "
                        f"{LATE_MS:.0f} ms one-way.", color=S.INK_2, fontsize=7.2, va="top")
    owd, t = c.fcol("owd_ms"), c.fcol("t_s")
    late_secs = sorted({int(math.floor(x)) for x, o in zip(t, owd) if math.isfinite(x) and math.isfinite(o) and o > LATE_MS})
    html = []
    y_top = 0.85
    strip_h = 0.15
    for h, color in (("a", S.HOST_A), ("b", S.HOST_B)):
        m = c.modem.get(h)
        mm = dig(c.metrics, f"modem.{h}", {}) or {}
        info = [f"activity index {_brief(mm.get('activity_index'))}", f"0x19EF records {_brief(mm.get('x19ef_records'))}"]
        if S.is_num(mm.get("kernel_queue_max_bytes")):
            info.append(f"kernel queue max {S.fmt(mm['kernel_queue_max_bytes'])} B")
        if S.is_num(mm.get("kernel_queue_max_pkts")):
            info.append(f"kernel queue max {S.fmt(mm['kernel_queue_max_pkts'])} pkts")
        for k, u in (("rsrp_dbm", "dBm"), ("snr_db", "dB")):
            s = mm.get(k) or {}
            if S.is_num(s.get("mean")):
                info.append(f"{k.split('_')[0].upper()} mean {s['mean']:.1f} min {S.fmt(s.get('min'))} {u}")
        ax = p.axes([0.07, y_top - strip_h, 0.90, strip_h], f"Host {h.upper()}: all DIAG records/s", "records/s")
        ax.text(1.0, 1.02, "   ".join(info), transform=ax.transAxes, ha="right", va="bottom", fontsize=6.5, color=S.INK_2)
        if m is None:
            _no_data(ax, f"no reduced/dlf-rates-{h}.csv (or no probe_start to align it)")
        else:
            tot = m.totals()
            secs = sorted(tot)
            vals = np.array([tot[s] for s in secs], dtype=float)
            interior = np.sort(vals[1:-1] if len(vals) > 3 else vals)
            lo_v = interior[int(len(interior) * 0.05)]
            hi_v = interior[min(len(interior) - 1, int(len(interior) * 0.95))]
            pad = max(1.0, (hi_v - lo_v) * 0.25)
            ax.plot(secs, vals, color=color, lw=0.8)
            ax.set_ylim(lo_v - pad, hi_v + pad)
            for s in late_secs:
                ax.axvline(s + 0.5, ymin=0, ymax=0.06, color=S.CRITICAL, lw=0.7)
            ax.text(0.005, 0.97, f"axis: p5–p95 of interior seconds; seen {int(vals.min()):,}–{int(vals.max()):,}/s"
                    + ("" if m.aligned else "   (NOT aligned to epoch: seconds since probe start)")
                    + (f"   clock {m.host_minus_utc_s:+.2f} s vs UTC" if m.host_minus_utc_s is not None else ""),
                    transform=ax.transAxes, va="top", fontsize=6.5, color=S.INK_2)
            tf = t[np.isfinite(t)]
            if len(tf):
                ax.set_xlim(float(tf.min()) - 2, float(tf.max()) + 2)
            ax.set_xlabel("seconds since epoch")
        y_top -= strip_h + 0.06
        html.append(f"<p class=\"note\">Host {h.upper()}: {H.esc('   '.join(info))}</p>")

    # band table + named codes at the late frames
    y = y_top - 0.005
    brow = []
    for h in ("a", "b"):
        bt = c.bands.get(h)
        if bt is None or not len(bt):
            b = c.cfg(f"band.{h}", {}) or {}
            if b:
                brow.append([f"Host {h.upper()} (manifest)", b.get("band", "–"), b.get("arfcn", "–"), b.get("pci", "–"), b.get("share")])
            continue
        combos: dict[tuple, int] = {}
        for r in bt.rows:
            k = (r.get("band", ""), r.get("arfcn", ""), r.get("pci", ""))
            combos[k] = combos.get(k, 0) + 1
        for k, n in sorted(combos.items(), key=lambda kv: -kv[1])[:3]:
            brow.append([f"Host {h.upper()}", k[0], k[1], k[2], n / len(bt)])
    brow = [r[:4] + [S.pct(r[4]) if S.is_num(r[4]) else "–"] for r in brow]
    p.text(0.03, y, "Serving cell", fontsize=9, weight="bold", va="top")
    yb = p.table(0.03, y - 0.025, 0.40, ["host", "band", "ARFCN", "PCI", "share"], brow or [["–", "not recorded", "", "", ""]],
                 [1.6, 0.8, 1.0, 0.7, 0.8], num_from=2, fs=7, lh=0.022)
    p.text(0.47, y, "Named codes at the late frames", fontsize=9, weight="bold", va="top")
    code_rows, verdict = _modem_code_rows(c, late_secs)
    if verdict:
        p.text(0.47, y - 0.028, verdict, fontsize=7, color=S.INK, va="top", wrap=True, weight="bold")
    if code_rows:
        p.table(0.47, y - (0.05 if verdict else 0.025), 0.50,
                ["host", "code", "meaning", "median/s ordinary", "median/s at late", "ratio"], code_rows,
                [0.5, 0.6, 1.3, 1.1, 1.1, 1.2], num_from=3, fs=6.8, lh=0.020)
    html.append("<h3>Serving cell</h3>" + H.table(["host", "band", "ARFCN", "PCI", "share"], brow, num_cols={2, 3, 4}))
    if code_rows:
        html.append("<h3>Named codes at the late frames</h3>"
                    + (f'<p class="note">{H.esc(verdict)}</p>' if verdict else "")
                    + H.table(["host", "code", "meaning", "median/s ordinary", "median/s at late", "ratio"], code_rows,
                              num_cols={3, 4, 5}))
    p.html_blocks.extend(html)
    del yb
    return p


def _brief(v) -> str:
    """A scalar, or a Summary shown as its tail (p50 / p99 / max)."""
    if is_summary(v):
        return f"p50 {S.fmt(v.get('p50'))} · p99 {S.fmt(v.get('p99'))} · max {S.fmt(v.get('max'))}"
    return S.fmt(v)


def _modem_code_rows(c: Cell, late_secs: list[int]) -> tuple[list[list], str]:
    rows: list[list] = []
    verdict = ""
    mods = [m for m in (c.modem.get("a"), c.modem.get("b")) if m is not None]
    if not mods:
        return rows, "no DLF rates for either host"
    if not late_secs:
        verdict = f"no frame over {LATE_MS:.0f} ms one-way: nothing to compare"
    lead = mods[0]
    span = max(1, len(lead.totals()))
    if late_secs and len(late_secs) / span > 0.5:
        return rows, ("NOT COMPARABLE: late frames touch more than half the seconds, so 'ordinary' and 'late' "
                      "overlap and every ratio would be near 1.00 by construction.")
    late = set(late_secs)
    for m in mods:
        codes = [k for k in MODEM_NAMED_CODES if k in m.per_second]
        codes += [k for k in m.top_codes(3) if k not in MODEM_NAMED_CODES]
        for code in codes:
            counts = m.per_second[code]
            ordinary = [v for s, v in counts.items() if s not in late]
            at_late = [counts.get(s, 0) for s in late if s in range(min(counts), max(counts) + 1)]
            if not ordinary:
                continue
            med_o = stats.summ(ordinary)["p50"]
            med_l = stats.summ(at_late)["p50"] if at_late else None
            if code in MODEM_ARTIFACT_CODES:
                ratio = "heartbeat"
            elif med_o < 5:
                ratio = "too sparse"
            elif med_l is None:
                ratio = "no late frames"
            else:
                ratio = f"{med_l / med_o:.2f}x" if med_o else "–"
            rows.append([f"Host {m.host.upper()}", code, MODEM_NAMED_CODES.get(code, "(high volume)"),
                         med_o, med_l, ratio])
    return rows[:14], verdict


def page_frame_size(c: Cell) -> Page:
    kbps, fps = c.kbps(), c.fps()
    budget_kb = (kbps / 8.0 / fps) if (kbps and fps) else None
    p = Page(c, "Frame size and packets per frame",
             f"budget = kbps/8/fps = {budget_kb:.2f} kB" if budget_kb else "budget unknown (kbps or fps missing)")
    t, size, pk = c.fcol("t_s"), c.fcol("size_bytes") / 1000.0, c.fcol("packets_a")
    key = c.fcol("keyframe") == 1
    s_size = c.summ("frame.size_kb") if dig(c.metrics, "frame.size_kb.n") else stats.summ(size.tolist())
    s_pk = c.summ("frame.packets_per_frame", "packets_a")
    rows = [stat_row("frame size", s_size, "kB"), stat_row("packets per frame (A wire)", s_pk)]
    y = p.table(0.03, 0.895, 0.94, STAT_HEAD, rows, STAT_W, fs=7.4, lh=0.025)
    spread = dig(c.metrics, "frame.size_spread_pct")
    if S.is_num(spread):
        p.text(0.03, y - 0.005, f"size spread (sd/mean) {spread:.1f}%", fontsize=7.2, color=S.INK_2, va="top")
    top = y - 0.04
    h1 = (top - 0.10) / 2 - 0.04
    ax = p.axes([0.07, top - h1, 0.55, h1], "Frame size over the cell", "kB")
    if np.isfinite(size).any():
        med = s_size.get("p50")
        cap = max([v for v in (s_size.get("p99"), budget_kb, (med or 0) * 2) if S.is_num(v)] + [1.0]) * 1.4
        over = _series(ax, np.where(~key, t, np.nan), size, S.SLOTS[0], "delta frames", ycap=cap, marker=True)
        over += _series(ax, np.where(key, t, np.nan), size, S.SLOTS[1], "keyframes", ycap=cap, marker=True, size=16)
        if budget_kb:
            ax.axhline(budget_kb, color=S.INK, lw=0.9, ls="--", label=f"budget {budget_kb:.1f} kB")
        if S.is_num(med):
            ax.axhline(2 * med, color=S.INK_2, lw=0.8, ls=":", label=f"2× median {2 * med:.1f} kB")
        ax.set_ylim(0, cap * 1.06)
        ax.legend(loc="upper left", ncol=4, markerscale=3)
        if over:
            ax.text(0.995, 0.02, f"{over:,} frames above {cap:.0f} kB drawn at {cap:.0f}", transform=ax.transAxes,
                    ha="right", va="bottom", fontsize=6.5, color=S.INK_2)
    else:
        _no_data(ax, "no frame sizes")
    ax = p.axes([0.07, 0.08, 0.55, h1], "Packets per frame over the cell (A wire)", "packets", "seconds since epoch")
    if np.isfinite(pk).any():
        _series(ax, np.where(~key, t, np.nan), pk, S.SLOTS[0], "delta frames", marker=True)
        _series(ax, np.where(key, t, np.nan), pk, S.SLOTS[1], "keyframes", marker=True, size=16)
        ax.set_ylim(0, max(1.0, float(np.nanmax(pk))) * 1.1)
        ax.legend(loc="upper left", ncol=2, markerscale=3)
    else:
        _no_data(ax, "no packets per frame")
    ax = p.axes([0.69, top - h1, 0.28, h1], "Size histogram", "frames", "kB")
    fs = size[np.isfinite(size)]
    if len(fs):
        hi = max(float(s_size.get("p99") or fs.max()) * 1.5, 1e-3)
        ax.hist(np.clip(fs, 0, hi), bins=50, range=(0, hi), color=S.SLOTS[0], edgecolor=S.SURFACE, linewidth=0.4)
        if budget_kb:
            ax.axvline(budget_kb, color=S.INK, lw=0.9, ls="--")
        ax.set_xlim(0, hi)
    else:
        _no_data(ax, "no frame sizes")
    ax = p.axes([0.69, 0.08, 0.28, h1], "Size CCDF", "share above x", "kB")
    if len(fs):
        xs = np.sort(fs)
        ax.step(xs, 1.0 - np.arange(len(xs)) / len(xs), where="post", color=S.SLOTS[0], lw=1.0)
        ax.set_yscale("log")
        ax.set_ylim(max(0.5 / len(xs), 1e-5), 1.05)
        if budget_kb:
            ax.axvline(budget_kb, color=S.INK, lw=0.9, ls="--")
            ax.text(budget_kb, 0.02, " budget", transform=ax.get_xaxis_transform(), fontsize=6.5, color=S.INK_2)
    else:
        _no_data(ax, "no frame sizes")
    p.html_blocks.append(stat_html(rows))
    return p


def page_rates(c: Cell) -> Page:
    p = Page(c, "Wire rates, Host A and Host B", "all RTP on each host's wwan0, per second")
    rt = c.metrics.get("rate") or {}
    rows = []
    for key, lab in (("bytes_per_s_a", "A bytes/s"), ("bytes_per_s_b", "B bytes/s"),
                     ("packets_per_s_a", "A packets/s"), ("packets_per_s_b", "B packets/s")):
        colname = {"bytes_per_s_a": "bytes_a", "bytes_per_s_b": "bytes_b",
                   "packets_per_s_a": "packets_a", "packets_per_s_b": "packets_b"}[key]
        rows.append(stat_row(lab, c.summ(f"rate.{key}", colname, table="seconds")))
    y = p.table(0.03, 0.895, 0.94, STAT_HEAD, rows, STAT_W, fs=7.4, lh=0.025)
    pad = rt.get("padding_bytes_share")
    if not S.is_num(pad) and c.seconds is not None and c.seconds.has("padding_bytes_a"):
        tot = np.nansum(c.scol("bytes_a"))
        pad = float(np.nansum(c.scol("padding_bytes_a")) / tot) if tot else None
    bits = [f"target {S.fmt(rt.get('kbps_target') or c.kbps())} kbps",
            f"achieved {S.fmt(rt.get('kbps_achieved'))} kbps",
            f"padding share {S.pct(pad) if S.is_num(pad) else 'unknown'}"
            + (f" ({rt['padding_note']})" if not S.is_num(pad) and rt.get("padding_note") else "")]
    p.text(0.03, y - 0.005, "   ·   ".join(bits), fontsize=7.4, va="top")
    top = y - 0.05
    h1 = (top - 0.10) / 2 - 0.04
    t = c.scol("t_s")
    ax = p.axes([0.07, top - h1, 0.90, h1], "Bitrate on the wire", "kbps")
    if len(t) and (c.seconds.has("bytes_a") or c.seconds.has("bytes_b")):
        ax.plot(t, c.scol("bytes_a") * 8 / 1000, color=S.HOST_A, lw=1.0, label="Host A (sent)")
        ax.plot(t, c.scol("bytes_b") * 8 / 1000, color=S.HOST_B, lw=1.0, label="Host B (received)")
        if c.seconds.has("padding_bytes_a"):
            ax.plot(t, c.scol("padding_bytes_a") * 8 / 1000, color=S.SLOTS[2], lw=0.8, label="A padding")
        if c.seconds.has("target_kbps"):
            ax.step(t, c.scol("target_kbps"), where="post", color=S.INK, lw=0.8, ls=":", label="encoder target")
        if c.kbps():
            ax.axhline(c.kbps(), color=S.INK, lw=0.9, ls="--", label=f"cap {c.kbps():.0f} kbps")
        ax.set_ylim(bottom=0)
        ax.legend(loc="lower left", ncol=5)
    else:
        _no_data(ax, "no per-second byte counts in reduced/seconds.csv")
    ax = p.axes([0.07, 0.08, 0.90, h1], "Packets on the wire", "packets/s", "seconds since epoch")
    if len(t) and (c.seconds.has("packets_a") or c.seconds.has("packets_b")):
        ax.plot(t, c.scol("packets_a"), color=S.HOST_A, lw=1.0, label="Host A (sent)")
        ax.plot(t, c.scol("packets_b"), color=S.HOST_B, lw=1.0, label="Host B (received)")
        ax.set_ylim(bottom=0)
        ax.legend(loc="lower left", ncol=2)
    else:
        _no_data(ax, "no per-second packet counts in reduced/seconds.csv")
    p.html_blocks.append(stat_html(rows) + f'<p class="note">{H.esc("   ·   ".join(bits))}</p>')
    return p


def page_qp(c: Cell) -> Page:
    p = Page(c, "Quality and codec timing", "QP per second from A's WebRTC stats; encode (A) and decode (B) per frame")
    s_qp = c.summ("encoder.qp", "qp_mean", table="seconds")
    s_enc = c.summ("encoder.encode_ms", "encode_ms")
    s_dec = c.summ("latency.decode", "decode_ms")
    rows = [stat_row("QP (higher is worse)", s_qp), stat_row("encode", s_enc, "ms"), stat_row("decode", s_dec, "ms")]
    y = p.table(0.03, 0.895, 0.94, STAT_HEAD, rows, STAT_W, fs=7.4, lh=0.025)
    ql = dig(c.metrics, "encoder.quality_limitation_s") or {}
    if ql:
        p.text(0.03, y - 0.005, "quality limitation (s): " + "   ".join(f"{k} {S.fmt(v)}" for k, v in ql.items()),
               fontsize=7.4, va="top")
    top = y - 0.05
    h1 = (top - 0.10) * 0.55
    ax = p.axes([0.07, top - h1, 0.90, h1], "QP over the cell (per second)", "QP")
    t, qp = c.scol("t_s"), c.scol("qp_mean")
    if np.isfinite(qp).any():
        ax.plot(t, qp, color=S.SLOTS[0], lw=1.0)
        _stat_lines(ax, s_qp, fmtu="{:.1f}")
        lo, hi = float(np.nanmin(qp)), max(float(np.nanmax(qp)), s_qp.get("max") or 0)
        ax.set_ylim(max(0, lo - 3), hi + 3)
    elif c.frames is not None and c.frames.has("qp"):
        _series(ax, c.fcol("t_s"), c.fcol("qp"), S.SLOTS[0], "per frame", marker=True)
        _stat_lines(ax, s_qp, fmtu="{:.1f}")
    else:
        _no_data(ax, "no QP in reduced/seconds.csv")
    h2 = top - h1 - 0.10 - 0.06
    for i, (colname, s, lab, colr) in enumerate((("encode_ms", s_enc, "Encode time (A)", S.SLOTS[0]),
                                                 ("decode_ms", s_dec, "Decode time (B)", S.SLOTS[1]))):
        ax = p.axes([0.07 + i * 0.47, 0.08, 0.43, h2], lab, "frames", "ms")
        v = c.fcol(colname)
        v = v[np.isfinite(v)]
        if len(v):
            hi = max(float(s.get("p99") or v.max()) * 1.5, 0.1)
            ax.hist(np.clip(v, 0, hi), bins=50, range=(0, hi), color=colr, edgecolor=S.SURFACE, linewidth=0.4)
            for k, ls in (("p50", ":"), ("p95", "--"), ("p99", "-.")):
                if S.is_num(s.get(k)):
                    ax.axvline(s[k], color=S.INK_2, lw=0.7, ls=ls)
                    ax.text(s[k], 0.97 - 0.08 * ("p50", "p95", "p99").index(k), f" {k} {s[k]:.1f}",
                            transform=ax.get_xaxis_transform(), fontsize=6.5, va="top", color=S.INK_2, clip_on=True)
            ax.set_xlim(0, hi)
            ax.text(0.99, 0.97, f"max {S.fmt(s.get('max'))} ms", transform=ax.transAxes, ha="right", va="top",
                    fontsize=6.5, color=S.INK_2)
        else:
            _no_data(ax, f"no {colname}")
    p.html_blocks.append(stat_html(rows))
    return p


def _spike_table_rows(spikes: list[dict]) -> list[list]:
    return [[r["t_s"], r["frame_id"], r["owd_ms"], r["e2e_ms"], S.SEGMENT_LABEL.get(r["dominant"], r["dominant"]),
             r["size_ratio"], "key" if r.get("keyframe") == 1 else "delta", r["kind"]] for r in spikes]


SPIKE_HEAD = ["t (s)", "frame", "one-way ms", "e2e ms", "dominant segment", "size / median", "type", "kind"]
SPIKE_W = [0.8, 0.9, 1.0, 0.9, 1.5, 1.0, 0.7, 0.9]


def _fmt_spike(row: list) -> list:
    t, fid, owd, e2e, dom, ratio, typ, kind = row
    return [f"{t:.2f}" if S.is_num(t) else "–", S.fmt(fid, 0) if S.is_num(fid) else "–", S.fmt(owd, 1),
            S.fmt(e2e, 1), dom, f"{ratio:.2f}×" if S.is_num(ratio) else "–", typ, kind]


def pages_late(c: Cell) -> list[Page]:
    spikes = c.spike_rows()
    n = len(spikes)
    p = Page(c, f"Frames over {LATE_MS:.0f} ms one-way", f"{n:,} frames")
    owd = c.fcol("owd_ms")
    late = np.nan_to_num(owd, nan=-1) > LATE_MS
    steady = np.isfinite(owd) & ~late
    ax = p.axes([0.03, 0.66, 0.94, 0.20], "Segment medians: late frames vs steady frames (ms, bar width ∝ ms)")
    ax.grid(False)
    bars = []
    for lab, mask in ((f"late (> {LATE_MS:.0f} ms), n={int(late.sum()):,}", late),
                      (f"steady, n={int(steady.sum()):,}", steady)):
        meds = []
        for name, _ in S.SEGMENTS:
            v = c.fcol(f"{name}_ms")[mask] if c.frames is not None else np.array([])
            meds.append((name, stats.summ(v.tolist())["p50"]))
        bars.append((lab, meds))
    scale = max([sum(m for _, m in meds if S.is_num(m) and m > 0) for _, meds in bars] + [0.0])
    if scale > 0:
        for yy, (lab, meds) in zip((1, 0), bars):
            x = 0.0
            for name, m in meds:
                if not S.is_num(m) or m <= 0:
                    continue
                w = m / scale * 0.80
                ax.barh(yy, w, left=0.18 + x, height=0.6, color=S.SEGMENT_COLOR[name], edgecolor=S.SURFACE, linewidth=1.5)
                if w > 0.035:
                    ax.text(0.18 + x + w / 2, yy, f"{m:.0f}", ha="center", va="center", fontsize=6.8, color="#fff", weight="bold")
                x += w
            tot = sum(m for _, m in meds if S.is_num(m) and m > 0)
            ax.text(0.175, yy, lab, ha="right", va="center", fontsize=7, color=S.INK_2)
            ax.text(0.18 + x + 0.005, yy, f"{tot:.0f} ms", va="center", fontsize=7, color=S.INK)
        for i, (name, lab) in enumerate(S.SEGMENTS):
            lx = 0.18 + i * 0.115
            ax.add_patch(Rectangle((lx, -0.95), 0.01, 0.3, color=S.SEGMENT_COLOR[name]))
            ax.text(lx + 0.014, -0.8, lab, va="center", fontsize=6.8, color=S.INK)
        ax.set_xlim(0, 1)
        ax.set_ylim(-1.1, 1.5)
    else:
        _no_data(ax, "no segment columns in reduced/frames.csv")
    ax.set_xticks([])
    ax.set_yticks([])
    for sp in ax.spines.values():
        sp.set_visible(False)
    rows = _spike_table_rows(spikes)
    if not rows:
        p.text(0.03, 0.60, f"No frame exceeded {LATE_MS:.0f} ms one-way.", fontsize=9, color=S.GOOD_TEXT, weight="bold", va="top")
    else:
        p.text(0.03, 0.62, "Every frame over the threshold, in time order", fontsize=9, weight="bold", va="top")
        p.table(0.03, 0.595, 0.94, SPIKE_HEAD, [_fmt_spike(r) for r in rows[:SPIKE_ROWS_FIRST]], SPIKE_W,
                num_from=0, fs=6.9, lh=0.0235)
    pages = [p]
    rest = rows[SPIKE_ROWS_FIRST:]
    k = 0
    while rest and k < SPIKE_MAX_CONT_PAGES:
        chunk, rest = rest[:SPIKE_ROWS_CONT], rest[SPIKE_ROWS_CONT:]
        k += 1
        q = Page(c, f"Frames over {LATE_MS:.0f} ms one-way (continued {k})", f"{n:,} frames")
        half = SPIKE_ROWS_CONT // 2
        q.table(0.03, 0.895, 0.455, SPIKE_HEAD, [_fmt_spike(r) for r in chunk[:half]], SPIKE_W, num_from=0, fs=6.2, lh=0.0205)
        if chunk[half:]:
            q.table(0.515, 0.895, 0.455, SPIKE_HEAD, [_fmt_spike(r) for r in chunk[half:]], SPIKE_W, num_from=0, fs=6.2, lh=0.0205)
        pages.append(q)
    if rest:
        pages[-1].text(0.03, 0.05, f"{len(rest):,} further frames not printed: the full list is in report.html "
                       "and reduced/spikes.csv.", fontsize=7.5, color=S.CRITICAL, weight="bold")
    p.html_blocks.append(
        f"<h3>Every frame over {LATE_MS:.0f} ms one-way ({n:,})</h3>"
        + (H.table(SPIKE_HEAD, [_fmt_spike(r) for r in rows], num_cols={0, 1, 2, 3, 5}) if rows else
           '<p class="note">none</p>'))
    return pages


# ======================================================================================
# html
# ======================================================================================

def _html_header(c: Cell) -> str:
    left, right = _header_lines(c)
    rows = []
    for k, v, colr in left + right:
        cls = "bad" if colr == S.CRITICAL else None
        rows.append(f"<tr><td>{H.esc(k)}</td><td{' class=\"bad\"' if cls else ''}>{H.esc(v)}</td></tr>")
    banners = []
    if not c.encoder_is_nvenc:
        banners.append(f'<div class="banner">Encoder is {H.esc(c.encoder)}, not NVENC.</div>')
    if c.excluded or str(c.cfg("status", "")) != "OK":
        banners.append(f'<div class="banner warn">Status {H.esc(c.cfg("status", "unknown"))}'
                       f'{" — excluded from comparison" if c.excluded else ""}.</div>')
    return "".join(banners) + f'<table class="t kv"><tbody>{"".join(rows)}</tbody></table>'


# ======================================================================================
# entry point
# ======================================================================================

def build_pages(c: Cell) -> list[Page]:
    S.apply_rc()
    pages = [page_overview(c), page_segments(c), page_network(c), page_modem(c),
             page_frame_size(c), page_rates(c), page_qp(c)]
    pages += pages_late(c)
    return pages


FOOTNOTES = {
    "Overview": "Red ticks at the bottom mark frames A sent that never reached webrtc_receive on B.",
    "Latency by segment": "Summaries from metrics.json; each from stats.summ over the frames where the segment was measured.",
    "One-way network latency and jitter": "Host A packetize to Host B webrtc receive, joined per frame.",
    "Modem activity, both hosts": "Per-second DIAG record counts, not grant sizes. Decoding the records themselves needs QCAT.",
}


def render(cell_dir) -> Path:
    """Write report.pdf and report.html into cell_dir; returns the PDF path."""
    cd = Path(cell_dir)
    c = Cell.load(cd)
    pages = build_pages(c)
    n = len(pages)
    for i, p in enumerate(pages, 1):
        p.footer(i, n, FOOTNOTES.get(p.name, ""))

    def write_pdf(tmp: Path) -> None:
        with PdfPages(tmp) as pdf:
            for p in pages:
                pdf.savefig(p.fig)
            d = pdf.infodict()
            d["Title"] = c.label
            d["Subject"] = "teleop cell report"

    H.atomic_write(cd / "report.pdf", write_pdf)

    sections = []
    for i, p in enumerate(pages, 1):
        body = "".join(p.html_blocks)
        sections.append(f'<section id="p{i}"><h2>{i}. {H.esc(p.name)}</h2>{body}'
                        f'{H.fig_img(p.fig, p.name)}</section>')
        plt.close(p.fig)
    nav = " · ".join(f'<a href="#p{i}">{H.esc(p.name)}</a>' for i, p in enumerate(pages, 1))
    sub = f"{c.cfg('grid_id', '')}  ·  status {c.cfg('status', '–')}  ·  encoder {c.encoder}"
    doc = H.page(c.label, sub, f'<p class="note">{nav}</p>' + "".join(sections))
    H.atomic_write(cd / "report.html", lambda tmp: tmp.write_text(doc, encoding="utf-8"))
    return cd / "report.pdf"


if __name__ == "__main__":
    import sys

    for arg in sys.argv[1:]:
        print(render(arg))
