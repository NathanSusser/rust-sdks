#!/usr/bin/env python3
"""Paired A/B report for one cell: video delivery, latency, and BOTH modems, on one time base.

THREE DEFECTS THIS GENERATOR EXISTS TO NOT REPEAT, each one shipped to the operator first:

  1. A MODEM PAGE ON ITS OWN AXIS. The first paired PDF plotted 398 s of modem data against a
     1.02 s video axis. It rendered as smooth near-straight lines with `nan` in every ratio
     column and looked entirely plausible. Here the modem page is FORCED onto the same x-limits
     as the video page, and the limits are computed once, in one place, and reused.

  2. JOINING TWO HOSTS ON A RELATIVE INDEX. A's reduction anchors at probe_start 1789759916 and
     B's at 1789759917 -- one second apart. Keyed on `second_rel_probe`, B's second 0 would be
     drawn on top of A's second 1 and the error would be invisible. EVERY series here is
     converted to ABSOLUTE epoch (probe_start + second_rel_probe) before it is plotted or
     compared. The two host clocks are PTP-synced to each other and agree to ~25 ms, so an
     absolute join is sound at one-second granularity; the UTC offsets differ and are irrelevant
     to the join, which is why neither is applied.

  3. AN ANCHOR YOU CANNOT SEE. A 48 s anchor error survived a full review because nothing on the
     page showed what the data was anchored to. Each host's probe_start is now PRINTED ON THE
     MODEM PAGE, next to the media window, so a repeat is visible at a glance rather than only
     to whoever thinks to diff two CSV headers.

Usage: paired-report.py <celldir> <out.pdf>
Expects <celldir>/<label>.pub.csv, <celldir>/dlf-rates.csv (host A),
        <celldir>/hostb/subscriber.csv, <celldir>/hostb/dlf-rates.csv (host B).
"""
import csv
import os
import statistics
import sys
from collections import defaultdict

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages

A_COL, B_COL, GRID, INK = "#1F6FB2", "#D1690C", "#D8D8D8", "#222222"

def anchor_of(path):
    """probe_start, in host-clock seconds, read from the reduction's own header."""
    with open(path) as fh:
        for line in fh:
            if not line.startswith("#"):
                break
            for tok in line.replace(";", " ").split():
                if tok.startswith("probe_start_ms="):
                    return float(tok.split("=")[1]) / 1000.0
                if tok.startswith("probe_start="):
                    return float(tok.split("=")[1])
    raise SystemExit(f"no probe_start in {path} -- refusing to guess an anchor")

def modem_series(path):
    """{code: {absolute_epoch_second: count}} -- absolute, never relative."""
    start = anchor_of(path)
    out = defaultdict(dict)
    with open(path) as fh:
        for row in csv.reader(fh):
            if not row or row[0].startswith("#") or row[0] == "second_rel_probe":
                continue
            try:
                sec, code, n = int(row[0]), int(row[1], 16), int(row[2])
            except (ValueError, IndexError):
                continue
            t = start + sec
            out[code][t] = out[code].get(t, 0) + n
    return start, out

def per_second(rows, tkey, vkey=None, scale=1e6):
    """Bin rows into absolute seconds. Returns {second: [values]} or {second: count}."""
    acc = defaultdict(list)
    for r in rows:
        try:
            t = int(float(r[tkey]) / scale)
        except (ValueError, KeyError, TypeError):
            continue
        acc[t].append(float(r[vkey]) if vkey and r.get(vkey) not in (None, "") else 0.0)
    return acc

