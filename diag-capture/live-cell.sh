#!/usr/bin/env bash
# Run one live cell with the full instrument stack, and make the reduction anchor
# IMPOSSIBLE to get wrong.
#
# WHY THIS EXISTS. The 2026-09-18 2 Mbps cell was hand-orchestrated: I computed a
# candidate epoch during planning, wrote it to results/<cell>/EPOCH, then ABANDONED
# that epoch and relaunched 48 s earlier to fit Host B's DIAG deadline. The launch
# used the new epoch; the EPOCH file still held the old one. The modem reduction
# later read the file and anchored the whole 300 s window 48 s late, so A's DIAG
# rates covered 19:32:44-19:37:44 while the cell ran 19:31:56-19:36:56. The first
# 48 s of the cell was missing and 48 s of dead air was counted as cell time.
# Nothing in the rendered report looked wrong: Host B caught it only by noticing
# that the two hosts' probe_start values differed by 47 s.
#
# THE RULE THAT PREVENTS IT: the epoch is computed EXACTLY ONCE, in this script,
# from the clock at launch -- never in a planning step, never carried across a
# relaunch, never typed twice. The EPOCH file is written from that same shell
# variable, and afterwards the file is CHECKED against what at-epoch actually
# printed. A cell whose recorded anchor disagrees with its own firing time fails
# here, loudly, rather than producing a report that is quietly 48 s wrong.
#
# Usage: live-cell.sh <label> <cap_kbps> <codec> [duration_s] [lead_s]
set -uo pipefail

label=${1:?usage: live-cell.sh <label> <cap_kbps> <codec> [duration_s] [lead_s]}
cap=${2:?cap_kbps required}
codec=${3:?codec required}
DUR=${4:-300}
LEAD=${5:-60}

DIAG=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$DIAG/.." && pwd)
D="$REPO/results/$label"
mkdir -p "$D"

# THE ONE COMPUTATION. Everything downstream reads $epoch; nothing recomputes it.
epoch=$(( $(date -u +%s) + LEAD ))
printf '%s\n' "$epoch" > "$D/EPOCH"
echo "epoch     : $epoch  ($(date -u -d @"$epoch" +%H:%M:%SZ))  <- written to $D/EPOCH"
echo "cell      : $label  cap=${cap}k codec=$codec duration=${DUR}s"
echo "sfu       : ${LK_URL:-<publish-cell default>}"
echo "clip      : ${CLIP:-<publish-cell default>}"

# Recorder spans the whole window: lead-in, the cell, and a tail so the last
# scheduler items and the last packets land inside the capture.
span=$(( LEAD + DUR + 20 ))
echo "recorder  : ${span}s span (lead ${LEAD} + cell ${DUR} + 20 tail), DIAG=1"
DIAG=1 "$DIAG/hop-recorder.sh" "$label" "$span" "$D" > "$D/recorder.out" 2>&1 &
rec=$!
sleep 8
kill -0 "$rec" 2>/dev/null || { echo "recorder died before the cell started:" >&2; cat "$D/recorder.out" >&2; exit 1; }

# DURATION must be EXPORTED, not merely known. publish-cell.sh takes the cell length from
# the DURATION environment variable and falls back to 150 s when it is unset -- it is not a
# positional argument. The first version of this script used $DUR for the recorder span and
# never passed it on, so a cell requested as 300 s recorded for 380 s and PUBLISHED for 150,
# and the only sign was "duration=150s" in a log line nobody reads until afterwards. The
# recorder span and the cell length must come from the same variable or they drift apart.
DURATION="$DUR" "$REPO/teleop-test-matrix/scripts/publish-cell.sh" "$epoch" "$label" "$cap" "$codec" "$D" || {
  echo "cell did not go LIVE" >&2; kill -TERM "$rec" 2>/dev/null; exit 1; }

echo "== cell live; waiting for recorder to finish its ${span}s span =="
wait "$rec"

# THE CHECK. at-epoch prints the instant it actually fired. If that disagrees with
# the anchor we recorded, every modem number derived from this cell is misaligned
# -- so say so here instead of letting it reach a report.
fired=$(sed -n 's/.*waiting [0-9]*s until \([0-9:]*\) UTC.*/\1/p' "$D/$label.log" | head -1)
want=$(date -u -d @"$epoch" +%H:%M:%S)
echo
if [ -z "$fired" ]; then
  echo "WARNING: could not read the firing time back from $D/$label.log; verify the anchor by hand" >&2
elif [ "$fired" != "$want" ]; then
  echo "ANCHOR MISMATCH: EPOCH file says $want but at-epoch fired at $fired" >&2
  echo "Do NOT reduce the modem log against $D/EPOCH until this is resolved." >&2
  exit 1
else
  echo "anchor verified: EPOCH $epoch = $want = at-epoch firing time"
fi
echo "outputs   : $D"
