#!/usr/bin/env bash
# Wait until an absolute UTC epoch, then exec a command.
#
# Both hosts are PTP-disciplined to microseconds (software timestamping, so ptp4l
# steers the system clock directly and there is no PHC in the path). That makes an
# absolute start time a better coordination primitive than a message: a message
# costs an unknown multi-second round trip, an epoch costs nothing and both sides
# act on the same instant.
#
# It also removes the empty-room failure: the SFU deletes a room after ~60 s
# empty, and a simultaneous start means the room is never empty in the first
# place. Publisher-first was the workaround; this is the fix.
#
# Usage: at-epoch.sh <epoch_seconds> <command> [args...]
set -euo pipefail

if [ $# -lt 2 ]; then
  echo "usage: $0 <epoch_seconds> <command> [args...]" >&2
  exit 2
fi

target=$1
shift

now=$(date -u +%s)
if [ "$target" -le "$now" ]; then
  echo "at-epoch: target $target is $((now - target))s in the PAST; refusing to start late." >&2
  echo "at-epoch: a cell started late is not the cell that was scheduled." >&2
  exit 3
fi

echo "at-epoch: now $(date -u -d @"$now" +%H:%M:%S), waiting $((target - now))s until $(date -u -d @"$target" +%H:%M:%S) UTC" >&2

# Coarse sleep to one second out, then poll so the final second is tight.
while [ "$(date -u +%s)" -lt $((target - 1)) ]; do
  sleep 0.2
done
while [ "$(date -u +%s)" -lt "$target" ]; do
  sleep 0.01
done

echo "at-epoch: firing at $(date -u +%H:%M:%S.%3N) UTC" >&2
exec "$@"
