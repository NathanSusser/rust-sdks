#!/usr/bin/env python3
"""Report which diag log item IDs actually landed in a QCSuper DLF capture.

DLF framing: each record is
    uint16 len | uint16 log_id | uint64 timestamp | payload[len-12]
Counts are grouped by log_id so we can tell whether the 5G NR ML1/MAC items
that carry UL rank and grant data were ever requested by the capture tool.
"""
import struct, sys
from collections import Counter

# Log ID ranges, per Qualcomm's diag allocation.
RANGES = [
    (0xB0C0, 0xB0FF, "LTE RRC / NAS"),
    (0xB800, 0xB8FF, "NR5G RRC / NAS"),
    (0xB880, 0xB8BF, "NR5G MAC"),
    (0xB970, 0xB9FF, "NR5G ML1 (PHY: rank, MCS, grants)"),
    (0x4000, 0x4FFF, "1x / legacy"),
]

def classify(log_id):
    # Narrowest range first: NR5G MAC (0xB880-0xB8BF) sits inside the NR5G
    # RRC/NAS span (0xB800-0xB8FF), and a first-match scan in list order filed
    # every MAC record as RRC/NAS -- which made the verdict below say "no MAC".
    for lo, hi, name in sorted(RANGES, key=lambda r: r[1] - r[0]):
        if lo <= log_id <= hi:
            return name
    return "other"

def main(path):
    import os, sys as _s
    _s.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from dlf_records import records
    counts, total, stats = Counter(), 0, {}
    with open(path, "rb") as f:
        data = f.read()
    for log_id, _t, _ln in records(data, stats=stats):
        counts[log_id] += 1
        total += 1
    bad = stats.get("resyncs", 0)
    print(f"file    : {path}")
    print(f"records : {total}   ({len(data)} bytes, {bad} resyncs past malformed records, {stats.get('skipped_bytes', 0)} bytes skipped)")
    if not total:
        print("\nNo parseable records — capture is empty.")
        return
    print(f"\n{'log_id':>8}  {'count':>7}  category")
    for log_id, n in counts.most_common(40):
        print(f"  0x{log_id:04X}  {n:7d}  {classify(log_id)}")
    cat = Counter()
    for log_id, n in counts.items():
        cat[classify(log_id)] += n
    print("\nby category:")
    for name, n in cat.most_common():
        print(f"  {n:8d}  {name}")
    ml1 = cat.get("NR5G ML1 (PHY: rank, MCS, grants)", 0)
    mac = cat.get("NR5G MAC", 0)
    print("\nverdict:", "5G PHY/MAC records present — scheduling data is in here."
          if (ml1 or mac) else
          "NO 5G ML1/MAC records — QCSuper did not enable those log masks.")

if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: inspect_dlf.py <capture.dlf>")
    main(sys.argv[1])
