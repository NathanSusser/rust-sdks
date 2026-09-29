#!/usr/bin/env bash
# Capture a full-mask Qualcomm DIAG log from the RM520N-GL as a DLF (QCAT/QXDM).
# Usage: capture.sh SECONDS [label]
# Runs as the invoking user (needs `dialout`); always turns modem logging back off.
set -u
# Launched unattended (setsid nohup over ssh), stdout may be a pipe that closes
# mid-run. Never let a failed echo kill this shell before diag-log-off runs.
trap '' PIPE
dur=${1:?usage: capture.sh SECONDS [label]}
label=${2:-cap}
dir=$(dirname "$(readlink -f "$0")")
port=/dev/ttyUSB0
mkdir -p ~/diag-logs
# TRUE UTC, not the host clock. `date -u` here returns the HOST clock formatted as if
# it were UTC, and this box free-runs ~15.8 s behind real UTC (PTP has no UTC anchor --
# see teleop-dlf-modem-time). So a stamp of ...T185351Z was really 18:54:07Z, and
# anyone joining these files to Host A's by filename inherits that error. Host A caught
# it on 2026-09-25; it is the same class as the QCAT package labels on the 23rd.
#
# HOST_MINUS_UTC is exported by run-cell-b.sh, which measures it at pre-flight. If it is
# absent we fall back to the host clock and mark the stamp with a trailing `h` instead of
# `Z`, so a filename NEVER claims UTC it does not have. The `h` breaks no consumer:
# run-cell-b.sh's stamp regex matches [0-9]{8}T[0-9]{6} and the room glob is $ROOM-*.
_stamp() {
  local off=${HOST_MINUS_UTC:-}
  if [ -n "$off" ]; then
    date -u -d "@$(awk "BEGIN{printf \"%.0f\", $(date +%s) - ($off)}")" +%Y%m%dT%H%M%SZ
  else
    date -u +%Y%m%dT%H%M%Sh
  fi
}
out=~/diag-logs/${label}-$(_stamp)

# One reader per DIAG port: two captures interleave reads and corrupt both files.
exec 9>"$dir/.ttyUSB0.lock"
if ! flock -n 9; then
  echo "another capture holds $port (lock $dir/.ttyUSB0.lock); not starting" >&2
  exit 1
fi
# flock only excludes other runs of THIS tool. Anything else holding the DIAG port
# corrupts both readers, so check the port itself too (Host A does this; we did not).
if command -v fuser >/dev/null 2>&1 && fuser "$port" >/dev/null 2>&1; then
  echo "another process already holds $port; two diag clients corrupt each other:" >&2
  fuser -v "$port" >&2
  exit 1
fi

# Match only a Python interpreter running QCSuper, not shells whose command text mentions it.
qc_re='^[^ ]*python[0-9.]* [^ ]*qcsuper'
if pgrep -f "$qc_re" >/dev/null; then
  echo "a qcsuper process is already running:" >&2; pgrep -af "$qc_re" >&2
  exit 1
fi

# Which QCSuper launcher: qcsuper-noroot is a symlink switched between builds in one
# step (ln -sfn + mv -T). QCSUPER_BIN overrides it for testing a candidate.
qcbin=${QCSUPER_BIN:-$dir/qcsuper-noroot}
# MASK=nr5g limits the log mask to 0xB800-0xB9FF (fast build only).
[ "${MASK:-full}" = nr5g ] && export QCSUPER_NR5G_ONLY=1

# Disk: the fast reader records the modem's real log rate, ~8 MB/s at full mask
# (~30 GB/h). Refuse rather than fill the disk mid-run.
need_kb=$(( dur * 10 * 1024 + 5 * 1024 * 1024 ))
avail_kb=$(df -Pk ~/diag-logs | awk 'NR==2 {print $4}')
if [ "$avail_kb" -lt "$need_kb" ]; then
  echo "not starting: ${avail_kb} KB free in ~/diag-logs, need ${need_kb} KB (10 MB/s x ${dur}s + 5 GB)" >&2
  exit 1
fi

