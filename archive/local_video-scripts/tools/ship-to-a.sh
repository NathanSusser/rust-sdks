#!/usr/bin/env bash
# Host B: move one cell's data to Host A and delete B's copy once every file verifies.
#
#   ship-to-a.sh <room> <cell_dir> <a_host> <a_dest> [capture files...]
#
# Operator policy since 2026-09-29: Host A stores everything; B keeps nothing after a run.
# Launched detached by run-cell-b.sh. Waits for this room's tcpdump first: it cannot be
# signalled (see pcap.sh) and keeps writing the pcap until its own -G deadline.
# Nothing is deleted unless A's sha256 of EVERY file matches B's; on any failure B's copy
# stays put and the log says FAILED.
set -uo pipefail
trap '' PIPE HUP

ROOM=${1:?usage: ship-to-a.sh <room> <cell_dir> <a_host> <a_dest> [files...]}
CELL=${2:?cell_dir}
A_HOST=${3:?a_host}
DEST=${4:?a_dest}
shift 4
EXTRA=("$@")
LOG=~/teleop/ship.log
SSH=(ssh -o BatchMode=yes -o ConnectTimeout=8)
mkdir -p ~/teleop
log() { echo "[$(date -u +%FT%TZ)] $ROOM: $*" >> "$LOG"; }

tcpdump_running() {
  local p c
  for p in /proc/[0-9]*; do
    c=$(cat "$p/comm" 2>/dev/null) || continue
    [ "$c" = tcpdump ] || continue
    case "$(tr '\0' ' ' < "$p/cmdline" 2>/dev/null)" in *"/$ROOM-"*) return 0 ;; esac
  done
  return 1
}
for _ in $(seq 1 180); do tcpdump_running || break; sleep 5; done
tcpdump_running && log "WARNING: tcpdump still running after 15 min; shipping the pcap as it stands"

# Everything B wrote for this cell. hosta/ holds copies pulled FROM A, so it is not sent back.
files=()
while IFS= read -r -d '' f; do files+=("$f"); done \
  < <(find "$CELL" -path "$CELL/hosta" -prune -o -type f -print0 2>/dev/null)
for f in "${EXTRA[@]}"; do [ -f "$f" ] && files+=("$f"); done
if [ ${#files[@]} -eq 0 ]; then
  log "nothing to ship"; exit 0
fi

if ! "${SSH[@]}" "$A_HOST" "mkdir -p '$DEST'"; then
  log "FAILED: cannot reach $A_HOST; B's copy kept in $CELL"; exit 1
fi
# Flat into DEST: the cell's files and the capture files share one directory on A, as the
# manual copies always have.
if ! rsync -a --partial -e "${SSH[*]}" "${files[@]}" "$A_HOST:$DEST/" 2>>"$LOG"; then
  log "FAILED: rsync error; B's copy kept"; exit 1
fi

local_sums=$(for f in "${files[@]}"; do printf '%s  %s\n' "$(sha256sum "$f" | cut -d' ' -f1)" "$(basename "$f")"; done | sort -k2)
names=$(for f in "${files[@]}"; do printf "'%s' " "$(basename "$f")"; done)
remote_sums=$("${SSH[@]}" "$A_HOST" "cd '$DEST' && sha256sum $names" 2>/dev/null | sort -k2)
if [ -z "$remote_sums" ] || [ "$local_sums" != "$remote_sums" ]; then
  log "FAILED: checksum mismatch on A; B's copy kept"
  diff <(echo "$local_sums") <(echo "$remote_sums") >> "$LOG"
  exit 1
fi

bytes=$(du -cb "${files[@]}" | tail -1 | cut -f1)
rm -f -- "${files[@]}"
rm -rf -- "$CELL"
log "shipped ${#files[@]} files ($((bytes / 1000000)) MB) to $A_HOST:$DEST, all sha256 verified; B's copy deleted"
