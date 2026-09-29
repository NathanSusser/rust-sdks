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
# FOUR TRAPS THIS SCRIPT EXISTS TO AVOID. 1-3 were hit for real on 2026-09-14;
# 4 was found on Host B on 2026-09-22 while converging the two implementations.
#  1. Backgrounded with stdin at EOF, QCSuper stops after ~4 s with no error.
#     Held-open stdin ran until told to stop. Hence `sleep infinity |`.
#  2. `pkill -f PATTERN` matches the shell whose own command line contains
#     PATTERN, and SIGINTs the caller mid-script. Everything here is stopped by
#     PID, never by pattern.
#  3. QCSuper's own shutdown loses the log-mask-off reply under load and leaves
#     the modem streaming MB/s, loading the baseband during every later run.
#     diag-log-off runs from an EXIT trap, so it happens however this exits.
#  4. THE FIX FOR (1) CREATES A DEADLOCK IF YOU EVER USE `wait`. `sleep infinity |`
#     puts a never-exiting process in the background JOB, and `wait "$qpid"` waits
#     for the whole job, not the one process -- so it never returns and the capture
#     hangs forever with no error. That is strictly worse than the 4 s early exit
#     being fixed. The `while kill -0 ... sleep 2` poll below is therefore LOAD
#     BEARING: do not "simplify" it to `wait`. Found on Host B, which had exactly
#     this loop shape and reproduced the hang.
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
# A MID-CELL CAPTURE DEATH MUST NOT LOOK LIKE SUCCESS. This used to `break` straight
# into the normal ending: full summary, clean dlf-check on a truncated file, rc=0, and
# a warning on stderr that nothing reads (nothing calls this script). Host B found the
# same shape on its side -- a retry logging into a file no consumer ever opened -- and
# the lesson is that a warning is only a warning if something downstream is obliged to
# see it. So: record it, MARK THE FILE, and fail.
died_midcell=0
while kill -0 "$hpid" 2>/dev/null; do
  kill -0 "$qpid" 2>/dev/null || {
    echo "WARNING: capture DIED mid-cell at $(date -u +%H:%M:%S); dlf covers only part of the run" >&2
    died_midcell=1; break; }
  sleep 2
done
sleep 3   # let the last scheduler items flush after the stream stops

cleanup

# Rename before reporting, so the marker is in the FILENAME. An audit weeks later reads
# names, not this terminal; a `-PARTIAL` suffix survives where a stderr line does not,
# and it stops a truncated capture being packaged for a vendor by mistake.
if [ "$died_midcell" = 1 ] && [ -f "$dlf" ]; then
  partial="${dlf%.dlf}-PARTIAL.dlf"
  mv -- "$dlf" "$partial" && dlf="$partial"
fi
echo
echo "dlf  : $dlf ($(du -h "$dlf" | cut -f1))"
echo "cell : $outdir/$room.jsonl"
echo
# INTEGRITY, not just size. A capture can be the right length and still be unusable:
# a wrong declared record length lands a reader inside a payload, where random bytes
# often still look like valid lengths. dlf-check walks strictly (length fits AND code
# plausible AND the next record parses) and reports resyncs; anything non-zero means
# the file is damaged and a vendor will bounce it. Host B's idea -- print it at the
# end so a degraded capture is visible without opening the file.
python3 "$DIAG/dlf-check" "$dlf" || echo "WARNING: dlf-check failed on $dlf" >&2
# `grep -c` PRINTS 0 and EXITS 1 when there are no matches, so `|| echo 0` appends a
# SECOND zero and the value becomes "0\n0". No -e here, so the exit status is harmless
# and the plain form is correct.
crc=$(grep -c 'Wrong CRC' "$qlog" 2>/dev/null)
unm=$(grep -c 'unmatched response' "$qlog" 2>/dev/null)
echo "qcsuper: $crc CRC-dropped frames, $unm unmatched responses  ($qlog)"
echo
"$PY" "$DIAG/inspect_dlf.py" "$dlf" | sed -n '/by category/,$p'

if [ "$died_midcell" = 1 ]; then
  echo >&2
  echo "=======================================================================" >&2
  echo " INCOMPLETE CAPTURE. qcsuper died while the cell was still running, so" >&2
  echo " the DLF covers only part of the run and the counts above are of a" >&2
  echo " TRUNCATED file -- dlf-check passing means it is undamaged, NOT whole." >&2
  echo " Renamed to: $dlf" >&2
  echo " Do not send this to a vendor or treat it as a completed cell." >&2
  echo "=======================================================================" >&2
  exit 1
fi
