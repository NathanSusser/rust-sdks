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

BIN="$(dirname "$0")/../../../target/release/subscriber"
[ -x "$BIN" ] || { echo "build first: cargo build --release -p local_video --features desktop --bin publisher --bin subscriber" >&2; exit 1; }

# Flag arrays are resolved HERE, before the manifest, because the manifest records the
# argv it will actually exec. Defined after it, they expand to nothing and the manifest
# silently omits flags the binary receives -- which is a run record that is wrong rather
# than merely incomplete.
TIMING_FLAG=()
[ "${SHOW_TIMING:-1}" = "1" ] && TIMING_FLAG=(--display-timestamp)


# LOW_LATENCY=1 disables WebRTC's receiver jitter buffer (zero playout delay). Off by
# default so the documented invocation is unchanged. Measured cost of the buffer under
# ~3.5 Mbps load: 8 ms at p50 and 124 ms at p95 on receive_to_gpu_complete, with no
# reduction in stall-episode count.
LOWLAT_FLAG=()
[ "${LOW_LATENCY:-0}" = "1" ] && LOWLAT_FLAG=(--low-latency)

# --- run manifest -------------------------------------------------------------
# A run has to describe itself. Five confusions in this programme came from
# reconstructing a run's meaning afterwards from shell history, scrollback and memory:
# an argv nobody recorded, a bitrate cap nobody recorded, a control that silently
# inherited a different cap, a run that overlapped the previous publisher, and a label
# ("A2-off") carrying meaning it could not hold. A run whose manifest is absent or
# incomplete is not citable.
#
# Nesting matches Host A's run_manifest.rs exactly so the two halves join per run.
# Written BEFORE the first frame -- a run killed mid-flight is the one whose
# configuration gets disputed later. Capture is best-effort: an unreadable field
# becomes null rather than failing the run.
MANIFEST="${OUTDIR}/subscriber.manifest.json"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
MAN_ARGV="$(printf '%s\037' "$BIN" --url "$URL" --room-name "$ROOM" --identity viewer-1 \
  --participant cam-1 "${TIMING_FLAG[@]}" "${LOWLAT_FLAG[@]}" --log-csv "$OUTDIR/subscriber.csv" \
  --log-start-frame-id "$START_FRAME" --log-end-frame-id "$END_FRAME")"
MAN_PATH="$MANIFEST" MAN_ARGV="$MAN_ARGV" MAN_DIR="$SCRIPT_DIR" \
MAN_START="$START_FRAME" MAN_END="$END_FRAME" MAN_FPS="$FPS" python3 - <<'PYEOF'
import json, os, pathlib, subprocess, datetime, platform, re

def read(path):
    try: return pathlib.Path(path).read_text().strip() or None
    except Exception: return None

def sh(*cmd):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        return r.stdout.strip() or None
    except Exception:
        return None

def journal(unit, pattern, since=None):
    cmd = ["journalctl", "-u", unit, "-q", "--no-pager"] + (["--since", since] if since else ["-b"])
    hits = re.findall(pattern, sh(*cmd) or "")
    return hits[-1] if hits else None

argv = [a for a in os.environ["MAN_ARGV"].split("\x1f") if a]
d = os.environ["MAN_DIR"]
doc = {
    "role": "subscriber",
    "started_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "invocation": {"argv": argv, "cwd": os.getcwd()},
    "environment": {
        "hostname": platform.node(),
        "kernel": platform.release(),
        "cpu_governor": read("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor"),
        "cpu_epp": read("/sys/devices/system/cpu/cpu0/cpufreq/energy_performance_preference"),
        "git_sha": sh("git", "-C", d, "rev-parse", "HEAD"),
        "git_dirty": bool(sh("git", "-C", d, "status", "--porcelain")),
        "ssl_cert_file": os.environ.get("SSL_CERT_FILE"),
    },
    # No requested_* here, deliberately: the subscriber requests nothing, it receives
    # whatever arrives. requested_* is publisher-only and delivered_resolutions is
    # subscriber-only; pairing them is the join that catches a silent downscale.
    "media": {"requested_fps": int(os.environ["MAN_FPS"]), "decoder_implementation": None},
    # Derived from the argv actually constructed, never from the environment variable.
    # An env var records what was asked for; argv records what the binary received. They
    # diverged here once already -- LOW_LATENCY was read into the manifest while the exec
    # line never passed the flag, which would have put a false claim in the run record.
    "flags": {
        "low_latency": "--low-latency" in argv,
        "display_timestamp": "--display-timestamp" in argv,
    },
    "sync": {
        "method": "journal",  # pmc needs root and this path has no TTY for a password
        "ptp_port_state": journal("ptp4l-B", r"to (SLAVE|MASTER|UNCALIBRATED|LISTENING)"),
        "ptp_grandmaster": journal("ptp4l-B", r"best master clock ([0-9a-f.]+)"),
        "ptp_rms_ns_start": (lambda v: int(v) if v else None)(journal("ptp4l-B", r"rms\s+(\d+)", "1 min ago")),
        "ptp_rms_ns_end": None,
        "phc2sys_servo_start": journal("phc2sys-B", r" (s[0-2]) ", "1 min ago"),
        "phc2sys_servo_end": None,
    },
    "window": {"log_start_frame_id": int(os.environ["MAN_START"]),
               "log_end_frame_id": int(os.environ["MAN_END"])},
    "outcome": None,
}
pathlib.Path(os.environ["MAN_PATH"]).write_text(json.dumps(doc, indent=2) + "\n")
PYEOF
echo "  manifest: $MANIFEST"
# ------------------------------------------------------------------------------

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

