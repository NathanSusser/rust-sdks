#!/usr/bin/env bash
# Long uplink stream with every Host A hop recorded on one clock.
#
# Starts hop-recorder.sh (per-second kernel queue / driver / modem counters and
# 5G signal, a header-only wwan0 capture, and -- with DIAG=1, the default here --
# the full modem diag log), then publishes one cell for DURATION seconds at an
# absolute epoch, so the partner host's subscriber and recorder cover the same
# window. Stops by itself; modem logging is forced off on every exit path.
#
# Usage: long-run.sh <label> <epoch> <duration_s> <cap_kbps> <codec> [extra harness args...]
set -uo pipefail
[ $# -ge 5 ] || { echo "usage: $0 <label> <epoch> <duration_s> <cap_kbps> <codec> [extra...]" >&2; exit 2; }
label=$1 epoch=$2 dur=$3 cap=$4 codec=$5; shift 5
DIAG_DIR=$(cd "$(dirname "$0")" && pwd); REPO=$(cd "$DIAG_DIR/.." && pwd)
out="${OUT:-$REPO/results/22-long-run/$label}"; mkdir -p "$out"
room="$label"

now=$(date -u +%s)
[ "$epoch" -gt $((now + 20)) ] || { echo "epoch must be at least 20 s away (recorder needs to be live first)" >&2; exit 2; }
if fuser /dev/ttyUSB0 >/dev/null 2>&1; then echo "diag port busy -- another capture is running" >&2; exit 1; fi

# Recorder covers 10 s before the epoch through 20 s after the run ends.
rec_dur=$(( epoch - now + 5 + dur + 20 ))
DIAG=${DIAG:-1} "$DIAG_DIR/hop-recorder.sh" "$label" "$rec_dur" "$out" > "$out/recorder.out" 2>&1 &
rpid=$!
# Background children ignore SIGINT in a non-interactive shell, so an interrupt is
# forwarded as TERM: the recorder's trap runs its cleanup (qcsuper stop, diag-log-off),
# and any publisher this run started is stopped. Verified by SIGINT to the process group.
hpid=""
stop_all() {
  [ -n "$hpid" ] && kill -TERM "$hpid" 2>/dev/null
  kill -TERM "$rpid" 2>/dev/null
  wait "$rpid" 2>/dev/null
}
trap stop_all EXIT
trap 'exit 130' INT TERM

sleep 5
kill -0 "$rpid" 2>/dev/null || { echo "recorder died at startup:" >&2; cat "$out/recorder.out" >&2; exit 1; }
[ -s "$out/$label.hops.csv" ] || { echo "recorder not writing samples" >&2; exit 1; }
echo "recorder live -> $out"

echo "publishing room $room for ${dur}s at $(date -u -d @"$epoch" +%H:%M:%S) UTC"
pc_out=$(DURATION="$dur" "$REPO/teleop-test-matrix/scripts/publish-cell.sh" "$epoch" "$room" "$cap" "$codec" "$out" "$@") || {
  echo "$pc_out"; echo "publisher did not go LIVE -- stopping" >&2; exit 1; }
echo "$pc_out"
hpid=$(printf '%s\n' "$pc_out" | sed -n 's/^LIVE .* pid=\([0-9]*\) .*/\1/p' | head -1)

while kill -0 "$rpid" 2>/dev/null; do sleep 1; done   # recorder outlives the cell by design
trap - EXIT INT TERM
wait "$rpid" 2>/dev/null
cat "$out/recorder.out"
echo "done: $out"
