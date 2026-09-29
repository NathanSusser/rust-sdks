#!/usr/bin/env python3
"""Per-second record counts per log code from a DLF, for a window around a probe.

Fast-reader DLFs run 8-13 MB/s (a cell is 3-9 GB), too large to move over the PTP cable.
Each host writes this small summary locally and only the CSV crosses. Streams the DLF.

Output columns: second_rel_probe,code,count   (code as 0xNNNN; zero counts omitted)
Header comment lines (#) record the parser, clock correction and window.

Clock: modem timestamps are network time (~UTC); both hosts run behind UTC together, so
host = modem + HOST_MINUS_UTC. Trust about +/-2 s; do not use for sub-second ordering.

Usage:
  dlf_rates.py <dlf> --probe-start-ms 1789443900000 --probe-end-ms 1789443915000 \
      [--host-minus-utc -4.548] [--before 60] [--after 60] -o dlf-rates.csv
"""
import argparse
import collections
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dlf_timeline import iter_dlf  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dlf")
    ap.add_argument("--probe-start-ms", type=int, required=True)
    ap.add_argument("--probe-end-ms", type=int, required=True)
    ap.add_argument("--host-minus-utc", type=float, default=-4.548)
    ap.add_argument("--before", type=int, default=60)
    ap.add_argument("--after", type=int, default=60)
    ap.add_argument("-o", "--output", required=True)
    args = ap.parse_args()

    p0 = args.probe_start_ms / 1000
    lo = math.floor(p0) - args.before
    hi = math.floor(args.probe_end_ms / 1000) + args.after
    counts = collections.Counter()
    stats = {}
    total = 0
    for code, t in iter_dlf(args.dlf, stats=stats):
        total += 1
        s = math.floor(t + args.host_minus_utc)
        if lo <= s <= hi:
            counts[(s - math.floor(p0), code)] += 1
    with open(args.output, "w") as f:
        f.write(f"# parser=dlf_timeline.iter_dlf (streaming, zero-run resync) records={total} gaps={stats.get('gaps', 0)}\n")
        f.write(f"# host_minus_utc_s={args.host_minus_utc} probe_start_ms={args.probe_start_ms} "
                f"probe_end_ms={args.probe_end_ms} window={lo - math.floor(p0)}..{hi - math.floor(p0)} s\n")
        f.write("second_rel_probe,code,count\n")
        for (s, code), n in sorted(counts.items()):
            f.write(f"{s},0x{code:04X},{n}\n")
    print(f"{args.dlf}: {total} records, {stats.get('gaps', 0)} gaps; wrote {len(counts)} rows to {args.output}")


if __name__ == "__main__":
    main()
