#!/usr/bin/env bash
# Run a modem diag capture AROUND one publish cell, so the baseband log and the
# cell cover the same wall-clock window and can be joined afterwards.
#
# WHAT IT IS FOR. The granted uplink bitrate collapsed to 35 kbps on a link that
# measured 50 Mbps. The 5G NR MAC/ML1 items in a DLF carry the scheduler's
# actual grants -- the first instrument that can separate "the network stopped
# granting" from "our sender stopped asking".
#
# NO SUDO. The user is in `dialout`, and qcsuper-noroot disables QCSuper's
# ModemManager check (which otherwise always escalates via pkexec). Verified
# 2026-09-14: rootless capture writes NR5G RRC/MAC records.
#
# THREE TRAPS THIS SCRIPT EXISTS TO AVOID, each hit for real on 2026-09-14:
#  1. Backgrounded with stdin at EOF, QCSuper stops after ~4 s with no error.
#     Held-open stdin ran until told to stop. Hence `sleep infinity |`.
#  2. `pkill -f PATTERN` matches the shell whose own command line contains
#     PATTERN, and SIGINTs the caller mid-script. Everything here is stopped by
#     PID, never by pattern.
#  3. QCSuper's own shutdown loses the log-mask-off reply under load and leaves
#     the modem streaming MB/s, loading the baseband during every later run.
#     diag-log-off runs from an EXIT trap, so it happens however this exits.
#
# Usage: capture-around-cell.sh <label> <epoch> <room> <cap_kbps> <codec> <outdir> [extra...]
set -uo pipefail

[ $# -ge 6 ] || { echo "usage: $0 <label> <epoch> <room> <cap_kbps> <codec> <outdir> [extra...]" >&2; exit 2; }
label=$1 epoch=$2 room=$3 cap=$4 codec=$5 outdir=$6; shift 6

DIAG=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$DIAG/.." && pwd)
PY="$DIAG/venv/bin/python3"
PORT=${PORT:-/dev/ttyUSB0}
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
dlf="$DIAG/logs/${label}-${STAMP}.dlf"
qlog="$DIAG/logs/${label}-${STAMP}.qcsuper.log"

[ -r "$PORT" ] && [ -w "$PORT" ] || {
  echo "cannot open $PORT -- needs 'dialout' membership (log out and back in after adding it)" >&2; exit 1; }
"$PY" -c 'import qcsuper' 2>/dev/null || { echo "qcsuper not importable from $PY" >&2; exit 1; }
# One reader per DIAG port, enforced with the same lock Host B's ~/diag-capture/capture.sh
# takes. Where that tool lives (Host B), share its lock file so the two can never collide;
# elsewhere keep the lock beside these scripts.
if [ -d "$HOME/diag-capture" ]; then DIAG_LOCK=${DIAG_LOCK:-$HOME/diag-capture/.ttyUSB0.lock}
else DIAG_LOCK=${DIAG_LOCK:-$DIAG/.ttyUSB0.lock}; fi
exec 9>"$DIAG_LOCK"
if ! flock -n 9; then echo "another capture holds the DIAG port (lock $DIAG_LOCK); not starting" >&2; exit 1; fi
if fuser "$PORT" >/dev/null 2>&1; then
  echo "another process already holds $PORT; two diag clients corrupt each other:" >&2
  fuser -v "$PORT" >&2; exit 1
fi
mkdir -p "$DIAG/logs" "$outdir"

qpid="" cleaned=0
cleanup() {
  [ "$cleaned" = 1 ] && return; cleaned=1
  if [ -n "$qpid" ] && kill -0 "$qpid" 2>/dev/null; then
    echo "== stopping capture (pid $qpid) =="
    kill -INT "$qpid" 2>/dev/null
    for _ in $(seq 20); do kill -0 "$qpid" 2>/dev/null || break; sleep 0.5; done
    kill -0 "$qpid" 2>/dev/null && kill -TERM "$qpid" 2>/dev/null
  fi
  pkill -P $$ -x sleep 2>/dev/null   # the stdin holder; scoped to OUR children by name
  echo "== forcing modem diag logging off =="
  DIAG_LOCK_HELD=1 "$PY" "$DIAG/diag-log-off" "$PORT" \
    || echo "WARNING: modem diag logging may STILL be streaming -- run: $PY $DIAG/diag-log-off" >&2
}
trap cleanup EXIT
trap 'exit 130' INT TERM

echo "== starting diag capture -> $dlf =="
# QCSuper prints the whole bad frame on every CRC failure: 277 MB of log for a 53 MB
# DLF on 2026-09-14. Keep the event, drop the hexdump (Host B's filter).
# Filter through a process substitution, NOT a pipe: `$!` of a pipeline is its LAST
# element, which would make qpid the sed and leave qcsuper holding the port when
# cleanup signals it.
sleep infinity | "$PY" "$DIAG/qcsuper-noroot" --usb-modem "$PORT" --dlf-dump "$dlf" \
  > >(trap '' INT TERM HUP; exec sed -u -E 's/(Wrong CRC).*/\1/; s/(unmatched response received: [0-9]+).*/\1/' > "$qlog") 2>&1 &
qpid=$!

for _ in $(seq 40); do
  [ -s "$dlf" ] && break
  kill -0 "$qpid" 2>/dev/null || { echo "qcsuper exited before writing anything:" >&2
                                   grep -vE 'unmatched response|Wrong CRC' "$qlog" | tail -10 >&2; exit 1; }
  sleep 0.5
done
[ -s "$dlf" ] || { echo "qcsuper alive but wrote nothing in 20 s" >&2; exit 1; }
sleep 3
kill -0 "$qpid" 2>/dev/null || { echo "qcsuper started then DIED within 3 s -- capture is not running" >&2; exit 1; }
echo "capture live ($(stat -c%s "$dlf") bytes and growing)"

echo "== publishing cell $room at epoch $epoch ($(date -u -d @"$epoch" +%H:%M:%S) UTC) =="
out=$("$REPO/teleop-test-matrix/scripts/publish-cell.sh" "$epoch" "$room" "$cap" "$codec" "$outdir" "$@" 2>&1)
echo "$out"
hpid=$(printf '%s\n' "$out" | sed -n 's/^LIVE .* pid=\([0-9]*\) .*/\1/p' | head -1)
trap '[ -n "$hpid" ] && kill -TERM "$hpid" 2>/dev/null; exit 130' INT TERM
[ -n "$hpid" ] || { echo "cell did not go LIVE; stopping capture" >&2; exit 1; }

echo "== cell LIVE (harness pid $hpid); capturing until it exits =="
while kill -0 "$hpid" 2>/dev/null; do
  kill -0 "$qpid" 2>/dev/null || { echo "WARNING: capture DIED mid-cell at $(date -u +%H:%M:%S); dlf covers only part of the run" >&2; break; }
  sleep 2
done
sleep 3   # let the last scheduler items flush after the stream stops

cleanup
echo
echo "dlf  : $dlf ($(du -h "$dlf" | cut -f1))"
echo "cell : $outdir/$room.jsonl"
echo
"$PY" "$DIAG/inspect_dlf.py" "$dlf" | sed -n '/by category/,$p'
