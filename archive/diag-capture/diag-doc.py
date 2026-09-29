#!/usr/bin/env python3
"""Per-cell DIAG document: what the modem log shows at the cell's event instant.

  diag-doc.py <celldir> <event_utc_hhmmss.mmm|auto> <out.md>

"auto" derives the instant from packets_lost in Host B's subscriber.csv -- always prefer that
over a hand-typed time. Hardcoding event epochs on 2026-09-23 put two of three analysis windows
on the wrong minute (off by exactly 600 s), which would have reported "no radio change at the
loss" for a time the loss never happened.

ONE PASS over the DLF, collecting everything: these files are 4-5 GB and a second pass costs
minutes for nothing.
"""
import os, sys, struct, csv, datetime
import statistics as st
from collections import Counter
sys.path.insert(0, os.environ.get('DIAG_CAPTURE_DIR',
                                  os.path.dirname(os.path.abspath(__file__))))
import dlf_records

SENT = -19968
ML1 = 0xB97F
WIN = 20.0            # seconds of ML1 kept either side of the instant

def blocks(b):
    out, base = [], 0
    while base <= len(b) - 76:
        arf = struct.unpack_from("<I", b, base + 32)[0]
        pci = struct.unpack_from("<H", b, base + 38)[0]
        alt = struct.unpack_from("<H", b, base + 64)[0]
        if 100000 <= arf <= 700000 and 0 <= pci <= 1007 and pci == alt:
            out.append((base, arf, pci)); base += 76
        else:
            base += 4
    return out

def beams(b, base, end):
    out, k = [], 0
    while True:
        fo, io = base + 72 + 60 * k, base + 80 + 60 * k
        if fo + 4 > end or io >= end: break
        raw = struct.unpack_from("<i", b, fo)[0]; idx = b[io]
        if idx > 63: break
        if raw == SENT: out.append((idx, None))
        else:
            db = raw / 128.0
            if not (-160.0 <= db <= -30.0): break
            out.append((idx, db))
        k += 1
    return out

def U(t):
    return datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime("%H:%M:%S.%f")[:-3]

def host_minus_utc(celldir):
    """The cell's OWN measured clock offset, from its dlf-rates.csv header.

    THIS IS NOT OPTIONAL AND IT IS NOT A CONSTANT. DLF record timestamps are true UTC; every
    host-side artefact -- capture_timestamp_us, hops.csv, the publisher CSV -- is on the HOST
    clock, and the hosts run ~27-28 s behind UTC. Comparing a host-clock instant directly
    against DLF timestamps puts the analysis window 27 s early.

    That is exactly what the first version of this script did. On bh09230639-003 it reported
    the serving beam moving 0.20 dB across the loss; the corrected window shows 4.44 dB. The
    wrong number looked entirely plausible and was the headline of a report.

    The offset also DRIFTS: -27.108 on the first cell of 2026-09-23 to -28.158 on the last,
    about 29 us/s. So use each cell's own measured value, never one borrowed from another cell.
    """
    import re
    p = os.path.join(celldir, "dlf-rates.csv")
    if not os.path.exists(p):
        return None
    m = re.search(r"host_minus_utc_s=([-0-9.]+)", open(p).readline())
    return float(m.group(1)) if m else None

def loss_instant(celldir):
    p = os.path.join(celldir, "hostb", "subscriber.csv")
    if not os.path.exists(p): return None, 0
    rows = list(csv.DictReader(open(p)))
    prev = 0
    for r in rows:
        try: v = int(float(r.get("packets_lost") or 0))
        except ValueError: continue
        if v > prev:
            try: return int(r["capture_timestamp_us"]) / 1e6, v - prev
            except Exception: return None, v - prev
        prev = v
    return None, 0

celldir = sys.argv[1].rstrip("/")
label = os.path.basename(celldir)
want = sys.argv[2]
out_path = sys.argv[3]
dlf = os.path.join(celldir, f"{label}.dlf")

