#!/usr/bin/env bash
# Deliberate uplink load at an agreed instant, for controlled tests (S1-S3 style).
#
# Unlike gap-probe.sh this does NOT refuse while a subscriber or capture is running:
# loading the uplink during a cell is the point. It runs the same traffic as the
# 2026-09-10 probe (N parallel uploads of a 4,000,000-byte incompressible payload, then
# one single upload, --max-time per curl) and logs every phase in unix ms, in Host A's
# probe.csv format, so both hosts' probe logs join the same way.
#
# Usage: upload-at.sh <start_unix_ms> <outdir> [N=12] [MAX_TIME=25]
set -uo pipefail
[ $# -ge 2 ] || { echo "usage: $0 <start_unix_ms> <outdir> [N] [MAX_TIME]" >&2; exit 2; }
start_ms=$1 out=$2 N=${3:-12} MAX_TIME=${4:-25}
PAY=${PAY:-$HOME/.cache/teleop-probe/up4m.bin}
URL=https://speed.cloudflare.com/__up
ms() { date +%s%3N; }

[ -s "$PAY" ] || { mkdir -p "$(dirname "$PAY")"; head -c 4000000 /dev/urandom > "$PAY"; }
mkdir -p "$out"
csv="$out/probe.csv"
now=$(ms)
[ "$start_ms" -gt $((now + 2000)) ] || { echo "start $start_ms is not at least 2 s in the future (now $now)" >&2; exit 1; }
python3 -c "import time; d=$start_ms/1000-time.time(); time.sleep(d if d>0 else 0)"

echo "event,unix_ms,detail" > "$csv"
echo "probe_start,$(ms),N=$N payload=$(stat -c %s "$PAY")B max_time=${MAX_TIME}s host=$(hostname)" >> "$csv"
up() {  # $1 = label for the end event
  local v
  v=$(curl --interface wwan0 -s -o /dev/null -w '%{speed_upload}' --max-time "$MAX_TIME" -F "f=@$PAY" "$URL" 2>/dev/null)
  echo "$1,$(ms),$(awk -v s="${v:-0}" 'BEGIN{printf "%.3f Mbps", s*8/1e6}')" >> "$csv"
}
pids=()
for i in $(seq "$N"); do up "parallel_end_$i" & pids+=($!); done
wait "${pids[@]}"
echo "single_start,$(ms)," >> "$csv"
up single_end
echo "probe_end,$(ms)," >> "$csv"
cat "$csv"
