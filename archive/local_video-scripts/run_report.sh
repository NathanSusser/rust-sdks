#!/usr/bin/env bash
# Builds the latency PDF from a paired publisher/subscriber CSV, after checking
# whether the two machines' clocks agree well enough for the transport figure to
# mean anything.
#
# Usage:
#   ./run_report.sh [outdir] [title]
set -euo pipefail

OUTDIR="${1:-./results}"
TITLE="${2:-Video Metrics}"

PUB="$OUTDIR/publisher.csv"
SUB="$OUTDIR/subscriber.csv"
for f in "$PUB" "$SUB"; do
  [ -s "$f" ] || { echo "missing or empty: $f" >&2; exit 1; }
done

# Clock check first, because it decides whether the transport row in the PDF is a
# measurement or an artifact. Publisher packetize and subscriber receive are
# stamped on different machines, so their difference carries the offset between
# the two clocks. The report script drops negative samples silently, which means a
# skewed run still renders a clean-looking PDF -- hence checking here instead.
python3 - "$PUB" "$SUB" <<'PY'
import csv, statistics, sys

pub = {r["frame_id"]: r for r in csv.DictReader(open(sys.argv[1])) if r.get("frame_id")}
sub = {r["frame_id"]: r for r in csv.DictReader(open(sys.argv[2])) if r.get("frame_id")}

t = []
for fid in set(pub) & set(sub):
    p = pub[fid].get("webrtc_packetize_timestamp_us")
    s = sub[fid].get("webrtc_receive_timestamp_us")
    if p and s:
        t.append((int(s) - int(p)) / 1000.0)

if not t:
    print("clock check: no paired frames with both endpoints; transport unavailable")
    sys.exit(0)

t.sort()
neg = [v for v in t if v < 0]
pct = 100.0 * len(neg) / len(t)
print(f"clock check: paired={len(t)}  negative={len(neg)} ({pct:.1f}%)")
print(f"             transport min={t[0]:.2f}  p50={statistics.median(t):.2f}  max={t[-1]:.2f} ms")

if pct > 1.0:
    print()
    print("  WARNING: the receiving clock is behind the sending clock, so transport")
    print("  and end-to-end are shifted by roughly that offset. Per-machine stages")
    print("  (encode, assembly, decode, render) are unaffected -- each is measured")
    print("  on one clock. Sync both hosts to the same source before quoting")
    print("  transport: chrony against a common server, or GPS/PPS for sub-ms.")
elif t[0] < 1.0:
    print()
    print("  NOTE: minimum transport is under 1 ms. Plausible on a LAN, but across")
    print("  a WAN or cellular path it more likely means the receiving clock leads")
    print("  the sending one, which understates transport.")
PY

echo
python3 "$(dirname "$0")/generate_frame_report.py" \
  --publisher "$PUB" \
  --subscriber "$SUB" \
  --output "$OUTDIR/report.pdf" \
  --title "$TITLE"
