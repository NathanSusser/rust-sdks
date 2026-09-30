"""Shared palette, typography and number formatting for the cell and grid reports.

One place for colour so that Host A is the same blue on every page of every report,
and a latency segment is the same colour in the per-cell stacked bar and in the grid
comparison. Categorical slots are assigned in fixed order, never cycled; the order is
the colour-blind-safety mechanism (validated adjacent-pair order, light surface).
"""
from __future__ import annotations

import math

import matplotlib

matplotlib.use("Agg")

# --- ink and chrome ----------------------------------------------------------------
SURFACE = "#fcfcfb"
PAGE = "#f9f9f7"
INK = "#0b0b0b"
INK_2 = "#52514e"
MUTED = "#898781"
GRIDLINE = "#e1e0d9"
BASELINE = "#c3c2b7"
PANEL = "#f0efec"
BAND = "#1f2a36"          # page title band
BAND_INK = "#ffffff"
BAND_INK_2 = "#c9d3dd"

# --- categorical slots (fixed order) -----------------------------------------------
SLOTS = (
    "#2a78d6",  # 1 blue
    "#eb6834",  # 2 orange
    "#1baf7a",  # 3 aqua
    "#eda100",  # 4 yellow
    "#e87ba4",  # 5 magenta
    "#008300",  # 6 green
    "#4a3aa7",  # 7 violet
    "#e34948",  # 8 red
)
SLOTS_DARK = ("#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767")
OTHER = "#898781"

HOST_A = SLOTS[0]
HOST_B = SLOTS[1]
OWD = SLOTS[0]
E2E = SLOTS[1]

# --- status (reserved; never a series colour) --------------------------------------
GOOD = "#0ca30c"
WARNING = "#fab219"
SERIOUS = "#ec835a"
CRITICAL = "#d03b3b"
GOOD_TEXT = "#006300"

# --- latency segments, in pipeline order -------------------------------------------
SEGMENTS = (
    ("app_to_wire_a", "A app → wire"),
    ("emission_a", "A emission"),
    ("in_flight", "in flight"),
    ("arrival_b", "B arrival"),
    ("wire_to_app_b", "B wire → app"),
    ("decode", "decode"),
    ("render", "render"),
)
SEGMENT_COLOR = {name: SLOTS[i] for i, (name, _) in enumerate(SEGMENTS)}
SEGMENT_LABEL = dict(SEGMENTS)

STATS = ("mean", "p50", "p95", "p99", "max", "min", "n")
TAIL_STATS = ("p95", "p99", "max")

FONT = "DejaVu Sans"
PAGE_SIZE = (11.0, 8.5)   # landscape letter, inches

CSS_FONT = 'system-ui, -apple-system, "Segoe UI", sans-serif'


def slot(i: int) -> str:
    """Categorical colour for the i-th entity; past eight, the neutral 'other'."""
    return SLOTS[i] if 0 <= i < len(SLOTS) else OTHER


def _fix_pdf_indexed_images() -> None:
    """matplotlib < 3.7 writes a rasterized region with <= 16 colours (a single-colour scatter or
    line: every per-frame series we draw) as a 1/2/4-bit indexed image, but leaves BitsPerComponent
    out of its PNG-predictor DecodeParms, so PDF viewers decode each row at the wrong stride and
    the image shears (white diagonal seams through dense scatters). Upstream fixed it in 3.7 by
    adding the key; this adds it the same way and changes nothing when it is already there."""
    try:
        from matplotlib.backends import backend_pdf as bp  # noqa: PLC0415
    except ImportError:
        return
    if getattr(bp.PdfFile.beginStream, "_teleop_bpc_fix", False):
        return
    orig = bp.PdfFile.beginStream

    def beginStream(self, id, len, extra=None, png=None):  # noqa: A002 -- matplotlib's signature
        bpc = (extra or {}).get("BitsPerComponent")
        if png is not None and isinstance(bpc, int) and bpc < 8 and "BitsPerComponent" not in png:
            png = dict(png, BitsPerComponent=bpc)
        return orig(self, id, len, extra, png)

    beginStream._teleop_bpc_fix = True
    bp.PdfFile.beginStream = beginStream


_fix_pdf_indexed_images()


def apply_rc() -> None:
    """Matplotlib defaults shared by every figure: recessive grid, no top/right spine."""
    matplotlib.rcParams.update({
        "font.family": FONT,
        "font.size": 8,
        "axes.titlesize": 9,
        "axes.titleweight": "bold",
        "axes.titlelocation": "left",
        "axes.titlecolor": INK,
        "axes.labelsize": 7.5,
        "axes.labelcolor": INK_2,
        "axes.edgecolor": BASELINE,
        "axes.linewidth": 0.8,
        "axes.facecolor": SURFACE,
        "axes.grid": True,
        "axes.axisbelow": True,
        "axes.spines.top": False,
        "axes.spines.right": False,
        "grid.color": GRIDLINE,
        "grid.linewidth": 0.6,
        "xtick.color": MUTED,
        "ytick.color": MUTED,
        "xtick.labelsize": 7,
        "ytick.labelsize": 7,
        "legend.fontsize": 7,
        "legend.frameon": False,
        "figure.facecolor": "#ffffff",
        "lines.linewidth": 1.2,
        "pdf.fonttype": 42,
        "svg.fonttype": "none",
    })


# --- number formatting -------------------------------------------------------------

def is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def fmt(v, digits: int | None = None) -> str:
    """Compact, stable formatting: '-' for missing, thousands separators, 0-2 decimals."""
    if v is None or (isinstance(v, float) and not math.isfinite(v)):
        return "–"
    if isinstance(v, bool):
        return "yes" if v else "no"
    if not isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, int) or (digits == 0):
        return f"{int(round(v)):,}"
    if digits is not None:
        return f"{v:,.{digits}f}"
    a = abs(v)
    if a >= 1000:
        return f"{v:,.0f}"
    if a >= 100:
        return f"{v:.0f}"
    if a >= 10:
        return f"{v:.1f}"
    if a >= 0.01 or a == 0:
        return f"{v:.2f}"
    return f"{v:.2g}"


def pct(v) -> str:
    return "–" if not is_num(v) else f"{100.0 * v:.2f}%"