"$BIN" \
  --url "$URL" \
  --room-name "$ROOM" \
  --identity viewer-1 \
  --participant cam-1 \
  "${TIMING_FLAG[@]}" "${LOWLAT_FLAG[@]}" \
  --log-csv "$OUTDIR/subscriber.csv" \
  --log-start-frame-id "$START_FRAME" \
  --log-end-frame-id "$END_FRAME" && SUB_STATUS=0 || SUB_STATUS=$?

# The manifest MUST close even when the subscriber exits non-zero. Under set -e a failed or
# terminated run aborts the script here, leaving outcome: null -- and a run that died
# mid-flight is precisely the one whose provenance later gets disputed. Terminating a run is
# also routine, not exceptional: --log-end-frame-id assumes a frame rate, and an arm that
# collapses the frame rate makes the end frame unreachable. Arm 2b ran at 1.12 fps, so its
# end frame was about an hour away and it had to be killed.
ROWS=$(( $(wc -l < "$OUTDIR/subscriber.csv" 2>/dev/null || echo 1) - 1 ))

# Close the manifest with what actually happened, so a run carries its own outcome.
MAN_PATH="$MANIFEST" MAN_ROWS="$ROWS" MAN_CSV="$OUTDIR/subscriber.csv" MAN_STATUS="${SUB_STATUS:-}" python3 - <<'PYEOF' || true
import csv, json, os, pathlib, subprocess, datetime, re
man = pathlib.Path(os.environ["MAN_PATH"])
rows = int(os.environ["MAN_ROWS"])
csv_path = pathlib.Path(os.environ["MAN_CSV"])
doc = json.loads(man.read_text())

def journal(unit, pattern):
    try:
        out = subprocess.run(["journalctl", "-u", unit, "--since", "1 min ago", "-q", "--no-pager"],
                             capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return None
    hits = re.findall(pattern, out)
    return hits[-1] if hits else None

rms = journal("ptp4l-B", r"rms\s+(\d+)")
doc["sync"]["ptp_rms_ns_end"] = int(rms) if rms else None
doc["sync"]["phc2sys_servo_end"] = journal("phc2sys-B", r" (s[0-2]) ")

outcome = {
    "ended_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "rows_written": rows,
    "exit_reason": None,   # set below from the subscriber's actual exit status
    "first_frame_id": None, "last_frame_id": None, "elapsed_s": None,
    "delivered_resolutions": None, "resolution_changed": None,
}
# Recorded from the process's real exit status, not inferred from row count. A run with
# rows can still have been killed, and that distinction is the whole point of the field.
_status = os.environ.get("MAN_STATUS", "")
if _status == "0":
    outcome["exit_reason"] = "end_frame_reached" if rows > 0 else "exited_clean_no_rows"
elif _status in ("143", "130"):
    outcome["exit_reason"] = "terminated_by_signal_%s" % _status
elif _status:
    outcome["exit_reason"] = "exited_nonzero_%s" % _status
else:
    outcome["exit_reason"] = "unknown"
outcome["subscriber_exit_status"] = int(_status) if _status.isdigit() else None

try:
    r = list(csv.DictReader(csv_path.open()))
    if r:
        outcome["first_frame_id"] = int(r[0]["frame_id"])
        outcome["last_frame_id"] = int(r[-1]["frame_id"])
        outcome["elapsed_s"] = round(float(r[-1]["elapsed_ms"]) / 1000.0, 1)
        seen, order = set(), []
        for row in r:
            w, h = row.get("frame_width"), row.get("frame_height")
            if w and h and f"{w}x{h}" not in seen:
                seen.add(f"{w}x{h}"); order.append(f"{w}x{h}")
        outcome["delivered_resolutions"] = order or None
        outcome["resolution_changed"] = (len(order) > 1) if order else None
        doc["media"]["decoder_implementation"] = r[-1].get("decoder_implementation") or None
except Exception as exc:
    outcome["exit_reason"] = "outcome_read_failed: %s" % exc
doc["outcome"] = outcome
man.write_text(json.dumps(doc, indent=2) + "\n")
PYEOF
echo
echo "done: $OUTDIR/subscriber.csv ($ROWS frames)"
if [ "$ROWS" -le 0 ]; then
  echo "WARNING: no frames logged. Either no video arrived, or nothing rendered" >&2
  echo "(check DISPLAY). A report needs rows on both sides." >&2
fi
