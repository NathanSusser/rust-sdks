#!/usr/bin/env bash
# Switch the running overnight loop to overnight-driver2.sh at a cycle boundary.
# Waits for "cycle <N> summary" in the driver log, then stops the old driver by PID during
# its 20 s post-summary sleep (before it launches the next cycle), then starts driver2
# continuing at START_K=N+1 into the same night directory.
# Usage: restart-to-driver2.sh <old_driver_pid> <after_cycle_N> <night> <end_unix_s>
set -uo pipefail
old=$1 n=$2 night=$3 end=$4
REPO=/home/nsusser/code/rust-sdks
L="$REPO/results/24-overnight/$night/driver.log"; R="$REPO/results/24-overnight/$night/restart.log"
echo "$(date -u +%FT%TZ) watcher: waiting for 'cycle $n summary' (old pid $old)" >> "$R"
tail -n 0 -F "$L" 2>/dev/null | while read -r line; do
  case "$line" in *"cycle $n summary"*) break ;; esac
done
if ! kill -0 "$old" 2>/dev/null; then echo "$(date -u +%FT%TZ) old driver already gone; not starting driver2" >> "$R"; exit 1; fi
kill -TERM "$old"; for _ in $(seq 20); do kill -0 "$old" 2>/dev/null || break; sleep 0.25; done
kill -0 "$old" 2>/dev/null && { echo "$(date -u +%FT%TZ) old driver did not stop; aborting switch" >> "$R"; exit 1; }
if grep -q "cycle $((n + 1)) " "$L"; then echo "$(date -u +%FT%TZ) WARNING: old driver already logged cycle $((n+1)); check for a half-launched cycle" >> "$R"; fi
echo "$(date -u +%FT%TZ) old driver stopped after cycle $n summary; starting driver2 START_K=$((n + 1))" >> "$R"
cd "$REPO" && START_K=$((n + 1)) setsid nohup diag-capture/overnight-driver2.sh "$night" "$end" 600 2000 >> "$REPO/results/24-overnight/$night/driver2.nohup" 2>&1 < /dev/null &
echo "$(date -u +%FT%TZ) driver2 launched pid $!" >> "$R"
