#!/usr/bin/env bash
# Measure usable uplink in Mbps, printed to stdout. Per-stream detail on stderr.
#
# BOTH METHODS, REPORT THE LARGER. Each is a lower bound on true capacity and each
# fails in a different direction:
#
#   single stream    underreports this path badly -- 1358-byte MTU and NAT64 mean one
#                    TCP flow cannot fill the link. Measured 0.4 against 2.4 aggregate
#   four parallel    can be rate-limited by the endpoint. Host B saw all four streams
#                    return 0.00 while a single stream immediately after read 96.5 Mbps
#
# Taking the max means a throttled parallel probe degrades to the single-stream number
# rather than reporting a link outage that is not there. A probe that fails pessimistic
# looks like a responsible stop, which is why it went unnoticed for an hour.
set -u
PAY="${PAY:-/tmp/claude-1000/-home-nsusser-code/323bfd6c-d311-471f-8de9-bc1272bf213b/scratchpad/up4m.bin}"
N="${N:-4}"
# curl prints the speed even when it exits nonzero on --max-time, so `|| echo 0`
# appended a SECOND line per stream and inflated the zero count. Take the last line.
up() { timeout 30 curl -s -o /dev/null -w "%{speed_upload}\n" --max-time 25 \
         -F "f=@$PAY" https://speed.cloudflare.com/__up 2>/dev/null | tail -1; }

par=$(mktemp); trap 'rm -f "$par"' EXIT
for _ in $(seq "$N"); do up >> "$par" & done
wait
zeros=$(awk '$1==0' "$par" | wc -l)
agg=$(awk '{s+=$1} END {printf "%.3f", s*8/1000000}' "$par")
one=$(awk -v v="$(up)" 'BEGIN{printf "%.3f", v*8/1000000}')

echo "  parallel streams: $(tr '\n' ' ' < "$par")  aggregate ${agg} Mbps ($zeros zero)" >&2
echo "  single stream:    ${one} Mbps" >&2
[ "$zeros" -gt 0 ] && echo "  WARNING: $zeros of $N parallel streams returned 0 -- likely endpoint throttling, not link" >&2

awk -v a="$agg" -v o="$one" 'BEGIN{printf "%.3f\n", (a>o?a:o)}'
