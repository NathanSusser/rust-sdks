#!/usr/bin/env bash
# Measure one qcsuper-noroot build on this host's DIAG port, so a candidate can be compared
# against the build it replaces before an experiment depends on it.
#   start      does QCSuper start and write within 20 s
#   cpu        per-2 s CPU% of the QCSuper process while capturing
#   content    records (resyncing reader), NR5G share, 0xB9xx (ML1-range) records, CRC lines
#   exit       seconds from SIGINT to exit; diag-log-off must read 0 bytes afterwards
# Usage: test-qcsuper-build.sh <qcsuper-noroot file> <seconds> <label>
set -uo pipefail
cand=$1 secs=$2 label=$3
DIAG_DIR=$(cd "$(dirname "$0")" && pwd); PY="$DIAG_DIR/venv/bin/python3"
out="$DIAG_DIR/logs/buildtest-$label-$(date -u +%Y%m%dT%H%M%SZ)"
exec 9>"$DIAG_DIR/.ttyUSB0.lock"; flock -n 9 || { echo "DIAG port lock held; not testing" >&2; exit 1; }
fuser /dev/ttyUSB0 >/dev/null 2>&1 && { echo "port held; not testing" >&2; exit 1; }
t0=$(date +%s.%N)
sleep infinity | "$PY" "$cand" --usb-modem /dev/ttyUSB0 --dlf-dump "$out.dlf" \
  > >(trap '' INT TERM HUP; exec sed -u -E 's/(Wrong CRC).*/\1/; s/(unmatched response received: [0-9]+).*/\1/' > "$out.log") 2>&1 &
q=$!
for _ in $(seq 40); do [ -s "$out.dlf" ] && break; kill -0 $q 2>/dev/null || break; sleep 0.5; done
if ! kill -0 $q 2>/dev/null; then echo "START FAILED"; tail -5 "$out.log"; DIAG_LOCK_HELD=1 "$PY" "$DIAG_DIR/diag-log-off" /dev/ttyUSB0; exit 1; fi
echo "start: writing after $(awk -v a="$t0" -v b="$(date +%s.%N)" 'BEGIN{printf "%.1f", b-a}') s"
cpus=""
end=$(( $(date +%s) + secs ))
while [ "$(date +%s)" -lt "$end" ]; do
  c=$(ps -o %cpu= -p $q 2>/dev/null | tr -d ' '); cpus="$cpus ${c:-dead}"; sleep 2
done
echo "cpu% samples:$cpus"
ti=$(date +%s.%N); kill -INT $q
for _ in $(seq 60); do kill -0 $q 2>/dev/null || break; sleep 0.5; done
if kill -0 $q 2>/dev/null; then echo "exit: STILL ALIVE 30 s after SIGINT -> TERM"; kill -TERM $q; sleep 1; fi
echo "exit: $(awk -v a="$ti" -v b="$(date +%s.%N)" 'BEGIN{printf "%.1f", b-a}') s after SIGINT"
pkill -P $$ -x sleep 2>/dev/null
DIAG_LOCK_HELD=1 "$PY" "$DIAG_DIR/diag-log-off" /dev/ttyUSB0
"$PY" - "$out.dlf" "$DIAG_DIR" <<'PYX'
import sys; sys.path.insert(0, sys.argv[2])
from collections import Counter
from dlf_records import records
st = {}; ids = Counter(); n = 0
for lid, t, ln in records(open(sys.argv[1], 'rb').read(), stats=st): ids[lid] += 1; n += 1
nr = sum(v for k, v in ids.items() if 0xB800 <= k <= 0xB9FF); b9 = sum(v for k, v in ids.items() if 0xB900 <= k <= 0xB9FF)
print(f"content: {n} records, NR5G {nr}, 0xB9xx {b9} ({len([k for k in ids if 0xB900 <= k <= 0xB9FF])} codes), resyncs {st.get('resyncs')}, skipped {st.get('skipped_bytes')} B")
print("  top 0xB9xx:", " ".join(f"0x{k:04X}:{v}" for k, v in sorted(((k, v) for k, v in ids.items() if 0xB900 <= k <= 0xB9FF), key=lambda x: -x[1])[:6]) or "none")
PYX
echo "log: $(grep -c 'Wrong CRC' "$out.log") CRC lines, $(grep -c 'unmatched response' "$out.log") unmatched, $(stat -c%s "$out.dlf") B DLF  -> $out.*"
