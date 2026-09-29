#!/usr/bin/env bash
# Host B system recorder: the "host machine" segment of the attribution chain.
#
#   sysrec.sh <seconds> <label>
#
# Samples at 1 Hz into one CSV. Everything here answers "was the MACHINE the
# reason a frame was late", which no network or modem instrument can rule out.
#
# PSI (/proc/pressure) is the point. total= is cumulative microseconds that
# runnable work was STALLED waiting for cpu/io/memory, so a delta across one
# second is directly comparable to a render gap in the same second. A 2.45 s
# render stall with a matching io-pressure delta is an I/O stall; the same gap
# with flat PSI is not, and that distinction is otherwise unobtainable.
set -uo pipefail
DUR=${1:?usage: sysrec.sh <seconds> <label>}
LABEL=${2:?usage: sysrec.sh <seconds> <label>}
mkdir -p ~/sys-logs
OUT=~/sys-logs/${LABEL}-$(date -u +%Y%m%dT%H%M%SZ).csv

# /proc/diskstats names the kernel device, not the mapper alias: /dev/mapper/...
# resolves to dm-N. Matching the alias silently yields zeros for every disk
# column, which looks exactly like an idle disk.
DEV=$(basename "$(readlink -f "$(findmnt -no SOURCE / 2>/dev/null)" 2>/dev/null)")
grep -qE " $DEV " /proc/diskstats 2>/dev/null || DEV=""

psi() { awk -v k="$1" '$1==k{for(i=2;i<=NF;i++) if($i ~ /^total=/){sub("total=","",$i); print $i}}' "/proc/pressure/$2" 2>/dev/null; }

echo "# host=$(hostname) disk=${DEV:-UNRESOLVED} dur=${DUR}s started_utc=$(date -u +%FT%TZ) epoch=$(date +%s)" > "$OUT"
echo "epoch,cpu_some_us,io_some_us,io_full_us,mem_some_us,sectors_written,ms_writing,loadavg1,mem_avail_kb" >> "$OUT"

start=$(date +%s); end=$(( start + DUR )); n=0
while :; do
  now=$(date +%s); [ "$now" -lt "$end" ] || break
  if [ -n "$DEV" ]; then
    read -r sw msw <<<"$(awk -v d="$DEV" '$3==d{print $10" "$13}' /proc/diskstats)"
  else sw=0; msw=0; fi
  printf '%s,%s,%s,%s,%s,%s,%s,%s,%s\n' \
    "$now" "$(psi some cpu)" "$(psi some io)" "$(psi full io)" "$(psi some memory)" \
    "${sw:-0}" "${msw:-0}" "$(awk '{print $1}' /proc/loadavg)" \
    "$(awk '/^MemAvailable:/{print $2}' /proc/meminfo)" >> "$OUT"
  n=$((n+1))
  # Sleep to the next whole second rather than `sleep 1`, so sampling does not
  # drift behind by the cost of the sample itself.
  next=$(( start + n )); cur=$(date +%s)
  [ "$next" -gt "$cur" ] && sleep $(( next - cur ))
done
echo "sysrec: $n samples -> $OUT"
