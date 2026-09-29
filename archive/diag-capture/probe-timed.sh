#!/usr/bin/env bash
# The 10 Sep uplink probe (teleop-test-matrix/scripts/uplink-probe.sh), IDENTICAL traffic:
# N parallel curl uploads of PAY to Cloudflare, each `timeout 30 curl --max-time 25`,
# then one single upload. Adds a unix-millisecond event log so the probe can be laid
# against the video delay: start, each parallel curl's end, single start/end, end.
#
# Usage: PAY=<4 MB file> [N=4] probe-timed.sh <out.csv>
set -u
PAY=${PAY:?set PAY to the upload payload}; N=${N:-4}; out=${1:?usage: $0 <out.csv>}
ms() { date +%s%3N; }
log() { printf '%s,%s,%s\n' "$1" "$(ms)" "${2:-}" >> "$out"; }
up() { timeout 30 curl -s -o /dev/null -w "%{speed_upload}\n" --max-time 25 \
         -F "f=@$PAY" https://speed.cloudflare.com/__up 2>/dev/null | tail -1; }
mbps() { awk -v v="${1:-0}" 'BEGIN{printf "%.3f Mbps", v*8/1000000}'; }
echo "event,unix_ms,detail" > "$out"
log probe_start "N=$N payload=$(stat -c%s "$PAY")B"
for i in $(seq "$N"); do ( v=$(up); log "parallel_end_$i" "$(mbps "$v")" ) & done
wait
log single_start
v=$(up); log single_end "$(mbps "$v")"
log probe_end