echo "capturing ${dur}s -> $out.dlf ($(basename "$(readlink -f "$qcbin")"), mask ${MASK:-full})"
# SIGINT so QCSuper closes the DLF cleanly; KILL only if it hangs 30 s past that.
# Each dropped frame logs its whole payload (~0.5 MB/s of log under full mask);
# keep one short line per drop so the count survives and the log stays small.
#
# Ctrl-C: `timeout` runs QCSuper in its own process group, so a terminal Ctrl-C
# never reaches it. Tested before this trap existed: the capture ran on to its full
# duration and the log filter died, losing every later line (CRC drops undercounted).
# So trap it here, forward ONE SIGINT through timeout, keep the shell alive, and
# make the log filter and diag-log-off ignore it.
# Teardown must run however we leave, not only on the happy path and the signals we
# trapped: `set -u` on an unset variable exits immediately, and QCSuper's shutdown can
# lose the log-mask-off reply under load, leaving the modem streaming MB/s into every
# later capture. Host A runs diag-log-off from an EXIT trap for exactly this; matching.
# Idempotent via logoff_done so the normal path does not run it twice.
logoff_done=0
logoff() {
  [ "$logoff_done" = 1 ] && return 0
  logoff_done=1
  pkill -P $$ -x sleep 2>/dev/null
  (trap '' INT TERM HUP; exec "$dir/diag-log-off" "$port")
}
tpid="" stopping=0
stop() {
  [ "$stopping" = 1 ] && return; stopping=1
  echo "interrupted: stopping QCSuper cleanly, then turning modem logging off" >&2
  [ -n "$tpid" ] && kill -INT "$tpid" 2>/dev/null
}
trap stop INT TERM HUP
trap 'logoff >/dev/null 2>&1' EXIT
# QCSuper can die during startup ("unmatched response received" before "Enabled
# logging for", seen 2026-09-15 two seconds after a previous capture): a few KB of
# DLF and a silent exit. Detect an early exit that nobody asked for, turn logging
# off, and relaunch for the remaining time, appending to the same DLF and log.
start=$(date +%s) attempt=0 early=0
while :; do
  attempt=$((attempt + 1))
  left=$(( dur - ($(date +%s) - start) ))
  if [ "$attempt" -gt 1 ]; then
    # `timeout 0` means no timeout at all: never relaunch into a vanishing window.
    [ "$left" -le 5 ] && { echo "WARNING: window over before relaunch; giving up" >&2; break; }
    echo "retrying for the remaining ${left}s, appending to $out.dlf" >&2
  fi
  # `sleep infinity |` holds QCSuper's stdin open. Host A found that backgrounded
  # with stdin at EOF it stops after ~4 s with no error, and run-cell-b.sh launches
  # this script with `</dev/null`, so our stdin IS at EOF. It has never actually
  # bitten here -- zero early exits across every capture log, and the retry loop
  # below has never fired -- so this is insurance, not a fix for an observed fault.
  sleep infinity | PYTHONUNBUFFERED=1 timeout -s INT -k 30 "$left" \
    "$qcbin" --usb-modem "$port" --dlf-dump "$out.dlf" \
    > >(trap '' INT TERM HUP; exec sed -u -E 's/(Wrong CRC).*/\1/' >>"$out.log") 2>&1 &
  tpid=$!   # LAST element of the pipeline = timeout, which is the one to signal.
            # Host A's note: with a pipe (not process substitution) on the OUTPUT
            # side this would be the filter instead, and cleanup would kill the
            # filter while QCSuper kept the port. Our output is >(...), so it is not.
  # POLL, do not `wait`. `wait "$tpid"` waits for the whole pipeline JOB, and the
  # job now contains `sleep infinity`, which never exits -- so wait never returns.
  # Host A's script polls with kill -0 for this reason. Verified: the wait form
  # hangs forever here once the stdin holder was added.
  while kill -0 "$tpid" 2>/dev/null; do sleep 0.5; done
  pkill -P $$ -x sleep 2>/dev/null   # reap the stdin holder; exact name, our children only
  [ "$stopping" = 1 ] && break
  left=$(( dur - ($(date +%s) - start) ))
  [ "$left" -le 5 ] && break
  early=1
  echo "WARNING: QCSuper exited after $((dur - left))s of ${dur}s (attempt $attempt); see $out.log" >&2
  if [ "$attempt" -ge 3 ] || [ "$left" -lt 20 ]; then
    echo "WARNING: giving up; DLF covers only part of the window" >&2; break
  fi
  (trap '' INT TERM HUP; exec "$dir/diag-log-off" "$port") >/dev/null 2>&1
  sleep 2
  [ "$stopping" = 1 ] && break
done
sleep 1  # let the log filter drain
logoff || echo "WARNING: modem still streaming DIAG logs; re-run $dir/diag-log-off"

# Put the marker where an AUDIT will trip over it. A DLF assembled across a restart
# is not the same artefact as one written straight through, and dlf-check cannot tell
# you so: "0 resyncs" means undamaged, not whole. A stderr warning has no consumer
# (this script is launched setsid/& and its exit code is never read) and terminal
# scrollback does not survive the week -- the filename does. Host A hit the stronger
# form of this: a capture that died mid-cell reported a clean summary and exit 0.
# run-cell-b.sh globs "$ROOM-*.dlf" and reads the stamp by regex, so the suffix is
# safe for the pipeline; it is meant to be unmissable, not to break anything.
dlf="$out.dlf"
if [ "$early" = 1 ] && [ -s "$out.dlf" ]; then
  if mv -n "$out.dlf" "$out-PARTIAL.dlf" 2>/dev/null; then
    dlf="$out-PARTIAL.dlf"
    echo "NOTE: capture restarted mid-window; renamed to $(basename "$dlf")" >&2
  fi
fi

# Summary. Streaming and time-boxed: fast-build DLFs are GBs, and callers wait on
# this script before declaring the run done. dlf-check is the strict walker (a
# length that merely fits is not trusted; misalignment is resynced and reported).
crc=$(grep -c 'Wrong CRC' "$out.log" 2>/dev/null); crc=${crc:-0}
if ! summary=$(timeout 120 "$dir/dlf-check" "$dlf" 2>&1); then
  summary="$dlf: $(stat -c%s "$dlf" 2>/dev/null) bytes, summary skipped (dlf-check timed out or failed; run $dir/dlf-check by hand)"
fi
echo "$summary; $crc frames dropped on bad CRC"
grep -E '^qcsuper-noroot' "$out.log" | tail -1
if [ "$early" = 1 ]; then
  echo "capture.sh: QCSuper exited early at least once ($attempt attempt(s)); exit 3" >&2
  exit 3
fi