inst, burst = loss_instant(celldir)
OFF = host_minus_utc(celldir)
if OFF is None:
    sys.exit(f"{label}: no measured clock offset in dlf-rates.csv -- refusing to guess. "
             f"Without it every window is ~27 s off and the answer looks plausible anyway.")
# host -> modem/UTC. host = utc + host_minus_utc_s, so utc = host - host_minus_utc_s.
if inst is not None:
    inst_host = inst
    inst = inst - OFF
if want != "auto" and inst is None:
    # a radio-candidate cell has no loss; take the supplied UTC time-of-day against the
    # capture's own date, which we learn from the first record below
    inst = None

f = open(dlf, "rb")
codes = Counter(); n = 0; first = last = None
ml1 = []
day = None
for lid, t, ln, off in dlf_records.iter_file(dlf, emit_offset=True):
    n += 1; codes[lid] += 1
    if first is None: first = t
    last = t
    if day is None:
        d = datetime.datetime.fromtimestamp(t, datetime.timezone.utc)
        day = (d.year, d.month, d.day)
        if inst is None and want != "auto":
            # The indexed candidate time comes from find-beam-events.py, which reads DLF
            # records directly -- it is already modem/UTC and must NOT be shifted again.
            hh, mm, ss = want.rstrip("Z").split(":")
            inst = datetime.datetime(day[0], day[1], day[2], int(hh), int(mm),
                                     int(float(ss)), int((float(ss) % 1) * 1e6),
                                     tzinfo=datetime.timezone.utc).timestamp()
            inst_host = inst + OFF
    if lid == ML1 and inst is not None and abs(t - inst) <= WIN:
        f.seek(off); b = f.read(ln)
        bl = blocks(b)
        if not bl: continue
        # SELECT THE CARRIER BY ARFCN, NOT BY POSITION. blocks[0] is not reliably the same
        # carrier: the block ORDER varies between records, so taking the first one silently
        # alternates between the PCell and an SCell and the "serving beam" series becomes a
        # mixture of two carriers on different frequencies. That shows up as ARFCN "CHANGED"
        # and as level swings that are really just the series hopping bands. Collect every
        # block here; the modal ARFCN is chosen after the pass, when we know which it is.
        end_of = lambda i: bl[i + 1][0] if i + 1 < len(bl) else len(b)
        per = []
        for i, (base, arf, pci) in enumerate(bl):
            bm = beams(b, base, end_of(i))
            if bm:
                per.append((arf, pci, tuple(x for x, _ in bm),
                            {x: v for x, v in bm if v is not None}))
        if per:
            ml1.append((t, per))
ml1.sort()
# REPORT EVERY CARRIER SEPARATELY. Do not collapse to one "serving beam".
#
# Measured on bh09230639-003: block position 0 holds ARFCN 521310 in 1,708 records and ARFCN
# 501390 in 1,250 -- two different n41 carriers alternating in the same slot. So blocks[0] is
# NOT a stable carrier identity, and a series built from it is a mixture of two frequencies.
# That produced a 4.44 dB "swing" that is an artefact of the series hopping carriers.
# Choosing the modal ARFCN instead is no better: it selects 393130, the n25 SCell, which is not
# the serving cell. Neither collapse is defensible, so present the carriers as what they are.
from collections import Counter as _C
arf_counts = _C(a for _, per in ml1 for (a, _p, _s, _l) in per)
series = {}
for a in arf_counts:
    rows = []
    for t, per in ml1:
        m = [x for x in per if x[0] == a]
        if m and m[0][3]:
            rows.append((t, m[0][1], m[0][2], max(m[0][3].values())))
    if rows:
        series[a] = rows

L = []
L.append(f"# DIAG document — {label}")
L.append("")
L.append(f"Capture `{os.path.basename(dlf)}` · {os.path.getsize(dlf)/1073741824:.2f} GB · "
         f"{n:,} records · span {U(first)}Z – {U(last)}Z")
