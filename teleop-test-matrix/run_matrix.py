#!/usr/bin/env python3
"""Expand and execute the LiveKit Rust teleoperation test matrix.

Three deliberately separable modes:

  --plan      Expand matrix.yaml into the concrete run list. Executes nothing,
              needs no SFU, no harness binary and no network. Prints per-suite
              run counts, wall-time estimates, and a SEPARATE count for the
              Tier 0 shaping-free subset. Do this before committing hours of
              wall time to a matrix you have not counted.

  --dry-run   Print the exact netem/tbf and harness invocations each run would
              issue, in scheduled order, and write nothing to the network.
              Works on macOS with no root and no harness binary.

  --run       Execute. Applies shaping per cell, invokes the harness once per
              run, and writes one run record per run per run_schema.json.

Two scheduling constraints are structural, not stylistic:

  1. buffering_mode is PROCESS-GLOBAL. enable_zero_playout_delay mutates
     LK_RUNTIME state and errors if the runtime is already up without it
     (livekit/src/rtc_engine/lk_runtime.rs:39-49). It cannot be toggled per room
     or per subscriber inside one process. Runs are therefore grouped into
     process batches keyed on buffering_mode and a batch NEVER spans a
     zero_jitter boundary. Getting this wrong produces a run labelled
     zero_jitter that silently ran with the default buffer.

  2. Tier 0 is shaping-free. macOS cannot run tc/netem, but it can run real
     sessions against a live SFU. --tier0 selects the subset of cells whose axes
     need no shaping and FAILS LOUDLY if a requested suite has no such subset,
     rather than silently producing an empty plan.

Usage:
    python3 run_matrix.py --plan
    python3 run_matrix.py --plan --tier0
    python3 run_matrix.py --dry-run --suite Q7_latency_definition --tier0
    python3 run_matrix.py --run --suite T2_loss_collapse --iface eth0 \
        --harness ./target/release/teleop-harness
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
import platform
import shlex
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover - environment problem, not a code path
    sys.exit("pyyaml required:  pip install pyyaml")

HERE = Path(__file__).parent
MATRIX_PATH = HERE / "matrix.yaml"
RUNS_DIR = HERE / "runs"
SNAPSHOTS_DIR = HERE / "snapshots"
# Full harness stderr, one file per run. The run record carries only a truncated
# excerpt, and a Tier 0 sweep lost 15 runs to a transient event whose actual error
# was unrecoverable because 300 characters was all that survived.
LOGS_DIR = HERE / "logs"

# The harness exits with this when the session never established: a transient
# failure that says nothing about the cell. Mirrors EXIT_RETRYABLE in src/main.rs.
HARNESS_EXIT_RETRYABLE = 75

# Per-run overhead that is not the measurement itself: room creation, connect,
# codec negotiation, first frame, teardown, and the shaping apply/clear. Used
# only for the wall-time estimate, which is why it lives here and not in
# matrix.yaml — it is a property of the runner, not of the experiment.
PER_RUN_OVERHEAD_S = 25.0
# Cost of starting a fresh subscriber process for a new buffering_mode batch.
PER_PROCESS_OVERHEAD_S = 10.0


class PlanError(RuntimeError):
    """A plan could not be produced. Always fail loudly rather than emit an
    empty or silently-reduced plan; a plan nobody notices is empty is worse
    than no plan."""


# ---------------------------------------------------------------------------
# Matrix loading and axis access
# ---------------------------------------------------------------------------


def load_matrix(path: Path = MATRIX_PATH) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def axis(matrix: dict, name: str) -> dict:
    ax = matrix["axes"].get(name)
    if ax is None:
        raise PlanError(f"unknown axis {name!r}; matrix.yaml defines: "
                        f"{', '.join(matrix['axes'])}")
    return ax


def axis_value_names(matrix: dict, name: str) -> list:
    """Values for an axis, keyed by name where the values are dicts."""
    vals = axis(matrix, name).get("values", [])
    return [v["name"] if isinstance(v, dict) else v for v in vals]


def axis_default(matrix: dict, name: str, *, unshaped: bool = False) -> Any:
    """The value an axis holds when a suite does not sweep it.

    `unshaped=True` asks for the value that is true when NO SHAPER IS PRESENT.
    It differs from the nominal default on axes whose default is itself a shaped
    condition: owd_ms defaults to 25 ms as the Tier 1 baseline, but on a MacBook
    with no netem the added delay is genuinely 0, and recording 25 would claim a
    condition that was never applied. Recording conditions as applied rather than
    as requested is the whole point of the run record.
    """
    ax = axis(matrix, name)
    if unshaped and "unshaped" in ax:
        return ax["unshaped"]
    if "default" in ax:
        return ax["default"]
    if "baseline" in ax:
        return ax["baseline"]
    vals = axis_value_names(matrix, name)
    return vals[0] if vals else None


def needs_shaping(matrix: dict, name: str) -> bool:
    return axis(matrix, name).get("shaping") == "required"


def profile_dims(matrix: dict, profile_name: str) -> dict:
    for v in axis(matrix, "video_profile")["values"]:
        if v["name"] == profile_name:
            return v
    raise PlanError(f"unknown video_profile {profile_name!r}")


# ---------------------------------------------------------------------------
# Plan expansion
# ---------------------------------------------------------------------------


# Axes that are absent unless a suite sweeps or holds them. They have no
# meaningful "off" value in their values list, so falling back to the first
# value would silently inject a condition the suite never asked for — a Q-7 cell
# carrying `--fault baseline_soak` would run a one-hour soak with fault
# injection and report it as a latency measurement.
OPT_IN_AXES = ("fault_injection", "playout_window_ms")


def suite_conditions_base(matrix: dict, suite: dict) -> dict:
    """Every axis at its held or default value.

    A held value is still a condition and must reach the run record: a metrics
    blob whose conditions are only partially recorded is only partially
    analyzable.
    """
    cond = {}
    for name, ax in matrix["axes"].items():
        if ax.get("recorded_only"):
            continue
        cond[name] = None if name in OPT_IN_AXES else axis_default(matrix, name)
    cond.update(suite.get("hold", {}))
    return cond


def tier0_violations(matrix: dict, cond: dict, sweep: list[str]) -> list[str]:
    """Shaping axes a cell cannot honestly hold without a shaper.

    Only SWEPT shaping axes can violate. The distinction is the difference
    between a nuisance parameter and the question being asked:

    - A HELD shaping axis (T-1 holds owd_ms: 25) is a constant background
      condition. Without a shaper it collapses to the unshaped value, the cell
      still answers T-1's bitrate-vs-codec question, and the run record states
      owd_ms=0 as applied. Nothing is misrepresented.
    - A SWEPT shaping axis IS the independent variable. A jitter-tolerance cell
      at jitter_ms=40 cannot run without netem, and admitting it at 0 would
      relabel a different experiment with this cell's name.

    So a swept value is admissible only when it already equals the unshaped
    value — the loss_pct=0 and owd_ms=0 rungs of a sweep, which a bare host
    genuinely provides.
    """
    out = []
    for name in sweep:
        if not needs_shaping(matrix, name):
            continue
        if name in OPT_IN_AXES and cond.get(name) is None:
            continue
        unshaped = axis_default(matrix, name, unshaped=True)
        if cond.get(name) != unshaped:
            out.append(f"{name}={cond.get(name)} (unshaped {unshaped})")
    return out


def unshaped_conditions(matrix: dict, cond: dict) -> dict:
    """Rewrite shaping axes to the value that actually holds with no shaper.

    Applied to every Tier 0 cell after it is admitted. Without this a Tier 0 run
    records owd_ms=25 while no netem ran, and the run record asserts a condition
    that was never applied — precisely the requested-vs-actual confusion the
    schema exists to prevent.
    """
    out = dict(cond)
    for name, value in out.items():
        if not needs_shaping(matrix, name):
            continue
        # An opt-in axis left unset stays unset. Resolving it here would
        # reintroduce the first value of its list — fault_injection would come
        # back as baseline_soak, turning a two-minute latency cell into a
        # one-hour soak with fault injection.
        if name in OPT_IN_AXES and value is None:
            continue
        out[name] = axis_default(matrix, name, unshaped=True)
    return out


def is_required_cell(matrix: dict, suite_name: str, cond: dict) -> bool:
    """Whether this cell is one matrix.yaml marks required_cells.

    The av1 x zero_jitter cell is the case: it is mandatory in T-2 and Q-7 and
    needs no netem, so it survives the Tier 0 filter even though T-2 as a whole
    is a Tier 1 suite.
    """
    for req in matrix["suites"][suite_name].get("required_cells", []):
        spec = {k: v for k, v in req.items() if k != "reason"}
        if all(cond.get(k) == v for k, v in spec.items()):
            return True
    return False


def tier0_admits(matrix: dict, suite_name: str, cond: dict,
                 sweep: list[str]) -> bool:
    """Whether a cell may run in the shaping-free (Tier 0) subset.

    Two conditions, and the second is the one that matters:

    1. No shaping axis sits at a non-default value. Necessary but NOT
       sufficient.
    2. The suite declares `tier0: true`, i.e. its question survives without
       shaping.

    Rule 1 alone silently reduces a suite to a configuration that answers
    nothing. T-3 sweeps jitter_ms, so a value-only filter keeps the
    jitter_ms=0 cell and reports it as "36 Tier 0 runs" of a jitter-tolerance
    suite measured at zero jitter. Those runs would execute, produce clean
    numbers, and answer none of T-3 — the precise failure the Tier 0 filter
    exists to prevent. The same holds for T-2 at loss_pct=0, T-4 at
    concurrency=1 and T-5 at fault=none.

    The carve-out is required_cells: a cell matrix.yaml marks mandatory needs no
    netem by construction and is admitted regardless of its suite's tier
    declaration.
    """
    if tier0_violations(matrix, cond, sweep):
        return False
    if matrix["suites"][suite_name].get("tier0"):
        return True
    return is_required_cell(matrix, suite_name, cond)


def buffering_modes_for(matrix: dict, suite: dict, cond: dict) -> list[str]:
    """Which buffering modes this cell runs under.

    A suite may sweep buffering_mode for specific codecs — this is how the
    mandatory av1 x zero_jitter cell is generated without crossing every codec
    against every mode and quadrupling the matrix for no question.
    """
    per_codec = suite.get("sweep_buffering_for_codecs") or {}
    codec = cond.get("video_codec")
    if codec in per_codec:
        return list(per_codec[codec])
    return [cond.get("buffering_mode", axis_default(matrix, "buffering_mode"))]


def expand_suite(matrix: dict, suite_name: str, repeats: int,
                 tier0: bool) -> tuple[list[dict], list[str]]:
    """Expand one suite into runs. Returns (runs, skipped_cell_descriptions)."""
    suite = matrix["suites"][suite_name]
    sweep = list(suite.get("sweep", []))
    base = suite_conditions_base(matrix, suite)

    grids = []
    for name in sweep:
        # A suite may restrict a swept axis to a subset by declaring the values
        # under its own name (V0 sweeps buffering_mode over just the two hint
        # modes; sweeping zero_jitter there would measure a mechanism the units
        # gate is not testing).
        vals = suite[name] if isinstance(suite.get(name), list) \
            else axis_value_names(matrix, name)
        if not vals:
            raise PlanError(f"suite {suite_name} sweeps {name!r} which has no values")
        grids.append(vals)

    runs: list[dict] = []
    skipped: list[str] = []

    for combo in itertools.product(*grids):
        cond = dict(base)
        cond.update(dict(zip(sweep, combo)))

        for mode in buffering_modes_for(matrix, suite, cond):
            cell = dict(cond)
            cell["buffering_mode"] = mode

            if tier0:
                if not tier0_admits(matrix, suite_name, cell, sweep):
                    violations = tier0_violations(matrix, cell, sweep)
                    skipped.append(", ".join(violations) if violations
                                   else f"{suite_name} is not a Tier 0 suite")
                    continue
                cell = unshaped_conditions(matrix, cell)

            # Only swept axes plus buffering_mode identify the cell; held axes
            # are constant across the suite and would be noise in the id.
            parts = [f"{a}={cell[a]}" for a in sweep]
            if mode != cond.get("buffering_mode"):
                parts.append(f"buffering_mode={mode}")
            cell_id = ",".join(parts) or "baseline"

            for rep in range(repeats):
                runs.append({
                    "suite": suite_name,
                    "cell_id": cell_id,
                    "repeat_index": rep,
                    "conditions": cell,
                    "run_first": bool(suite.get("run_first")),
                    "duration_s": suite_duration_s(matrix, suite, cell),
                    "video_poll_hz": suite.get(
                        "video_poll_hz",
                        matrix["meta"]["parameters"]["stats_poll_hz_default"]["value"]),
                })

    return runs, skipped


def suite_duration_s(matrix: dict, suite: dict, cond: dict) -> float:
    """Run duration. The fault_injection axis carries per-value overrides
    (baseline_soak is an hour, not two minutes)."""
    if cond.get("fault_injection"):
        for v in axis(matrix, "fault_injection")["values"]:
            if v["name"] == cond["fault_injection"] and "duration_s" in v:
                return float(v["duration_s"])
    return float(suite.get("duration_s", axis(matrix, "duration_s")["default"]))


def check_required_cells(matrix: dict, suite_name: str, runs: list[dict],
                         tier0: bool) -> list[str]:
    """Assert that cells matrix.yaml marks required actually survived expansion.

    The av1 x zero_jitter cell is the one this exists for: it is mandatory in
    T-2 and Q-7, it needs no netem, and it must never be dropped by the Tier 0
    filter. A required cell silently absent is a hole in the matrix that only
    shows up as a missing row in a report weeks later.
    """
    problems = []
    for req in matrix["suites"][suite_name].get("required_cells", []):
        spec = {k: v for k, v in req.items() if k != "reason"}
        found = any(
            all(r["conditions"].get(k) == v for k, v in spec.items())
            for r in runs
        )
        if not found:
            desc = ", ".join(f"{k}={v}" for k, v in spec.items())
            problems.append(
                f"{suite_name}: required cell [{desc}] is absent from the plan"
                f"{' under --tier0' if tier0 else ''} — {req.get('reason', '')}")
    return problems


def expand(matrix: dict, suite_names: list[str], repeats: int,
           tier0: bool) -> tuple[list[dict], dict[str, list[str]]]:
    plan: list[dict] = []
    skipped: dict[str, list[str]] = {}
    for name in suite_names:
        if name not in matrix["suites"]:
            raise PlanError(f"unknown suite {name!r}; matrix.yaml defines: "
                            f"{', '.join(matrix['suites'])}")
        runs, skips = expand_suite(matrix, name, repeats, tier0)
        if tier0 and not runs:
            note = matrix["suites"][name].get("tier0_note", "")
            raise PlanError(
                f"suite {name} has no shaping-free subset: every cell depends on "
                f"an axis that requires tc/netem. {note}\n"
                f"Run it at Tier 1 on a Linux host with root, or drop it from "
                f"--suite. Refusing to emit an empty plan.")
        problems = check_required_cells(matrix, name, runs, tier0)
        if problems:
            raise PlanError("\n".join(problems))
        plan += runs
        if skips:
            skipped[name] = skips
    return plan, skipped


# ---------------------------------------------------------------------------
# Scheduling: buffering_mode is a per-process grouping key
# ---------------------------------------------------------------------------


def schedule(plan: list[dict]) -> list[dict]:
    """Order runs so each subscriber process serves exactly one buffering_mode.

    enable_zero_playout_delay is process-global and irreversible within a
    process. Sorting by (suite, buffering_mode) means every process batch is
    homogeneous in buffering_mode; `process_batch` is stamped on each run and
    carried into the run record so the analysis can verify the grouping held
    rather than trust that it did.
    """
    # run_first suites sort ahead of everything else: a validation gate that
    # runs after the matrix it validates is not a gate.
    ordered = sorted(
        plan,
        key=lambda r: (0 if r.get("run_first") else 1,
                       r["suite"], r["conditions"]["buffering_mode"],
                       r["cell_id"], r["repeat_index"]),
    )
    batch = -1
    previous = None
    for run in ordered:
        key = (run["suite"], run["conditions"]["buffering_mode"])
        if key != previous:
            batch += 1
            previous = key
        run["process_batch"] = batch
    return ordered


def batch_count(plan: list[dict]) -> int:
    return len({r["process_batch"] for r in plan}) if plan else 0


def estimate_wall_time_s(plan: list[dict]) -> float:
    measure = sum(r["duration_s"] + PER_RUN_OVERHEAD_S for r in plan)
    return measure + batch_count(plan) * PER_PROCESS_OVERHEAD_S


def fmt_duration(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 5400:
        return f"{seconds / 60:.1f}m"
    return f"{seconds / 3600:.1f}h"


# ---------------------------------------------------------------------------
# Shaping
# ---------------------------------------------------------------------------


def netem_command(matrix: dict, cond: dict, iface: str) -> str | None:
    """The single netem command for a cell.

    ONE qdisc command per cell. Delay, jitter and loss combine into one
    `tc qdisc replace` because two separate netem commands silently replace
    each other, leaving only the last one applied and the run record claiming
    conditions that were never in force.
    """
    params = []
    delay = cond.get("owd_ms") or 0
    jitter = cond.get("jitter_ms") or 0
    loss = cond.get("loss_pct") or 0

    if delay or jitter:
        params.append(f"delay {delay}ms")
        if jitter:
            params.append(f"{jitter}ms distribution normal")
    if loss:
        params.append(f"loss {loss}%")

    if not params:
        return None
    return matrix["shaping"]["netem_template"].format(
        iface=iface, params=" ".join(params))


def tbf_command(matrix: dict, cond: dict, iface: str) -> str | None:
    """Rate limiting, as a SEPARATE tbf qdisc layered on top of netem. It is not
    a netem parameter and merging it into the netem command would silently drop
    either the shaping or the rate limit."""
    rate = cond.get("uplink_mbps")
    if rate is None or rate == axis_default(matrix, "uplink_mbps"):
        return None
    return matrix["shaping"]["tbf_template"].format(iface=iface, rate=rate)


def shaping_supported() -> bool:
    return platform.system().lower() not in ("darwin", "windows")


def apply_shaping(matrix: dict, cond: dict, iface: str,
                  execute: bool) -> tuple[str | None, str | None, str | None]:
    """Returns (netem_cmd, tbf_cmd, tc_qdisc_show). Records what was APPLIED."""
    netem = netem_command(matrix, cond, iface)
    tbf = tbf_command(matrix, cond, iface)
    if not execute:
        return netem, tbf, None

    for cmd in (netem, tbf):
        if cmd:
            subprocess.run(["sh", "-c", f"sudo {cmd}"], check=True)
    show = subprocess.run(
        ["sh", "-c", matrix["shaping"]["verify"].format(iface=iface)],
        capture_output=True, text=True, check=False).stdout.strip()
    return netem, tbf, show


def clear_shaping(matrix: dict, iface: str, execute: bool) -> None:
    if execute:
        subprocess.run(
            ["sh", "-c", "sudo " + matrix["shaping"]["netem_clear"].format(iface=iface)],
            check=False)


# ---------------------------------------------------------------------------
# Harness invocation
# ---------------------------------------------------------------------------


def harness_command(matrix: dict, run: dict, args) -> list[str]:
    cond = run["conditions"]
    prof = profile_dims(matrix, cond["video_profile"])
    ref = matrix["reference_config"]
    params = matrix["meta"]["parameters"]
    buffering = axis(matrix, "buffering_mode")["settings"][cond["buffering_mode"]]

    cmd = [
        args.harness,
        "--url", args.url,
        "--room-name", f"teleop-{run['suite'].lower()}-{run['repeat_index']}",
        "--duration-s", str(int(run["duration_s"])),
        "--codec", cond["video_codec"],
        "--encoder", ref["video"]["encoder"],
        "--width", str(prof["w"]),
        "--height", str(prof["h"]),
        "--fps", str(prof["fps"]),
        "--max-bitrate", str(ref["video"]["max_bitrate"]),
        "--attach-timestamp",
        "--attach-frame-id",
        "--buffering-mode", cond["buffering_mode"],
        "--control-transport", cond["control_transport"],
        "--control-rate-hz", str(int(cond["control_rate_hz"])),
        "--control-buffer-size", str(ref["control"]["buffer_size"]),
        "--stats-poll-hz", str(params["stats_poll_hz_default"]["value"]),
        "--video-poll-hz", str(run["video_poll_hz"]),
        "--warmup-s", str(params["warmup_excluded_s"]["value"]),
        "--poll-overbudget-multiplier", str(params["poll_overbudget_multiplier"]["value"]),
        # Decoupled from the stats poll: at the stats cadence a 105 s scored window
        # yielded only 63 usable probes, too few for the percentiles the latency
        # bar is scored against.
        "--probe-rate-hz", str(params["probe_rate_hz"]["value"]),
        "--probe-lifetime-ms", str(params["probe_lifetime_ms"]["value"]),
        "--concurrency", str(int(cond["concurrency"])),
        "--snapshots-out", str(SNAPSHOTS_DIR / f"{run['run_id']}.jsonl"),
        "--publisher-seq-log", str(SNAPSHOTS_DIR / f"{run['run_id']}.seq.jsonl"),
        # Not an axis and never a cell default: the same value goes to every run in
        # a sweep, and it is `test_pattern` unless the operator asked otherwise.
        # A camera makes bitrate depend on what the lens saw, so camera_source is in
        # never_pool_across and camera runs are never aggregated with pattern runs.
        # The harness fails the run if a requested camera cannot be opened rather
        # than falling back, so a mislabelled run cannot reach the record.
        "--camera-source", args.camera_source,
    ]

    # RTSP-only flags, emitted only for an rtsp:// source so a pattern or local-device
    # run's invocation is byte-identical to what it was before RTSP existed.
    if args.camera_source.lower().startswith(("rtsp://", "rtsps://")):
        cmd += [
            "--rtsp-transport", args.rtsp_transport,
            # A wedged RTSP session leaves ffmpeg alive with its pipe open and no bytes
            # flowing; without this bound the run hangs to its full duration with nothing
            # in the log to say why.
            "--rtsp-stall-timeout-s",
            str(params["rtsp_stall_timeout_s"]["value"]),
        ]

    # No --playout-delay-* flags: the room-level hint modes are retired from
    # buffering_mode (matrix.yaml buffering_mode.retired_values). zero_jitter is
    # applied by the harness via enable_zero_playout_delay before runtime init,
    # not through the room API.

    if ref["audio"]["enabled"]:
        cmd += ["--audio", "--audio-source", ref["audio"]["source"],
                "--audio-bitrate", str(ref["audio"]["target_bitrate"])]

    if cond.get("playout_window_ms") is not None:
        cmd += ["--playout-window-ms", str(cond["playout_window_ms"])]
    if cond.get("fault_injection"):
        cmd += ["--fault", str(cond["fault_injection"])]

    return cmd


# ---------------------------------------------------------------------------
# Run record assembly
# ---------------------------------------------------------------------------


def git_sha() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                              cwd=HERE, capture_output=True, text=True,
                              check=True).stdout.strip()
    except Exception:
        return "unknown"


def redact_camera_source(value: str) -> str:
    """Strip any user:pass@ from an RTSP --camera-source before it is recorded.

    Run records are committed and shared and RTSP URLs commonly embed credentials.
    Mirrors rtsp::redact_url in the harness: only the authority is touched, so an @
    later in a stream path is not mistaken for a credential delimiter. A value that
    is not a URL comes back unchanged, so this is safe to apply unconditionally.
    """
    marker = value.find("://")
    if marker < 0:
        return value
    start = marker + 3
    end = len(value)
    for i in range(start, len(value)):
        if value[i] in "/?#":
            end = i
            break
    authority = value[start:end]
    at = authority.rfind("@")
    if at < 0:
        return value
    return f"{value[:start]}***@{authority[at + 1:]}{value[end:]}"


def ran_profile_for(path: str) -> dict:
    """On loopback and lan, ran_profile is n/a — those results do not transfer
    to cellular and the record says so rather than leaving 'unknown', which
    would imply the question was merely unanswered."""
    value = "n/a" if path in ("loopback", "lan") else "unknown"
    return {
        "rlc_mode": value,
        "aqm_mode": value,
        "pdcp_discard_timer_ms": value,
        "pdcp_reordering_timer_ms": value,
        "rlc_reassembly_timer_ms": value,
    }


def connect_retries(matrix: dict) -> int:
    """How many times a run whose session never established may be retried."""
    return int(matrix["meta"]["parameters"]["connect_retries"]["value"])


def connect_retry_backoff_s(matrix: dict) -> float:
    """Base delay before the first retry; doubles on each subsequent attempt."""
    return float(matrix["meta"]["parameters"]["connect_retry_backoff_s"]["value"])


def write_harness_log(run_id: str, attempt: int, cmd: list[str],
                      exit_code: int | None, stderr: str | None) -> Path:
    """Writes one attempt's FULL stderr, and returns the path.

    The run record keeps only a 300-character excerpt. That truncation made a real
    failure undiagnosable: 15 runs of a Tier 0 sweep died inside a 3-second window
    and the actual error had been cut off, leaving no way to tell a rate limit from
    a connectivity blip after the fact.
    """
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    path = LOGS_DIR / f"{run_id}__attempt{attempt}.log"
    with open(path, "w") as f:
        f.write(f"# run_id: {run_id}\n# attempt: {attempt}\n"
                f"# exit_code: {exit_code}\n"
                f"# command: {' '.join(shlex.quote(c) for c in cmd)}\n"
                f"# {'-' * 68}\n")
        f.write(stderr or "(no stderr)")
        if not (stderr or "").endswith("\n"):
            f.write("\n")
    return path


def build_run_record(matrix: dict, run: dict, args, *, netem: str | None,
                     tbf: str | None, tc_show: str | None, shaping_applied: bool,
                     started_utc: str, subscriber_pid: int,
                     harness_cmd: list[str], invalid: list[str],
                     attempts: list[dict] | None = None) -> dict:
    cond = run["conditions"]
    prof = profile_dims(matrix, cond["video_profile"])
    ref = matrix["reference_config"]
    params = matrix["meta"]["parameters"]
    return {
        "run_id": run["run_id"],
        "suite": run["suite"],
        "cell_id": run["cell_id"],
        "repeat_index": run["repeat_index"],
        "started_utc": started_utc,
        "ended_utc": datetime.now(timezone.utc).isoformat(),
        "duration_s": run["duration_s"],
        "tier": args.tier,
        "conditions": {
            "video_codec_requested": cond["video_codec"],
            # Filled from CodecStats by the extractor; never assumed to equal
            # the request. A cell labelled av1 that fell back is the worst data
            # point the matrix can produce.
            "video_codec_actual": None,
            "codec_mismatch": False,
            "buffering_mode": cond["buffering_mode"],
            # Always null: zero_jitter is applied via enable_zero_playout_delay,
            # not through the room API. The retired hint modes set these.
            "playout_delay_min_ms": None,
            "playout_delay_max_ms": None,
            "audio_buffering": ref["audio_buffering"],
            "control_transport": cond["control_transport"],
            "control_buffer_size": (ref["control"]["buffer_size"]
                                    if cond["control_transport"] == "data_track_buf1"
                                    else None),
            "control_max_partial_frames": (ref["control"]["max_partial_frames"]
                                           if cond["control_transport"] == "data_track_buf1"
                                           else None),
            "control_rate_hz": cond["control_rate_hz"],
            "playout_window_ms": cond.get("playout_window_ms"),
            "video_profile": cond["video_profile"],
            "video_width_requested": prof["w"],
            "video_height_requested": prof["h"],
            "video_width_actual": None,
            "video_height_actual": None,
            "video_fps_requested": prof["fps"],
            "video_max_bitrate_bps": ref["video"]["max_bitrate"],
            "simulcast": ref["video"]["simulcast"],
            "dynacast": ref["video"]["dynacast"],
            "audio_enabled": ref["audio"]["enabled"],
            "audio_direction": ref["audio"]["direction"],
            "audio_source": ref["audio"]["source"],
            "loss_pct": cond["loss_pct"],
            "owd_ms": cond["owd_ms"],
            "jitter_ms": cond["jitter_ms"],
            "uplink_mbps": cond["uplink_mbps"],
            "concurrency": cond["concurrency"],
            "fault_injection": cond.get("fault_injection"),
            "netem_cmd": netem,
            "tbf_cmd": tbf,
            "tc_qdisc_show": tc_show,
            "shaping_applied": shaping_applied,
        },
        "environment": {
            "path": args.path,
            "encoder_tier": "unknown",
            "encoder_requested": ref["video"]["encoder"],
            "encoder_implementation": None,
            "decoder_implementation": None,
            "power_efficient_encoder": None,
            "ran_profile": ran_profile_for(args.path),
            # The requested value. parse_runs.py overwrites it from the harness's
            # run_metadata record with the source that was actually opened, which is
            # what keys the never_pool_across group.
            # The requested value, with any RTSP credentials stripped: this record is
            # committed and shared.
            "camera_source": redact_camera_source(args.camera_source),
            "camera_device": None,
            "host_id": platform.node(),
            "host_os": f"{platform.system()} {platform.release()}",
            "host_arch": platform.machine(),
            "load_generator_hosts": args.load_generator_host or [],
            "sfu_url": args.url,
            "sfu_version": None,
            "sdk_git_sha": git_sha(),
            "build_config": args.build_config,
            "clock_source": args.clock_source,
        },
        "harness": {
            "video_poll_hz": run["video_poll_hz"],
            "stats_poll_hz": params["stats_poll_hz_default"]["value"],
            "warmup_excluded_s": params["warmup_excluded_s"]["value"],
            "poll_overbudget_multiplier": params["poll_overbudget_multiplier"]["value"],
            "subscriber_process_id": subscriber_pid,
            "publisher_process_id": None,
            "scored_window_start_unix_us": None,
            "scored_window_end_unix_us": None,
            "playout_units_confirmed": None,
            "publisher_seq_log": str(SNAPSHOTS_DIR / f"{run['run_id']}.seq.jsonl"),
            "harness_version": None,
            # Redacted argv: an rtsp:// --camera-source embeds user:pass and this
            # record is committed and shared. The invocation stays reproducible --
            # only the credential is replaced.
            "harness_cmd": [redact_camera_source(a) for a in harness_cmd],
        },
        # Extraction is parse_runs.py's job, not the runner's: making
        # differencing an analysis-side operation means a bug in it is fixable
        # without re-running the matrix.
        "metrics": {},
        "distributions": {},
        "events": [],
        "raw": {
            "snapshots_jsonl_path": str(SNAPSHOTS_DIR / f"{run['run_id']}.jsonl"),
            "publisher_seq_log_path": str(SNAPSHOTS_DIR / f"{run['run_id']}.seq.jsonl"),
            # One entry per harness invocation, each naming the log holding its
            # FULL stderr. More than one entry means a connection failure was
            # retried; the record says so rather than presenting the successful
            # attempt as if it had been the only one.
            "harness_attempts": attempts or [],
        },
        "validity": {
            "valid": not invalid,
            "invalid_reasons": [],
            "invalid_detail": invalid,
            "clock_sync_confidence": "none",
            "theta_ms": None,
            # Filled by the extractor from the frame-timing log. Left null here
            # rather than defaulted to 100: a missing coverage figure must not
            # read as full coverage, which is the failure this gate exists for.
            "g2g_metadata_coverage_pct": None,
            "frames_received": None,
            "frames_with_metadata": None,
            "warmup_excluded_s": params["warmup_excluded_s"]["value"],
            "samples_scored": None,
            "notes": [],
        },
    }


# ---------------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------------


def print_plan(matrix: dict, args) -> int:
    suites = resolve_suites(matrix, args)

    # With --tier0, expand under the Tier 0 filter FIRST so a suite with no
    # shaping-free subset raises here and the user sees the refusal. Printing a
    # full-matrix plan under a --tier0 flag would report runs the requested tier
    # cannot execute, which is worse than an error.
    if args.tier0:
        expand(matrix, suites, args.repeats, tier0=True)

    full, _ = expand(matrix, suites, args.repeats, tier0=False)
    full = schedule(full)

    # The Tier 0 subset is computed for every suite that declares one, so the
    # count is reported even when the main plan is the full matrix. This is the
    # number the user needs first: it is what a MacBook can actually run.
    tier0: list[dict] = []
    tier0_skips: dict[str, list[str]] = {}
    for name in suites:
        try:
            runs, skips = expand(matrix, [name], args.repeats, tier0=True)
        except PlanError:
            # No shaping-free subset. Reported below as Tier 1 only, never
            # silently counted as zero Tier 0 runs of a runnable suite.
            continue
        tier0 += schedule(runs)
        if skips:
            tier0_skips[name] = skips.get(name, [])
    tier0 = schedule(tier0)

    print(f"MATRIX PLAN  —  {args.repeats} repeats per cell "
          f"(matrix.yaml: default_repeats="
          f"{matrix['meta']['parameters']['default_repeats']['value']})\n")

    by_suite: dict[str, list[dict]] = {}
    for r in full:
        by_suite.setdefault(r["suite"], []).append(r)

    tier0_by_suite: dict[str, list[dict]] = {}
    for r in tier0:
        tier0_by_suite.setdefault(r["suite"], []).append(r)

    header = f"{'suite':<24}{'runs':>7}{'cells':>7}{'procs':>7}{'wall':>10}   {'tier0 runs':>10}"
    print(header)
    print("-" * len(header))
    for name in suites:
        runs = by_suite.get(name, [])
        cells = len({r["cell_id"] for r in runs})
        t0 = tier0_by_suite.get(name, [])
        t0_label = str(len(t0)) if t0 else "—"
        print(f"{name:<24}{len(runs):>7}{cells:>7}{batch_count(runs):>7}"
              f"{fmt_duration(estimate_wall_time_s(runs)):>10}   {t0_label:>10}")
    print("-" * len(header))
    print(f"{'TOTAL':<24}{len(full):>7}"
          f"{len({(r['suite'], r['cell_id']) for r in full}):>7}"
          f"{batch_count(full):>7}"
          f"{fmt_duration(estimate_wall_time_s(full)):>10}"
          f"   {len(tier0):>10}")

    print(f"\nTier 0 (shaping-free) subset: {len(tier0)} runs, "
          f"{len({(r['suite'], r['cell_id']) for r in tier0})} cells, "
          f"{batch_count(tier0)} processes, "
          f"~{fmt_duration(estimate_wall_time_s(tier0))} wall time.")
    # Derived from what expansion ACTUALLY produced, never from the static
    # `tier0:` flag. A suite declared tier0: false still contributes when a
    # required_cell bypasses the filter — T-2 is the case — and a summary that
    # disagrees with the table printed three lines above it is worse than no
    # summary, because a reader who trusts it concludes T-2 contributes nothing.
    contributing = [s for s in suites if tier0_by_suite.get(s)]
    if contributing:
        labelled = []
        for name in contributing:
            if matrix["suites"][name].get("tier0"):
                labelled.append(name)
            else:
                # Only its mandatory cells got through; the suite's own question
                # still needs Tier 1.
                labelled.append(f"{name} (required cells only)")
        print(f"  Suites contributing Tier 0 runs: {', '.join(labelled)}")
    no_t0 = [s for s in suites if s not in tier0_by_suite]
    if no_t0:
        print(f"  No shaping-free subset (Tier 1 only): {', '.join(no_t0)}")
        for name in no_t0:
            note = matrix["suites"][name].get("tier0_note", "").strip()
            if note:
                print(f"    {name}: {' '.join(note.split())}")

    print(f"\nWall-time estimate = sum(duration_s) + {PER_RUN_OVERHEAD_S:.0f}s/run "
          f"setup+teardown + {PER_PROCESS_OVERHEAD_S:.0f}s per process batch.")
    print("Process batches are keyed on (suite, buffering_mode): "
          "enable_zero_playout_delay is process-global and cannot be toggled "
          "within a process, so no batch spans a zero_jitter boundary.")

    gates = [n for n in suites if matrix["suites"][n].get("validation_gate")]
    if gates:
        print(f"\nVALIDATION GATE — run before scoring anything else: "
              f"{', '.join(gates)}")
        for name in gates:
            rule = " ".join(matrix["suites"][name].get("decision_rule", "").split())
            if rule:
                print(f"  {name}: {rule}")

    required = []
    for name in suites:
        for req in matrix["suites"][name].get("required_cells", []):
            spec = ", ".join(f"{k}={v}" for k, v in req.items() if k != "reason")
            n_full = sum(1 for r in full if r["suite"] == name and all(
                r["conditions"].get(k) == v for k, v in req.items() if k != "reason"))
            n_t0 = sum(1 for r in tier0 if r["suite"] == name and all(
                r["conditions"].get(k) == v for k, v in req.items() if k != "reason"))
            required.append(f"  {name}: [{spec}] — {n_full} runs in full plan, "
                            f"{n_t0} at Tier 0")
    if required:
        print("\nRequired cells (matrix.yaml required_cells, never filtered out):")
        print("\n".join(required))

    if args.verbose:
        print("\nFirst 20 run ids:")
        target = tier0 if args.tier0 else full
        for r in target[:20]:
            print(f"  [batch {r['process_batch']:>2}] {r['suite']}__{r['cell_id']}"
                  f"__r{r['repeat_index']}")
        if len(target) > 20:
            print(f"  ... and {len(target) - 20} more")

    return 0


def resolve_suites(matrix: dict, args) -> list[str]:
    """Which suites this invocation covers.

    An EXPLICITLY named suite is always honored, so --tier0 --suite T3 fails
    loudly rather than quietly returning nothing: the user asked for a specific
    thing this tier cannot do, and silence would be a lie.

    With no --suite the user is asking what this host can run, so under --tier0
    the default list narrows to the suites that have a shaping-free subset.
    """
    if args.suite:
        return args.suite
    names = list(matrix["suites"].keys())
    if not args.tier0:
        return names
    runnable = []
    for name in names:
        try:
            expand(matrix, [name], args.repeats, tier0=True)
        except PlanError:
            continue
        runnable.append(name)
    if not runnable:
        raise PlanError("no suite has a shaping-free subset; nothing to run at "
                        "Tier 0. Run on a Linux host with root for the full matrix.")
    skipped = [n for n in names if n not in runnable]
    if skipped:
        print(f"# Tier 0: {', '.join(skipped)} have no shaping-free subset and "
              f"are omitted. Name one explicitly to see why.\n")
    return runnable


def execute(matrix: dict, args, *, dry: bool) -> int:
    suites = resolve_suites(matrix, args)
    plan, skipped = expand(matrix, suites, args.repeats, tier0=args.tier0)
    plan = schedule(plan)

    for run in plan:
        run["run_id"] = (f"{run['suite']}__{run['cell_id']}"
                         f"__r{run['repeat_index']}__{int(time.time())}")

    shaping_possible = shaping_supported()
    if not args.tier0 and not shaping_possible and not dry:
        needed = {n for r in plan for n in r["conditions"]
                  if needs_shaping(matrix, n)
                  and r["conditions"][n] != axis_default(matrix, n)}
        if needed:
            raise PlanError(
                f"this plan needs traffic shaping ({', '.join(sorted(needed))}) but "
                f"{platform.system()} cannot run tc/netem. Use --tier0 for the "
                f"shaping-free subset, or run on a Linux host with root. "
                f"Refusing to run unshaped cells under shaped labels.")

    if skipped:
        total = sum(len(v) for v in skipped.values())
        print(f"# Tier 0: excluded {total} shaping-dependent cells "
              f"across {len(skipped)} suites.\n")

    mode = "DRY RUN — no network changes, no harness executed" if dry else "RUN"
    print(f"# {mode}: {len(plan)} runs in {batch_count(plan)} process batches, "
          f"~{fmt_duration(estimate_wall_time_s(plan))}")
    if dry and not shaping_possible:
        shaped = sum(1 for r in plan
                     if netem_command(matrix, r["conditions"], args.iface)
                     or tbf_command(matrix, r["conditions"], args.iface))
        if shaped:
            print(f"# NOTE: {shaped} of these runs emit tc commands that "
                  f"{platform.system()} cannot execute. The commands below are a "
                  f"preview of what Tier 1 would apply; --run would refuse here. "
                  f"Use --tier0 for the subset this host can actually measure.")
    print()

    if not dry:
        RUNS_DIR.mkdir(parents=True, exist_ok=True)
        SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
        LOGS_DIR.mkdir(parents=True, exist_ok=True)

    current_batch = None
    exit_code = 0
    try:
        for i, run in enumerate(plan, 1):
            if run["process_batch"] != current_batch:
                current_batch = run["process_batch"]
                mode_name = run["conditions"]["buffering_mode"]
                print(f"# ---- process batch {current_batch}: "
                      f"buffering_mode={mode_name} "
                      f"(fresh process; enable_zero_playout_delay is "
                      f"process-global and irreversible) ----")

            print(f"[{i}/{len(plan)}] {run['run_id']}")
            cond = run["conditions"]
            started = datetime.now(timezone.utc).isoformat()

            execute_shaping = (not dry) and shaping_possible
            netem, tbf, tc_show = apply_shaping(
                matrix, cond, args.iface, execute_shaping)
            cmd = harness_command(matrix, run, args)

            if dry:
                print(f"  shaping: {netem or '(none — no netem parameters at this cell)'}")
                if tbf:
                    print(f"  tbf:     {tbf}")
                # Credentials are stripped from the printed invocation: --dry-run output
                # is routinely pasted into tickets and commit messages. The operator
                # already has the URL they passed in, so nothing is lost.
                print("  harness: " + " ".join(
                    shlex.quote(redact_camera_source(c)) for c in cmd))
                continue

            invalid: list[str] = []
            max_retries = connect_retries(matrix)
            backoff_s = connect_retry_backoff_s(matrix)
            attempts: list[dict] = []

            for attempt in range(max_retries + 1):
                # Popen rather than run(): the run record stores the harness PID,
                # which is what ties a record to the snapshot lines that process
                # wrote.
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, text=True)
                harness_pid = proc.pid
                try:
                    _, stderr = proc.communicate(timeout=run["duration_s"] + 180)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    _, stderr = proc.communicate()
                    stderr = (stderr or "") + "\nharness timed out and was killed"

                log_path = write_harness_log(run["run_id"], attempt, cmd, proc.returncode,
                                             stderr)
                attempts.append({"attempt": attempt, "exit_code": proc.returncode,
                                 "pid": harness_pid, "stderr_log": str(log_path)})

                retryable = proc.returncode == HARNESS_EXIT_RETRYABLE
                if proc.returncode == 0 or not retryable or attempt == max_retries:
                    break
                delay = backoff_s * (2 ** attempt)
                print(f"     connect failed (exit {proc.returncode}); retry "
                      f"{attempt + 1}/{max_retries} in {delay:.1f}s — see {log_path}")
                time.sleep(delay)

            if proc.returncode != 0:
                # The full stderr is in the log; the record carries an excerpt plus
                # the path, so a failure stays diagnosable after the sweep.
                invalid.append(f"harness exit {proc.returncode}: "
                               f"{(stderr or '').strip()[:300]}"
                               f" [full stderr: {attempts[-1]['stderr_log']}]")
                exit_code = 1

            record = build_run_record(
                matrix, run, args, netem=netem, tbf=tbf, tc_show=tc_show,
                shaping_applied=execute_shaping and bool(netem or tbf),
                started_utc=started, subscriber_pid=harness_pid,
                harness_cmd=cmd, invalid=invalid, attempts=attempts)

            out = RUNS_DIR / f"{run['suite']}.jsonl"
            with open(out, "a") as f:
                f.write(json.dumps(record) + "\n")
            retried = len(attempts) - 1
            suffix = f" (after {retried} retry/retries)" if retried else ""
            print(f"     -> {'invalid' if invalid else 'recorded'}{suffix}")
    finally:
        clear_shaping(matrix, args.iface, (not dry) and shaping_possible)

    return exit_code


# ---------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--plan", action="store_true",
                      help="expand the matrix and print counts; execute nothing")
    mode.add_argument("--dry-run", action="store_true",
                      help="print the exact shaping and harness commands; change nothing")
    mode.add_argument("--run", action="store_true", help="execute the plan")

    ap.add_argument("--suite", action="append",
                    help="suite name; repeatable. Default: all.")
    ap.add_argument("--tier0", action="store_true",
                    help="shaping-free subset only: excludes every cell whose axes "
                         "need tc/netem. This is the mode that runs on macOS.")
    ap.add_argument("--repeats", type=int, default=None,
                    help="repeats per cell; default from matrix.yaml")
    ap.add_argument("--matrix", type=Path, default=MATRIX_PATH)

    ap.add_argument("--harness", default="./target/release/teleop-harness")
    ap.add_argument("--url", default=os.environ.get("LIVEKIT_URL",
                                                    "ws://127.0.0.1:7880"))
    ap.add_argument("--iface", default=None,
                    help="shaping interface; default from matrix.yaml")
    ap.add_argument("--tier", type=int, default=None, choices=[0, 1, 2],
                    help="execution tier recorded in the run record; "
                         "inferred from --tier0 when omitted")
    ap.add_argument("--path", default="cloud",
                    choices=["loopback", "lan", "edge_mso", "cellular", "cloud"])
    ap.add_argument("--clock-source", default="ntp",
                    choices=["none", "ntp", "chrony", "ptp"])
    ap.add_argument("--build-config", default="release",
                    choices=["debug", "release"])
    ap.add_argument("--camera-source", default="test_pattern",
                    help="video source for every run in this sweep: 'test_pattern' "
                         "(default), a local capture device given as an enumeration "
                         "index or a substring of its name, or an rtsp:// / rtsps:// "
                         "URL for an IP camera. NOT an axis and never a "
                         "cell default -- a camera makes bitrate depend on scene "
                         "content, so camera_source is in never_pool_across and "
                         "camera runs are never aggregated with pattern runs. A "
                         "camera that cannot be opened fails the run rather than "
                         "falling back.")
    ap.add_argument("--rtsp-transport", default="tcp", choices=["tcp", "udp"],
                    help="RTSP media transport, used only when --camera-source is an "
                         "rtsp:// URL. TCP by default: UDP RTSP degrades by dropping "
                         "media silently on a filtered path, which reaches the record "
                         "as a broken camera rather than as a network problem.")
    ap.add_argument("--load-generator-host", action="append",
                    help="T-4 only; repeatable. Must differ from this host.")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args()

    matrix = load_matrix(args.matrix)
    if args.repeats is None:
        args.repeats = matrix["meta"]["parameters"]["default_repeats"]["value"]
    if args.iface is None:
        args.iface = matrix["shaping"]["iface_default"]
    if args.tier is None:
        args.tier = 0 if args.tier0 else 1

    try:
        if args.plan:
            return print_plan(matrix, args)
        return execute(matrix, args, dry=args.dry_run)
    except PlanError as e:
        print(f"\nerror: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
