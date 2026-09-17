#!/usr/bin/env python3
"""Per-second, per-code DLF record counts around a probe, for sharing across hosts without
moving multi-GB DLFs. Streams the file (dlf_records.iter_file: structural acceptance,
resync on bad structure). Host time = modem time + HOST_MINUS_UTC.

The acceptance rule is dlf_records', NOT dlf-check's: those two disagree where a capture
has malformed records (650 records apart on the 107 MB s1 capture, 2026-09-17). Earlier
versions of this file, and the headers they wrote, claimed dlf-check equivalence.

Usage: dlf-rates.py <dlf> <probe_start_unix_s> <probe_end_unix_s> <host_minus_utc_s> <out.csv> [margin_s=60]
CSV: second_rel_probe,code,count   (second_rel_probe = floor(host_ts - probe_start))

Header emits host_minus_utc_s=<signed>, negative when this host's clock reads EARLIER than
UTC (A measured -8.98 s on 2026-09-17). Both hosts emit that spelling so one reader parses
either file; the older host=modem<signed>s form is kept until no archived file needs it.
The value is the same quantity under both names: local_clock - UTC, which is what is added
to modem time to reach host time (h = t + off, line below).
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from collections import Counter
from dlf_records import iter_file

dlf, ps, pe, off, out = sys.argv[1], float(sys.argv[2]), float(sys.argv[3]), float(sys.argv[4]), sys.argv[5]
margin = float(sys.argv[6]) if len(sys.argv) > 6 else 60.0
lo, hi = ps - margin, pe + margin
counts = Counter(); st = {}; n = 0
for lid, t, _ln in iter_file(dlf, stats=st):
    n += 1
    h = t + off
    if lo <= h < hi: counts[(int((h - ps) // 1), lid)] += 1
with open(out, 'w') as f:
    f.write(f"# parser=code-c9 dlf_records.iter_file (structural acceptance: length fits, code 0x1000-0x1FFF|0xB000-0xB9FF, next parses; NOT dlf-check's rule, which differs on malformed records); "
            f"host_minus_utc_s={off:+.3f}; host=modem{off:+.3f}s; probe_start={ps:.3f} probe_end={pe:.3f}; records_total={n} resyncs={st.get('resyncs')} skipped_bytes={st.get('skipped_bytes')}\n")
    f.write("second_rel_probe,code,count\n")
    for (sec, code), c in sorted(counts.items()): f.write(f"{sec},0x{code:04X},{c}\n")
print(f"{out}: {len(counts)} rows; records_total {n}, resyncs {st.get('resyncs')}, skipped {st.get('skipped_bytes')} B")
