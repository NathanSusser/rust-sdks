#!/usr/bin/env bash
# Subscriber side of a two-machine latency test.
#
# Run this on the RECEIVING machine FIRST, then start the publisher.
#
# Needs a display. The subscriber writes a CSV row when a frame completes on the
# GPU, so with no window it renders nothing and the CSV stays empty apart from its
# header. Over SSH, export DISPLAY=:0 (and `xhost +si:localuser:$USER` on the
# console) so the window opens on the machine's own screen.
#
# Usage:
#   ./run_subscriber_test.sh <ws-url> <room> [seconds] [outdir]
set -euo pipefail

URL="${1:?usage: $0 <ws-url> <room> [seconds] [outdir]}"
ROOM="${2:?usage: $0 <ws-url> <room> [seconds] [outdir]}"
SECONDS_TO_RUN="${3:-120}"
OUTDIR="${4:-./results}"

FPS=30
# The resolution collapse completes inside the first second, so the window we used to
# discard as startup noise is now the interval under study. Must match Host A's
# run_publisher_test.sh exactly -- the two CSVs pair by frame ID, and mismatched windows
# are how a metric disagreement got mistaken for a real effect.
START_FRAME="${START_FRAME:-0}"
END_FRAME=$(( START_FRAME + FPS * SECONDS_TO_RUN ))

: "${LIVEKIT_API_KEY:?set LIVEKIT_API_KEY}"
: "${LIVEKIT_API_SECRET:?set LIVEKIT_API_SECRET}"

# A bursty 30fps duty cycle never convinces the powersave governor to ramp, which inflated
# subscriber decode from 0.65ms to 2.41ms with no symptom other than the number itself. On
# this host the governor is NOT persisted -- cpufrequtils is not installed -- so a reboot
# silently reverts it. Check rather than assume. An unreadable file yields an empty string,
# which is not 'performance', so the guard fails safe.
GOV_FILE=/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor
EPP_FILE=/sys/devices/system/cpu/cpu0/cpufreq/energy_performance_preference
GOV="$(cat "$GOV_FILE" 2>/dev/null || true)"
EPP="$(cat "$EPP_FILE" 2>/dev/null || true)"
if [ "$GOV" != "performance" ] || [ "$EPP" != "performance" ]; then
  echo >&2
  echo "  ####################################################################" >&2
  echo "  # WARNING: cpu governor is '${GOV:-unreadable}', EPP is '${EPP:-unreadable}'." >&2
  echo "  # Expected 'performance' for both. Subscriber decode and render timings" >&2
  echo "  # will be inflated and are NOT comparable to runs made at performance." >&2
  echo "  #   echo performance | sudo tee /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor" >&2
  echo "  #   echo performance | sudo tee /sys/devices/system/cpu/cpu*/cpufreq/energy_performance_preference" >&2
  echo "  # Note cpufrequtils persists the governor but NOT EPP -- that is an" >&2
  echo "  # intel_pstate knob outside its scope, so check EPP after every reboot." >&2
  echo "  ####################################################################" >&2
  echo >&2
fi

if [ -z "${DISPLAY:-}" ] && [ -z "${WAYLAND_DISPLAY:-}" ]; then
  echo "ERROR: no DISPLAY set. The subscriber logs on GPU render completion, so a" >&2
  echo "headless run produces an empty CSV. Try: DISPLAY=:0 $0 $*" >&2
  exit 1
fi

mkdir -p "$OUTDIR"

