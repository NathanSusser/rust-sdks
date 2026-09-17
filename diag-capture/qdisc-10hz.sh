#!/usr/bin/env bash
# 10 Hz wwan0 fq_codel sampler. The S1 driver pushback (requeues 30 -> 724/s) lasted only
# ~4 s, which 1 Hz resolves to four points; this resolves the burst's rise and fall.
# Usage: qdisc-10hz.sh <duration_s> <out.csv> [iface=wwan0]
set -u
dur=$1 out=$2 IF=${3:-wwan0}
echo "unix_ms,sent_pkts,dropped,overlimits,requeues,backlog_bytes,backlog_pkts" > "$out"
end=$(( $(date +%s) + dur ))
while [ "$(date +%s)" -lt "$end" ]; do
  t=$(date +%s%3N)
  q=$(tc -s qdisc show dev "$IF")
  a=$(sed -n 's/.*Sent [0-9]* bytes \([0-9]*\) pkt (dropped \([0-9]*\), overlimits \([0-9]*\) requeues \([0-9]*\)).*/\1,\2,\3,\4/p' <<<"$q" | head -1)
  b=$(sed -n 's/.*backlog \([0-9]*\)b \([0-9]*\)p.*/\1,\2/p' <<<"$q" | head -1)
  echo "$t,${a:-,,,},${b:-,}" >> "$out"
  # sleep only the remainder of the 100 ms slot (tc itself costs ~40 ms)
  rem=$(( 100 - ($(date +%s%3N) - t) )); [ "$rem" -gt 0 ] && sleep "0.$(printf %03d "$rem")"
done