L.append("")
L.append("## Capture integrity")
L.append("")
L.append("A record is accepted only if its declared length fits, its log code is plausible, AND")
L.append("the next record also parses. A length that merely fits is not enough: a wrong declared")
L.append("length lands the walk inside a payload, where random bytes often still look like lengths.")
L.append("")
nr5g = sum(v for k, v in codes.items() if 0xB800 <= k <= 0xB9FF)
L.append(f"- total records: **{n:,}**  ({nr5g:,} NR5G, {n-nr5g:,} other)")
L.append(f"- distinct log codes: **{len(codes)}**")
L.append(f"- ML1 (0xB97F) records: **{codes.get(ML1,0):,}**")
L.append("")
L.append("## Event window")
L.append("")
if inst is None:
    L.append("No event instant available for this cell.")
else:
    src = f"packets_lost, burst of {burst} packets" if burst else "radio-candidate time"
    L.append(f"Instant **{U(inst)}Z** (modem/UTC) — source: {src}.")
    L.append("")
    L.append(f"Host-clock equivalent **{U(inst_host)}Z**, converted with this cell's own measured")
    L.append(f"offset `host_minus_utc_s = {OFF:+.3f}`. DLF timestamps are true UTC; host-side")
    L.append("artefacts run ~27–28 s behind, and the offset drifts ~29 µs/s across a night, so")
    L.append("each cell uses its own value.")
    L.append("")
    L.append(f"ML1 records within ±{WIN:.0f} s: **{len(ml1)}**")
    L.append("")
    L.append("### Carriers present (carrier aggregation — these are concurrent, not a handover)")
    L.append("")
    L.append("| NR-ARFCN | records in window | PCI | strongest beam, ±2 s | range |")
    L.append("|---|---|---|---|---|")
    for a in sorted(series, key=lambda k: -len(series[k])):
        rows = series[a]
        near = [r for r in rows if abs(r[0] - inst) <= 2.0]
        pcis = {r[1] for r in rows}
        if near:
            v = [r[3] for r in near]
            L.append(f"| {a} | {len(rows)} | {'/'.join(str(x) for x in sorted(pcis))} | "
                     f"{min(v):.1f} to {max(v):.1f} dBm | **{max(v)-min(v):.2f} dB** |")
        else:
            L.append(f"| {a} | {len(rows)} | {'/'.join(str(x) for x in sorted(pcis))} | "
                     f"no records within ±2 s | — |")
    L.append("")
    L.append("> **Which of these is the serving cell has NOT been established from these records.**")
    L.append("> Block position is not a stable carrier identity here — on one capture, position 0")
    L.append("> held ARFCN 521310 in 1,708 records and 501390 in 1,250. Any single-number")
    L.append("> \"serving beam moved N dB\" claim built on block position is unsafe, and an earlier")
    L.append("> version of this document made exactly that claim. Settle it with SCAT")
    L.append("> (`scat -t qc -d <file>.dlf`), which names PCell and SCell explicitly.")
    L.append("")
    for a in sorted(series, key=lambda k: -len(series[k]))[:1]:
        L.append(f"### Per-sample, ARFCN {a} (most-represented carrier)")
        L.append("")
        L.append("| UTC | strongest beam |")
        L.append("|---|---|")
        for t, pci, idxs, v in series[a]:
            if abs(t - inst) > 1.6: continue
            mark = " **← instant**" if abs(t - inst) < 0.18 else ""
            L.append(f"| {U(t)}Z{mark} | {v:.1f} |")
    L.append("")
L.append("## Log-code census (top 20 by volume)")
L.append("")
L.append("| code | records |")
L.append("|---|---|")
for c, v in codes.most_common(20):
    L.append(f"| `{hex(c)}` | {v:,} |")
open(out_path, "w").write("\n".join(L) + "\n")
print(f"{label}: {n:,} records, {len(ml1)} ML1 in window -> {out_path}")
