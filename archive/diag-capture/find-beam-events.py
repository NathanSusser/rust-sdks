#!/usr/bin/env python3
"""Scan a DLF for the 2026-09-18 receive-side reconfiguration signature.

  find-beam-events.py <dlf> [--step-db 5] [--quiet]

THE SIGNATURE, as established on cell5m-a (see RUNBOOK and the vendor package):
  * the serving beam's FILTERED BRSRP steps by >= step_db within ~1 s,
  * while PCI, NR-ARFCN and the reported SSB index set are UNCHANGED,
  * and the beam that gains is the one that was ALREADY strongest.
That combination is not a handover and not a beam switch. It is what we believe is a
UE receive-side reconfiguration, and it produced a visible video blackout.

WHY A TOOL AND NOT AN EYEBALL: the event lasted one 160 ms sample in a 385 s capture,
and we have 183 GB of captures nobody has looked at. Hand-analysis found it once; it
will not find it 42 times.

RECORD MODEL (validated against SCAT, which names these fields):
  carrier blocks are DISCOVERED per record -- the stride is +212 on one host and +272
  or +332 on the other, and varies WITHIN a capture, so it is never assumed. A block is
  accepted only where the ARFCN is raster-plausible, the PCI is in range, and the two
  PCI copies at +38/+64 agree.
  Within a block, relative to the first beam entry at block+72:
      entry k filtered BRSRP   block + 72 + 60k
      entry k SSB index        block + 80 + 60k      (== filtered offset + 8)
      entry k instant pair     block + 100 + 60k , block + 104 + 60k
  int32/128 for dB values; raw -19968 (== -156.00 dB) is the NOT-MEASURED sentinel and
  must be excluded from every statistic, not averaged into one.

FALSE-POSITIVE CONTROLS, each of which caught a wrong answer during this work:
  1. sentinel-aware: a field going unmeasured is not a level change.
  2. the SSB index set must be constant -- if it changes this is a beam switch and a
     DIFFERENT event, reported separately rather than silently lumped in.
  3. PCI/ARFCN must be constant -- otherwise it is a handover.
  4. the gaining beam must already have been the strongest.
"""
import os, sys, struct, datetime
import statistics as st
from collections import Counter
sys.path.insert(0, os.environ.get('DIAG_CAPTURE_DIR',
                                  os.path.dirname(os.path.abspath(__file__))))
import dlf_records

SENT = -19968
ML1 = 0xB97F

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
    """[(ssb_index, filtered_dB or None)] for the beam entries in ONE block.

    BOUNDED BY THE NEXT BLOCK, not by a guessed entry count. The first version of this
    ran k over range(8) and walked straight out of the PCell block into the SCell block
    at base+252 (the carrier stride is 212 on this host), inventing a fourth "beam" with
    a level of +510 dB and a random index byte. It then classified every record as a
    beam-set change. A detector that accepts impossible values is worse than no detector.

    So: stop at the next block, and accept an entry only if its level is a PLAUSIBLE
    RSRP or the explicit not-measured sentinel. Anything else ends the list.
    """
    out = []
    k = 0
    while True:
        fo, io = base + 72 + 60 * k, base + 80 + 60 * k
        if fo + 4 > end or io >= end:
            break
        raw = struct.unpack_from("<i", b, fo)[0]
        idx = b[io]
        if idx > 63:
            break
        if raw == SENT:
            out.append((idx, None))
        else:
            db = raw / 128.0
            if not (-160.0 <= db <= -30.0):
                break
            out.append((idx, db))
        k += 1
    return out

def U(t):
    return datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime("%H:%M:%S.%f")[:-3]

path = sys.argv[1]
step_db = 5.0
if "--step-db" in sys.argv:
    step_db = float(sys.argv[sys.argv.index("--step-db") + 1])

recs = []
f = open(path, "rb")
for lid, t, ln, off in dlf_records.iter_file(path, emit_offset=True):
    if lid != ML1:
        continue
    f.seek(off); b = f.read(ln)
    bl = blocks(b)
    if not bl:
        continue
    base, arf, pci = bl[0]                      # first block = PCell
    end = bl[1][0] if len(bl) > 1 else len(b)   # bound: where the NEXT carrier starts
    bm = beams(b, base, end)
    if not bm:
        continue
    recs.append((t, arf, pci, tuple(i for i, _ in bm),
                 {i: v for i, v in bm if v is not None}))
if len(recs) < 20:
    print(f"{os.path.basename(path)}: only {len(recs)} usable ML1 records; nothing to scan")
    sys.exit(0)
recs.sort(key=lambda r: r[0])

arfs = Counter(r[1] for r in recs); pcis = Counter(r[2] for r in recs)
idxsets = Counter(r[3] for r in recs)
span = recs[-1][0] - recs[0][0]
print(f"{os.path.basename(path)}: {len(recs)} ML1 records, {span:.0f}s, "
      f"{U(recs[0][0])}Z..{U(recs[-1][0])}Z")
print(f"  ARFCN {dict(arfs)}  PCI {dict(pcis)}  SSB index sets {dict(idxsets)}")

# The strongest beam per record, and its level.
series = []
for t, arf, pci, idxs, lv in recs:
    if not lv:
        continue
    best = max(lv.items(), key=lambda kv: kv[1])
    series.append((t, arf, pci, idxs, best[0], best[1]))

# WINDOW MEDIANS, NOT ADJACENT SAMPLES. The first version differenced consecutive
# records and MISSED the known event: byte 72 is the L3-FILTERED level, so a real 8.6 dB
# step arrives spread over ~3 samples and the largest single-sample jump was 4.92 dB,
# under a 5 dB threshold. Comparing medians either side is both more sensitive to a real
# step and less sensitive to the multi-dB sample noise these fields carry -- the same
# reason medians, not traces, settled the "progressive degradation" question.
W = 8                                   # ~1.3 s either side at 160 ms cadence
found = []
for i in range(W, len(series) - W):
    before = series[i - W:i]
    after = series[i:i + W]
    mb = st.median([r[5] for r in before])
    ma = st.median([r[5] for r in after])
    if abs(ma - mb) < step_db:
        continue
    t = series[i][0]
    a0, p0, s0, i0 = before[-1][1], before[-1][2], before[-1][3], before[-1][4]
    a1, p1, s1, i1 = after[0][1], after[0][2], after[0][3], after[0][4]
    if a0 != a1 or p0 != p1:
        kind = "HANDOVER (ARFCN/PCI changed) -- not our signature"
    elif s0 != s1:
        kind = f"SSB INDEX SET CHANGED {s0}->{s1} -- beam-set reconfiguration, different event"
    elif i0 != i1:
        kind = f"SERVING BEAM SWITCHED SSB{i0}->SSB{i1} -- a real beam switch, different event"
    else:
        kind = f"SIGNATURE MATCH -- SSB{i1} already strongest, index set and cell constant"
    found.append((t, mb, ma, kind))

# Collapse each run of consecutive triggers to its largest excursion: one step produces
# a detection at every offset where the windows straddle it.
events = []
for e in found:
    if events and e[0] - events[-1][-1][0] <= 3.0:
        events[-1].append(e)
    else:
        events.append([e])
if not events:
    print(f"  no level step >= {step_db:.1f} dB (window medians, +/-{W} samples) in this capture")
for run in events:
    t, mb, ma, kind = max(run, key=lambda e: abs(e[2] - e[1]))
    print(f"  {U(t)}Z  {mb:.2f} -> {ma:.2f} dB ({ma-mb:+.2f})   [{len(run)} consecutive triggers]")
    print(f"      {kind}")
