#!/usr/bin/env python3
"""Find which ML1 byte offset carries serving-cell RSRP, using QMI as ground truth.

  ml1-findfield.py <dlf> <hops.csv> [out.txt]

The method: Host A's QMI RSRP stepped +8.00 dB at a known instant during cell5m-a.
Any ML1 field that IS serving-cell RSRP must reproduce that step. So scan every
4-byte offset in the record, compute before/after means across the known step, and
report offsets that match.

This exists because a field matched only by RANGE is not identified. Byte 72 reads
a plausible -91 dBm median and tracks like a real measurement, and it is NOT RSRP:
it shows +1.14 dB where QMI shows +8.00. Range agreement is not identification.
"""
import sys, struct, statistics
sys.path.insert(0, "/home/nsusser/diag-capture")
import dlf_records, csv

DLF, HOPS = sys.argv[1], sys.argv[2]
OUT = sys.argv[3] if len(sys.argv) > 3 else "/tmp/ml1-findfield.txt"

# ground truth: find the largest 1 Hz RSRP step in hops.csv
h = [(int(r["unix_ms"]) / 1000, int(r["nr_rsrp_dbm"]))
     for r in csv.DictReader(open(HOPS)) if r.get("nr_rsrp_dbm")]
steps = [(abs(h[i][1] - h[i-1][1]), h[i][0], h[i-1][1], h[i][1]) for i in range(1, len(h))]
steps.sort(reverse=True)
mag, when, v0, v1 = steps[0]
print(f"ground truth: QMI stepped {v0} -> {v1} dBm ({v1-v0:+d}) at {when:.1f}")

rows = []
f = open(DLF, "rb")
for lid, t, ln, off in dlf_records.iter_file(DLF, emit_offset=True):
    if lid != 0xB97F:
        continue
    f.seek(off); b = f.read(ln)
    if len(b) < 300:
        continue
    rows.append((t, b))
print(f"{len(rows)} ML1 records")

before = [b for t, b in rows if when - 6 <= t < when]
after  = [b for t, b in rows if when <= t < when + 6]
print(f"{len(before)} before, {len(after)} after")

hits = []
for i in range(0, 296, 4):
    try:
        a = statistics.mean(struct.unpack_from("<i", b, i)[0] / 128.0 for b in before)
        c = statistics.mean(struct.unpack_from("<i", b, i)[0] / 128.0 for b in after)
    except Exception:
        continue
    if -140 < a < -50 and abs((c - a) - (v1 - v0)) < 2.5:
        hits.append((i, a, c, c - a))

with open(OUT, "w") as o:
    o.write(f"ground truth {v0} -> {v1} dBm at {when:.1f}\n")
    for i, a, c, d in hits:
        line = f"byte {i:>3}: {a:8.2f} -> {c:8.2f}  ({d:+.2f} dB)  MATCHES"
        print(line); o.write(line + "\n")
    if not hits:
        print("NO offset reproduces the step -- serving-cell RSRP is not a plain int32/128 here")
        o.write("no match\n")
