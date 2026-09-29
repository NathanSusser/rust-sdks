"""Per-second, per-code DLF record counts in the cell window, plus the ML1 band read, in one pass.

Ported from archive/diag-capture/dlf-rates.py. Acceptance is dlf_records' structural rule
(NOT dlf-check's; the two differ where a capture has malformed records). Host time =
modem time + host_minus_utc_s (h = t + off).

Records are kept only if their host time falls inside [epoch - margin, epoch + duration +
margin). That window comes from the cell, never from the record timestamps: ~0.28% of
records carry garbage timestamps (years 1981 / 9000) and a min/max of those is meaningless.

CSV (same header line format as the reference, so one reader parses old and new files):
    # parser=...; host_minus_utc_s=<signed>; host=modem<signed>s; probe_start=<E> probe_end=<E+dur>; records_total=N resyncs=R skipped_bytes=S
    second_rel_probe,code,count
second_rel_probe = floor(host_ts - probe_start); probe_start is the cell epoch.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from pathlib import Path

from . import dlf_records, ml1

# The modem activity index: DL/UL scheduling and grant codes that rose with the startup
# transient in the spike ledger (proof.py ELEV).
ELEV = ("0xB8C4", "0xB8CE", "0xB8CF", "0xB8D0", "0xB896", "0xB887", "0xB89B", "0xB89E")
X19EF = "0x19EF"
STEADY_FROM_S = 15.0


def scan(dlf_path, host_minus_utc_s: float, epoch: float, duration_s: float, margin_s: float = 60.0,
         want_ml1: bool = True) -> dict:
    """One streaming pass: rate counts in the window and (optionally) ML1 carrier blocks.

    Returns {"counts": Counter[(sec, code)], "stats": {...}, "ml1": ml1.Collector|None,
             "records_total", "records_in_window", "records_outside_window"}.
    """
    lo, hi = epoch - margin_s, epoch + duration_s + margin_s
    counts: Counter = Counter()
    st: dict = {}
    col = ml1.Collector() if want_ml1 else None
    n = inside = 0
    fh = open(dlf_path, "rb") if want_ml1 else None
    try:
        for lid, t, ln, off in dlf_records.iter_file(dlf_path, stats=st, emit_offset=True):
            n += 1
            h = t + host_minus_utc_s
            if not dlf_records.in_window(h, lo, hi):
                continue
            inside += 1
            counts[(int((h - epoch) // 1), lid)] += 1
            if col is not None and lid == ml1.ML1_CODE:
                fh.seek(off)
                col.add(t, h - epoch, fh.read(ln))
    finally:
        if fh:
            fh.close()
    return {"counts": counts, "stats": {"resyncs": st.get("resyncs", 0), "skipped_bytes": st.get("skipped_bytes", 0)},
            "ml1": col, "records_total": n, "records_in_window": inside, "records_outside_window": n - inside,
            "window": [lo, hi]}


def write_csv(path, counts: Counter, host_minus_utc_s: float, epoch: float, duration_s: float,
              records_total: int, stats: dict) -> None:
    off = host_minus_utc_s
    with open(path, "w") as f:
        f.write("# parser=teleop.grid.reduce.dlf_records.iter_file (structural acceptance: length fits, code "
                "0x1000-0x1FFF|0xB000-0xB9FF, next parses; NOT dlf-check's rule, which differs on malformed records); "
                f"host_minus_utc_s={off:+.3f}; host=modem{off:+.3f}s; probe_start={epoch:.3f} probe_end={epoch + duration_s:.3f}; "
                f"records_total={records_total} resyncs={stats.get('resyncs')} skipped_bytes={stats.get('skipped_bytes')}\n")
        f.write("second_rel_probe,code,count\n")
        for (sec, code), c in sorted(counts.items()):
            f.write(f"{sec},0x{code:04X},{c}\n")


_OFF_RE = re.compile(r"host_minus_utc_s=([+-]?[0-9.]+)")
_PS_RE = re.compile(r"probe_start=([0-9.]+)")
_PSMS_RE = re.compile(r"probe_start_ms=([0-9]+)")


def read_header_offset(path) -> float | None:
    """host_minus_utc_s from a rates CSV header (either the reference or the older host-B spelling)."""
    try:
        with open(path) as f:
            for line in f:
                if not line.startswith("#"):
                    break
                m = _OFF_RE.search(line)
                if m:
                    return float(m.group(1))
    except FileNotFoundError:
        return None
    return None


def read_csv(path, epoch: float | None = None) -> dict[str, dict[int, int]] | None:
    """per[code]["second rel epoch"] = count. If the file's probe_start differs from `epoch`, seconds are re-based."""
    p = Path(path)
    if not p.exists():
        return None
    per: dict[str, dict[int, int]] = defaultdict(lambda: defaultdict(int))
    shift = 0
    with open(p) as f:
        for line in f:
            if line.startswith("#"):
                ps = None
                m = _PS_RE.search(line)
                if m:
                    ps = float(m.group(1))
                m = _PSMS_RE.search(line)
                if m:
                    ps = int(m.group(1)) / 1000
                if ps is not None and epoch is not None:
                    shift = int(round(ps - epoch))
                continue
            if line.startswith("second"):
                continue
            s, c, n = line.strip().split(",")
            per[c.upper().replace("0X", "0x")][int(s) + shift] += int(n)
    return per


def elev_series(per, seconds) -> dict[int, int]:
    return {s: sum(per.get(c, {}).get(s, 0) for c in ELEV) for s in seconds}


def activity_index(per, duration_s: float, first_s: int = -5, last_s: int | None = None) -> dict:
    """Per-second modem activity index: ELEV-code records in that second / mean per second over
    the cell's own steady state (t >= 15 s to the end of the cell).

    Returns {"steady_per_s": float|None, "index": {sec: float}}. index is empty when there is no
    steady state (no records, or a cell shorter than 16 s).
    """
    last = int(duration_s) if last_s is None else last_s
    steady_secs = range(int(STEADY_FROM_S), int(duration_s))
    if per is None or len(steady_secs) == 0:
        return {"steady_per_s": None, "index": {}}
    steady = sum(elev_series(per, steady_secs).values()) / len(steady_secs)
    if steady <= 0:
        return {"steady_per_s": 0.0, "index": {}}
    ser = elev_series(per, range(first_s, last))
    return {"steady_per_s": steady, "index": {s: v / steady for s, v in ser.items()}}


def code_count(per, code: str, a: int, b: int) -> int:
    if per is None:
        return 0
    return sum(v for s, v in per.get(code, {}).items() if a <= s < b)
