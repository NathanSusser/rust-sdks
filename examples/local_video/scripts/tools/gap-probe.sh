#!/usr/bin/env bash
# Capped uplink capacity probe for the GAP between cells -- never during one.
#
# On 2026-09-10 an uncapped version of this probe (4 parallel + 1 single 4 MB upload,
# 25 s per curl) ran inside video cells and caused every "grant collapse" that night.
# So this one: refuses to start while a subscriber or modem capture is alive, caps
# each phase at MAX_TIME seconds so it cannot overrun a gap even at <2 Mbps, and logs
# the START and END of each phase in wall ms (the old uplink.csv logged only the end).
#
# It also snapshots what the modem is attached to before and after: access tech,
# registration, operator, signal. If Host A and Host B disagree on capacity, those
# say whether they were even on the same RAT/cell.
#
# Usage: gap-probe.sh <label> <outdir>     (env: MAX_TIME=8 N=4 PAY=<4,000,000-byte file>)
set -uo pipefail
[ $# -ge 2 ] || { echo "usage: $0 <label> <outdir>" >&2; exit 2; }
label=$1 out=$2
MAX_TIME=${MAX_TIME:-8} N=${N:-4}
PAY=${PAY:-$HOME/.cache/teleop-probe/up4m.bin}
URL=https://speed.cloudflare.com/__up

if pgrep -x subscriber >/dev/null || pgrep -f '^[^ ]*python[0-9.]* [^ ]*qcsuper' >/dev/null; then
  echo "a subscriber or modem capture is running: a probe now would contaminate the cell" >&2; exit 1
fi
if [ ! -s "$PAY" ]; then
  mkdir -p "$(dirname "$PAY")"
  head -c 4000000 /dev/urandom > "$PAY"      # incompressible, same size as 2026-09-10
fi
mkdir -p "$out"
csv="$out/gap-probe.csv"
[ -s "$csv" ] || echo "label,phase,start_ms,end_ms,mbps,streams,zero_streams,max_time_s" > "$csv"
ms() { date +%s%3N; }

snapshot() {
  {
    echo "== $label $1 $(date -u +%FT%T.%3NZ) ms=$(ms)"
    mmcli -m 0 2>/dev/null | grep -E 'access tech|signal quality|state:' | sed 's/\x1b\[[0-9;]*m//g'
    mmcli -m 0 --3gpp 2>/dev/null | grep -E 'registration|operator' || mmcli -m 0 2>/dev/null | grep -E 'registration|operator'
    mmcli -m 0 --signal-get 2>/dev/null | sed -n '/5G/,/^ *-/p'
  } >> "$out/gap-probe-modem.txt"
}

up() { curl --interface wwan0 -s -o /dev/null -w '%{speed_upload}\n' --max-time "$MAX_TIME" \
         -F "f=@$PAY" "$URL" 2>/dev/null | tail -1; }

snapshot before
mmcli -m 0 --signal-setup=5 >/dev/null 2>&1

par=$(mktemp); trap 'rm -f "$par"' EXIT
t0=$(ms)
for _ in $(seq "$N"); do up >> "$par" & done
wait
t1=$(ms)
zeros=$(awk '$1==0' "$par" | wc -l)
agg=$(awk '{s+=$1} END {printf "%.3f", s*8/1000000}' "$par")
echo "$label,parallel,$t0,$t1,$agg,$N,$zeros,$MAX_TIME" >> "$csv"

t2=$(ms)
one=$(awk -v v="$(up)" 'BEGIN{printf "%.3f", v*8/1000000}')
t3=$(ms)
echo "$label,single,$t2,$t3,$one,1,$([ "$one" = 0.000 ] && echo 1 || echo 0),$MAX_TIME" >> "$csv"

snapshot after
echo "gap-probe $label: parallel ${agg} Mbps (${N} streams, $zeros zero) $((t1 - t0)) ms; single ${one} Mbps $((t3 - t2)) ms; window $t0..$t3"