# --- run manifest -------------------------------------------------------------
# A run has to describe itself. Five separate confusions in this programme came from
# reconstructing a run's meaning afterwards from shell history, scrollback and memory:
# an argv nobody recorded, a bitrate cap nobody recorded, a control that silently
# inherited a different cap, a run that overlapped the previous publisher, and a label
# ("A2-off") carrying meaning it could not hold. A run whose manifest is absent or
# incomplete is not citable.
# Schema is shared with Host A -- keep field names identical or the two halves will not join.
MANIFEST="$OUTDIR/run_manifest.json"
json_str() { printf '%s' "$1" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))'; }
_gov="$(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor 2>/dev/null || echo unreadable)"
_epp="$(cat /sys/devices/system/cpu/cpu0/cpufreq/energy_performance_preference 2>/dev/null || echo unreadable)"
_sha="$(git -C "$(dirname "$0")" rev-parse HEAD 2>/dev/null || echo unknown)"
_dirty="$(git -C "$(dirname "$0")" status --porcelain 2>/dev/null | head -c1)"
_ptp_state="$(journalctl -u ptp4l-B -b -q --no-pager 2>/dev/null | grep -oE 'to (SLAVE|MASTER|UNCALIBRATED|LISTENING)' | tail -1)"
_ptp_gm="$(journalctl -u ptp4l-B -b -q --no-pager 2>/dev/null | grep -oE 'best master clock [0-9a-f.]+' | tail -1 | awk '{print $4}')"
_ptp_rms="$(journalctl -u ptp4l-B --since '1 min ago' -q --no-pager 2>/dev/null | grep -oE 'rms +[0-9]+' | awk '{print $2}' | tail -1)"
_phc_s="$(journalctl -u phc2sys-B --since '1 min ago' -q --no-pager 2>/dev/null | grep -oE ' s[0-2] ' | tail -1 | tr -d ' ')"
{
  printf '{\n'
  printf '  "role": "subscriber",\n'
  printf '  "host": %s,\n'            "$(json_str "$(hostname)")"
  printf '  "started_utc": %s,\n'     "$(json_str "$(date -u +%Y-%m-%dT%H:%M:%SZ)")"
  printf '  "argv": %s,\n'            "$(json_str "$BIN --url $URL --room-name $ROOM --identity viewer-1 --participant cam-1 ${TIMING_FLAG[*]} --log-csv $OUTDIR/subscriber.csv --log-start-frame-id $START_FRAME --log-end-frame-id $END_FRAME")"
  printf '  "room": %s,\n'            "$(json_str "$ROOM")"
  printf '  "url": %s,\n'             "$(json_str "$URL")"
  printf '  "git_sha": %s,\n'         "$(json_str "$_sha")"
  printf '  "git_dirty": %s,\n'       "$([ -n "$_dirty" ] && echo true || echo false)"
  printf '  "governor": %s,\n'        "$(json_str "$_gov")"
  printf '  "epp": %s,\n'             "$(json_str "$_epp")"
  printf '  "low_latency": %s,\n'     "$([ "${LOW_LATENCY:-0}" = "1" ] && echo true || echo false)"
  printf '  "show_timing": %s,\n'     "$([ "${SHOW_TIMING:-1}" = "1" ] && echo true || echo false)"
  printf '  "start_frame": %s,\n'     "$START_FRAME"
  printf '  "end_frame": %s,\n'       "$END_FRAME"
  printf '  "fps_requested": %s,\n'   "$FPS"
  printf '  "ptp_port_state_start": %s,\n'  "$(json_str "${_ptp_state:-unknown}")"
  printf '  "ptp_grandmaster": %s,\n' "$(json_str "${_ptp_gm:-unknown}")"
  printf '  "ptp_rms_ns_start": %s,\n' "${_ptp_rms:-null}"
  printf '  "phc2sys_servo_start": %s\n' "$(json_str "${_phc_s:-unknown}")"
  printf '}\n'
} > "$MANIFEST"
echo "  manifest: $MANIFEST"
# ------------------------------------------------------------------------------
BIN="$(dirname "$0")/../../../target/release/subscriber"
[ -x "$BIN" ] || { echo "build first: cargo build --release -p local_video --features desktop --bin publisher --bin subscriber" >&2; exit 1; }

echo "subscriber -> $ROOM on $URL"
echo "  frames $START_FRAME..$END_FRAME (~${SECONDS_TO_RUN}s at ${FPS}fps), exits on its own"
echo "  csv: $OUTDIR/subscriber.csv"
echo "  start the publisher now (or within ~30s)"

# The video window is always shown. --display-timestamp adds the diagnostics
# window with the live per-stage timing readout, which is what makes the pipeline
# visible during a demo. It is a second window doing GPU work on the machine being
# measured: fine on a desktop GPU, but if frames stall or the CSV stops growing
# mid-run, drop this flag first -- on a laptop it has been enough to kill the
# stream. Set SHOW_TIMING=0 to omit it.
TIMING_FLAG=()
[ "${SHOW_TIMING:-1}" = "1" ] && TIMING_FLAG=(--display-timestamp)

"$BIN" \
  --url "$URL" \
  --room-name "$ROOM" \
  --identity viewer-1 \
  --participant cam-1 \
  "${TIMING_FLAG[@]}" \
  --log-csv "$OUTDIR/subscriber.csv" \
  --log-start-frame-id "$START_FRAME" \
  --log-end-frame-id "$END_FRAME"

ROWS=$(( $(wc -l < "$OUTDIR/subscriber.csv") - 1 ))

# Close the manifest with what actually happened, so a run carries its own outcome.
python3 - "$MANIFEST" "$ROWS" "$OUTDIR/subscriber.csv" <<'PYEOF' || true
import csv, json, sys, datetime, pathlib
man, rows, csv_path = pathlib.Path(sys.argv[1]), int(sys.argv[2]), pathlib.Path(sys.argv[3])
d = json.loads(man.read_text())
d["ended_utc"] = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
d["rows_written"] = rows
try:
    r = list(csv.DictReader(csv_path.open()))
    if r:
        d["frame_id_first"], d["frame_id_last"] = int(r[0]["frame_id"]), int(r[-1]["frame_id"])
        d["elapsed_s"] = round(float(r[-1]["elapsed_ms"]) / 1000.0, 1)
        res = {f'{x["frame_width"]}x{x["frame_height"]}' for x in r if x.get("frame_width")}
        d["delivered_resolutions"] = sorted(res)
        d["resolution_changed"] = len(res) > 1
        d["decoder_implementation"] = r[-1].get("decoder_implementation")
except Exception as exc:
    d["outcome_error"] = str(exc)
d["complete"] = rows > 0
man.write_text(json.dumps(d, indent=2) + "\n")
PYEOF
echo
echo "done: $OUTDIR/subscriber.csv ($ROWS frames)"
if [ "$ROWS" -le 0 ]; then
  echo "WARNING: no frames logged. Either no video arrived, or nothing rendered" >&2
  echo "(check DISPLAY). A report needs rows on both sides." >&2
fi
