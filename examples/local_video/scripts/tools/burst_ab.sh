#!/usr/bin/env bash
# Paced vs clumped at the SAME average rate and packet size, interleaved.
#
# Unprivileged ICMP sockets are off on this host (ping_group_range "1 0"), so we drive
# ping(8) rather than a custom socket. One process at 240 pps is evenly paced; eight
# processes each at 30 pps start together and therefore clump ~8 packets per 33 ms slot,
# which is the shape a 30 fps video encoder emits. Same 240 pps, same 1200-byte payload.
#
# Interleaved A/B/A/B: this link has moved 7.3x within minutes, so running one arm to
# completion and then the other measures drift as though it were effect.
DEST="${1:-10.1.20.21}"; SECS="${2:-8}"; ROUNDS="${3:-3}"
N_PACED=$(python3 -c "print(int(240*$SECS))")
N_CLUMP=$(python3 -c "print(int(30*$SECS))")
loss() { echo "$1" | grep -oE '[0-9.]+% packet loss' | tr -d '%' | cut -d' ' -f1; }

echo "dest $DEST   1200B payload   240 pps both arms   ${SECS}s x ${ROUNDS} rounds"
for r in $(seq 1 "$ROUNDS"); do
  o=$(ping -q -n -s 1200 -i 0.00417 -c "$N_PACED" "$DEST" 2>&1)
  p=$(loss "$o"); prtt=$(echo "$o" | grep -oE 'rtt.*' | cut -d= -f2 | cut -d/ -f2)
  tmp=$(mktemp -d)
  for i in $(seq 1 8); do
    ( ping -q -n -s 1200 -i 0.0333 -c "$N_CLUMP" "$DEST" > "$tmp/$i" 2>&1 ) &
  done
  wait
  tx=0; rx=0
  for i in $(seq 1 8); do
    t=$(grep -oE '^[0-9]+ packets transmitted' "$tmp/$i" | cut -d' ' -f1)
    x=$(grep -oE '[0-9]+ received' "$tmp/$i" | cut -d' ' -f1)
    tx=$((tx+${t:-0})); rx=$((rx+${x:-0}))
  done
  crtt=$(cat "$tmp"/* | grep -ohE 'rtt.*' | cut -d= -f2 | cut -d/ -f2 | head -1)
  rm -rf "$tmp"
  cl=$(python3 -c "print(f'{100*($tx-$rx)/max($tx,1):.1f}')")
  printf 'round %d   paced %5s%% loss (rtt %s)    clumped %5s%% loss (rtt %s)  [%d/%d]\n' \
    "$r" "$p" "${prtt:-?}" "$cl" "${crtt:-?}" "$rx" "$tx"
done