def main(celldir, out):
    label = os.path.basename(celldir.rstrip("/"))
    pub = os.path.join(celldir, f"{label}.pub.csv")
    sub = os.path.join(celldir, "hostb", "subscriber.csv")
    a_dlf = os.path.join(celldir, "dlf-rates.csv")
    b_dlf = os.path.join(celldir, "hostb", "dlf-rates.csv")
    for p in (pub, sub, a_dlf, b_dlf):
        if not os.path.exists(p):
            raise SystemExit(f"missing input: {p}")

    arows = list(csv.DictReader(open(pub)))
    brows = list(csv.DictReader(open(sub)))

    # THE ONE TIME BASE. capture_timestamp_us is stamped by A at capture and carried through
    # to B on the wire, so both sides key off the identical instant -- no clock conversion,
    # no assumption about either host's UTC offset.
    a_fps = per_second(arows, "capture_timestamp_us")
    b_fps = per_second(brows, "capture_timestamp_us")
    b_e2e = per_second(brows, "capture_timestamp_us", "e2e_to_gpu_complete_ms")
    t0 = min(min(a_fps), min(b_fps))
    t1 = max(max(a_fps), max(b_fps))
    XLIM = (0, t1 - t0)                       # computed ONCE; every page uses it

    a_start, a_mod = modem_series(a_dlf)
    b_start, b_mod = modem_series(b_dlf)

    secs = sorted(set(a_fps) | set(b_fps))
    x = [s - t0 for s in secs]
    a_cnt = [len(a_fps.get(s, [])) for s in secs]
    b_cnt = [len(b_fps.get(s, [])) for s in secs]
    e50 = [statistics.median(b_e2e[s]) if b_e2e.get(s) else float("nan") for s in secs]
    e95 = [sorted(b_e2e[s])[int(len(b_e2e[s]) * 0.95)] if len(b_e2e.get(s, [])) > 3 else float("nan")
           for s in secs]

    lost = [int(r["packets_lost"]) for r in brows if r.get("packets_lost", "").strip().isdigit()]
    e2e_all = [float(r["e2e_to_gpu_complete_ms"]) for r in brows
               if r.get("e2e_to_gpu_complete_ms", "").strip() not in ("", "nan")]

    with PdfPages(out) as pdf:
        # ---------- page 1: delivery and latency ----------
        fig, ax = plt.subplots(3, 1, figsize=(11, 8.5), sharex=True,
                               gridspec_kw={"height_ratios": [2, 2, 1.4], "hspace": 0.28})
        fig.suptitle(f"{label} -- paired A/B, one time base", fontsize=13, weight="bold", y=0.975)
        ax[0].plot(x, a_cnt, color=A_COL, lw=1.4, label=f"Host A captured ({len(arows):,} frames)")
        ax[0].plot(x, b_cnt, color=B_COL, lw=1.4, label=f"Host B rendered ({len(brows):,} frames)")
        ax[0].set_ylabel("frames / s")
        ax[0].legend(loc="lower right", fontsize=8, framealpha=0.9)
        ax[0].set_title("Delivery", fontsize=10, loc="left")

        ax[1].plot(x, e50, color=B_COL, lw=1.4, label="e2e p50")
        ax[1].plot(x, e95, color=B_COL, lw=0.9, alpha=0.5, label="e2e p95")
        ax[1].set_ylabel("ms, capture to GPU complete")
        ax[1].legend(loc="upper right", fontsize=8, framealpha=0.9)
        ax[1].set_title("End-to-end latency, measured at Host B", fontsize=10, loc="left")

        drop = [a - b for a, b in zip(a_cnt, b_cnt)]
        ax[2].axhline(0, color=GRID, lw=1)
        ax[2].plot(x, drop, color=INK, lw=1.0)
        ax[2].set_ylabel("A - B\nframes/s")
        ax[2].set_xlabel("seconds into the cell")
        ax[2].set_title("Shortfall (positive = captured but not rendered)", fontsize=10, loc="left")
        for a_ in ax:
            a_.grid(True, color=GRID, lw=0.6)
            a_.set_axisbelow(True)
            a_.set_xlim(*XLIM)
        pdf.savefig(fig); plt.close(fig)

        # ---------- page 2: both modems, SAME axis ----------
        codes = [(0xB883, "UL scheduling report"), (0xB881, "UL TB stats"),
                 (0xB872, "NR L2 UL TB"), (0xB821, "NR RRC OTA")]
        fig, ax = plt.subplots(len(codes), 1, figsize=(11, 8.5), sharex=True,
                               gridspec_kw={"hspace": 0.35})
        fig.suptitle(f"{label} -- modem, both hosts, on the video time base",
                     fontsize=13, weight="bold", y=0.975)
        for axis, (code, name) in zip(ax, codes):
            for series, col, who, st in ((a_mod, A_COL, "Host A", a_start),
                                         (b_mod, B_COL, "Host B", b_start)):
                d = series.get(code, {})
                # DENSIFY, then draw as steps. A sparse code (0xB821 fires a few times a
                # minute) plotted as an interpolated line draws a straight ramp ACROSS the
                # seconds that had no records at all -- inventing a slow rise and fall where
                # the truth is two isolated events and silence between them. Filling the
                # window with explicit zeros and stepping between them shows the silence.
                dense = [(sec, d.get(t0 + sec, 0)) for sec in range(int(XLIM[0]), int(XLIM[1]) + 1)]
                total = sum(v for t, v in d.items() if XLIM[0] <= t - t0 <= XLIM[1])
                if total:
                    axis.plot([p[0] for p in dense], [p[1] for p in dense], color=col, lw=1.0,
                              drawstyle="steps-mid", label=f"{who} ({total:,} in window)")
            axis.set_ylim(bottom=0)
            axis.set_ylabel("records/s")
            axis.set_title(f"0x{code:04X}  {name}", fontsize=10, loc="left")
            axis.legend(loc="upper right", fontsize=8, framealpha=0.9)
            axis.grid(True, color=GRID, lw=0.6)
            axis.set_axisbelow(True)
            axis.set_xlim(*XLIM)
        ax[-1].set_xlabel("seconds into the cell -- SAME axis as page 1")
        # Defect 3: make the anchors visible on the page itself.
        fig.text(0.5, 0.015,
                 f"anchors: Host A probe_start={a_start:.0f}   Host B probe_start={b_start:.0f}   "
                 f"(delta {b_start - a_start:+.0f} s)   media window {t0:.0f}..{t1:.0f}   "
                 f"joined on ABSOLUTE epoch, not on the relative second index",
                 ha="center", fontsize=7.5, color="#555555")
        pdf.savefig(fig); plt.close(fig)

        # ---------- page 3: latency distribution and the numbers ----------
        fig, ax = plt.subplots(1, 2, figsize=(11, 8.5), gridspec_kw={"width_ratios": [1.4, 1]})
        ax[0].hist(e2e_all, bins=80, color=B_COL, alpha=0.85)
        ax[0].set_xlabel("end-to-end ms"); ax[0].set_ylabel("frames")
        ax[0].set_title("End-to-end latency distribution", fontsize=10, loc="left")
        ax[0].grid(True, color=GRID, lw=0.6); ax[0].set_axisbelow(True)

        q = statistics.quantiles(e2e_all, n=100) if len(e2e_all) > 10 else []
        stages = [("exposure_to_receive_ms", "transit"), ("receive_and_assembly_ms", "assembly"),
                  ("decode_ms", "decode"), ("render_ms", "render")]
        lines = [f"frames captured (A)      {len(arows):,}",
                 f"frames rendered (B)      {len(brows):,}",
                 f"shortfall                {len(arows) - len(brows):,}",
                 f"packets_lost (B, max)    {max(lost) if lost else 'n/a'}",
                 "",
                 f"e2e p50                  {statistics.median(e2e_all):.1f} ms",
                 f"e2e p95                  {q[94]:.1f} ms" if q else "",
                 f"e2e p99                  {q[98]:.1f} ms" if q else "",
                 f"e2e max                  {max(e2e_all):.1f} ms",
                 "",
                 "stage medians (Host B):"]
        for key, nm in stages:
            v = [float(r[key]) for r in brows if r.get(key, "").strip() not in ("", "nan")]
            if v:
                lines.append(f"  {nm:22s} {statistics.median(v):.1f} ms")
        lines += ["", f"Host A modem anchor      {a_start:.0f}",
                  f"Host B modem anchor      {b_start:.0f}",
                  f"media window             {t1 - t0:.0f} s"]
        ax[1].axis("off")
        ax[1].text(0.0, 1.0, "\n".join(l for l in lines if l is not None),
                   va="top", ha="left", family="monospace", fontsize=9, color=INK)
        ax[1].set_title("Numbers", fontsize=10, loc="left")
        pdf.savefig(fig); plt.close(fig)

    print(f"wrote {out}")
    print(f"  media window   {t1 - t0:.0f} s, axis {XLIM[0]}..{XLIM[1]:.0f} on every page")
    print(f"  A anchor {a_start:.0f}   B anchor {b_start:.0f}   delta {b_start - a_start:+.0f} s")
    print(f"  frames A {len(arows):,}  B {len(brows):,}  e2e p50 {statistics.median(e2e_all):.1f} ms")

if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    main(sys.argv[1], sys.argv[2])
