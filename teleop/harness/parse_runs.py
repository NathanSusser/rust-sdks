#!/usr/bin/env python3
"""Extraction, scoring, breakpoint detection and reporting for the teleop matrix.

Reads run records (JSONL, one per suite, schema: run_schema.json) plus the per-poll
snapshot files each record points at, differences the cumulative WebRTC counters,
scores the result against matrix.yaml, and writes a markdown report.

Four verdicts, not two. PASS / FAIL / INVALID / OBSERVE. A run that did not measure
the thing is INVALID, is excluded from every breakpoint, and is never counted as a
failure. Conflating INVALID with FAIL is how a matrix produces confident wrong
answers, so the separation is enforced structurally: invalidation is evaluated
before any blocking threshold, and a run that invalidates never reaches the
threshold loop.

Everything numeric comes from matrix.yaml. A threshold, an axis value, or a rate
duplicated here is a defect.

Usage:
    parse_runs.py --runs runs/                       # score every suite, print report
    parse_runs.py --runs runs/ --report report.md
    parse_runs.py --runs fixtures/ --json out.json   # machine-readable scores
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Sequence

try:
    import yaml
except ImportError:  # pragma: no cover - environment problem, not a code path
    sys.exit("pyyaml required:  pip install pyyaml")

HERE = Path(__file__).parent
MATRIX_PATH = HERE / "matrix.yaml"

PASS, FAIL, INVALID, OBSERVE = "PASS", "FAIL", "INVALID", "OBSERVE"


class AnalysisError(RuntimeError):
    """The data could not be analyzed as specified. Never downgraded to a warning:
    a scorer that silently guesses produces exactly the confident wrong answer the
    four-verdict model exists to prevent."""


# ---------------------------------------------------------------------------
# Matrix access
# ---------------------------------------------------------------------------


def load_matrix(path: Path = MATRIX_PATH) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def parameter(matrix: dict, name: str) -> Any:
    return matrix["meta"]["parameters"][name]["value"]


def invalid_vocabulary(matrix: dict) -> set[str]:
    return set(matrix["invalid_reasons"])


def never_pool_keys(matrix: dict) -> list[str]:
    return list(matrix["never_pool_across"])


def min_samples(matrix: dict, metric: str) -> int | None:
    """The ">=N samples" floor a metric's `requires` clause states, if any.

    Read out of matrix.yaml rather than hardcoded: the number is a threshold and
    thresholds live in one file. A metric measured below its floor cannot resolve
    the bar it is scored against, and reporting it anyway is a confident answer
    the sample count does not support.
    """
    spec = matrix["metrics"].get(metric)
    if not isinstance(spec, dict):
        return None
    match = re.search(r">=\s*([\d_ ]+)\s*samples", str(spec.get("requires", "")))
    if not match:
        return None
    return int(match.group(1).replace("_", "").replace(" ", ""))


# ---------------------------------------------------------------------------
# Statistics
#
# Nearest-rank percentiles throughout: with the sample counts these runs produce,
# interpolation invents values that were never measured.
# ---------------------------------------------------------------------------


def percentile(values: Sequence[float], q: float) -> float | None:
    """Nearest-rank percentile. `q` in 0..100."""
    xs = sorted(v for v in values if v is not None)
    if not xs:
        return None
    rank = math.ceil(q / 100.0 * len(xs))
    return xs[max(1, min(rank, len(xs))) - 1]


def median(values: Sequence[float]) -> float | None:
    return percentile(values, 50)


def spread(values: Sequence[float]) -> tuple[float | None, float | None]:
    """Min and max across repeats. A cell whose repeats disagree must show it."""
    xs = [v for v in values if v is not None]
    if not xs:
        return (None, None)
    return (min(xs), max(xs))


# ---------------------------------------------------------------------------
# Snapshot reading
# ---------------------------------------------------------------------------


class Snapshots:
    """One run's per-poll snapshot file, plus its terminal run_metadata record.

    The metadata record is written LAST by the harness, so its absence means the
    run did not complete. That is a validity fact, not a parsing inconvenience:
    without it the scored window is unreconstructable and the delivered-share
    denominator has no boundary.
    """

    def __init__(self, polls: list[dict], metadata: dict | None, path: Path | None):
        self.polls = polls
        self.metadata = metadata
        self.path = path

    @property
    def complete(self) -> bool:
        return self.metadata is not None

    @classmethod
    def load(cls, path: Path) -> "Snapshots":
        polls: list[dict] = []
        metadata: dict | None = None
        with open(path) as f:
            for lineno, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError as e:
                    raise AnalysisError(f"{path}:{lineno}: malformed JSON: {e}") from e
                if rec.get("record") == "run_metadata":
                    metadata = rec
                else:
                    polls.append(rec)
        return cls(polls, metadata, path)

    def scored(self) -> list[dict]:
        """Polls inside the post-warmup scored window.

        Prefers the harness's own `scored` flag; falls back to the metadata window
        bounds. Never infers the window from poll timestamps alone — that shifts it
        by however long connection setup took.
        """
        flagged = [p for p in self.polls if p.get("scored")]
        if flagged:
            return flagged
        if self.metadata:
            lo = self.metadata["scored_window_start_unix_us"]
            hi = self.metadata["scored_window_end_unix_us"]
            return [p for p in self.polls if lo <= p.get("t_unix_us", -1) <= hi]
        return []


def load_publisher_seq_log(path: Path) -> list[dict]:
    entries = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                entries.append(json.loads(line))
    return entries


# ---------------------------------------------------------------------------
# Differencing
#
# Cumulative counters are differenced across CONSECUTIVE polls before any average
# or rate. Dividing two lifetime cumulatives yields a session average that hides
# exactly the transient a suite is looking for.
# ---------------------------------------------------------------------------


def _get(poll: dict, section: str, field: str) -> Any:
    sec = poll.get(section)
    if sec is None:
        return None
    return sec.get(field)


def deltas(polls: Sequence[dict], section: str, field: str,
           *, clamp_negative: bool = True) -> list[float]:
    """Per-interval deltas of one cumulative counter across consecutive polls.

    Intervals where either endpoint lacks the section are skipped rather than
    treated as zero — an absent section means "not subscribed yet", not "measured
    nothing", and the two must not be conflated.
    """
    out: list[float] = []
    for prev, cur in zip(polls, polls[1:]):
        a, b = _get(prev, section, field), _get(cur, section, field)
        if a is None or b is None:
            continue
        d = b - a
        if d < 0 and clamp_negative:
            d = 0
        out.append(d)
    return out


def interval_dts(polls: Sequence[dict], section: str | None = None) -> list[float]:
    """Seconds between consecutive polls, on the monotonic clock.

    When `section` is given, only intervals where BOTH endpoints carry that section
    are returned, so a rate's numerator and denominator span the same intervals.
    """
    out: list[float] = []
    for prev, cur in zip(polls, polls[1:]):
        if section is not None and (prev.get(section) is None or cur.get(section) is None):
            continue
        dt = (cur["t_monotonic_us"] - prev["t_monotonic_us"]) / 1e6
        out.append(dt if dt > 0 else 0.0)
    return out


def delta_ratio(polls: Sequence[dict], section: str, numerator: str,
                denominator: str, *, scale: float = 1.0) -> float | None:
    """Sum of per-interval numerator deltas over sum of per-interval denominator
    deltas, restricted to intervals where the denominator actually advanced.

    Intervals with a zero denominator delta are dropped rather than contributing a
    zero to an average: a poll where no frame was emitted carries no information
    about per-frame delay, and including it would drag the figure toward zero in
    exactly the stalled conditions the metric is meant to expose.
    """
    num_total = 0.0
    den_total = 0.0
    for prev, cur in zip(polls, polls[1:]):
        a_n, b_n = _get(prev, section, numerator), _get(cur, section, numerator)
        a_d, b_d = _get(prev, section, denominator), _get(cur, section, denominator)
        if None in (a_n, b_n, a_d, b_d):
            continue
        dd = b_d - a_d
        if dd <= 0:
            continue
        dn = b_n - a_n
        num_total += dn
        den_total += dd
    if den_total <= 0:
        return None
    return (num_total / den_total) * scale


def delta_rate(polls: Sequence[dict], section: str, field: str,
               *, per: float = 1.0) -> float | None:
    """Total delta over total elapsed seconds, times `per` (60 for per-minute)."""
    ds = deltas(polls, section, field)
    dts = interval_dts(polls, section)
    if not ds or sum(dts) <= 0:
        return None
    return (sum(ds) / sum(dts)) * per


def lost_pct_with_clamp(polls: Sequence[dict], section: str) -> dict:
    """Loss share for one stream, differenced HERE from the cumulative counter.

    The harness also writes a per-poll `packets_lost_delta`, but this function
    deliberately does not consume it. Design gap 13 makes differencing an
    analysis-side operation precisely so a bug in it is fixable without re-running
    the matrix; reading a harness-precomputed delta would put the arithmetic back
    on the far side of that boundary, where a defect costs a full re-run. The
    harness field is instead cross-checked below, so a disagreement is visible
    rather than silently preferred either way.

    `packets_lost` is i64 and may go negative across a poll on reorder or
    duplicate. A negative delta is an artifact, not a gain: it is clamped at zero
    for the ratio and the pre-clamp value is reported, because a run with a
    reordering path has a loss figure that is a lower bound.
    """
    lost_total = 0
    clamp_events: list[int] = []
    for prev, cur in zip(polls, polls[1:]):
        a, b = _get(prev, section, "packets_lost"), _get(cur, section, "packets_lost")
        if a is None or b is None:
            continue
        d = b - a
        if d < 0:
            clamp_events.append(d)
            d = 0
        lost_total += d

    recv_total = sum(deltas(polls, section, "packets_received"))
    denom = lost_total + recv_total

    # The harness's own clamp annotation, kept as a cross-check on the above.
    harness_clamps = [c for c in (_get(p, section, "packets_lost_clamped_from")
                                  for p in polls) if c is not None]

    return {
        "pct": (lost_total / denom * 100.0) if denom > 0 else None,
        "lost": lost_total,
        "received": recv_total,
        "clamp_events": len(clamp_events) or len(harness_clamps),
        "clamped_min": min(clamp_events + harness_clamps)
                       if (clamp_events or harness_clamps) else None,
    }


def raw_series(polls: Sequence[dict], section: str, field: str) -> list[Any]:
    out = []
    for p in polls:
        v = _get(p, section, field)
        if v is not None:
            out.append(v)
    return out


# ---------------------------------------------------------------------------
# Extraction — snapshots to the flat `metrics` map of run_schema.json
#
# Every metric here corresponds to a row in matrix.yaml `metrics`. Extraction is
# Python so a derived metric can change without a rebuild.
# ---------------------------------------------------------------------------


def extract_video_recv(polls: Sequence[dict], m: dict, dist: dict) -> None:
    sec = "video_in"
    fps_gauge = raw_series(polls, sec, "frames_per_second")
    m["video_fps_p50"] = median(fps_gauge)
    m["video_fps_delta"] = delta_rate(polls, sec, "frames_decoded")

    intervals: list[float] = []
    for p in polls:
        vals = _get(p, sec, "frame_arrival_intervals_ms")
        if vals:
            intervals.extend(vals)
    m["video_frame_interval_p99_ms"] = percentile(intervals, 99)

    freeze = deltas(polls, sec, "freeze_count")
    m["video_freeze_count"] = sum(freeze) if freeze else None
    fdur = deltas(polls, sec, "total_freeze_duration_s")
    m["video_freeze_duration_ms"] = sum(fdur) * 1000.0 if fdur else None
    pause = deltas(polls, sec, "pause_count")
    m["video_pause_count"] = sum(pause) if pause else None

    m["jitter_buffer_delay_avg_ms"] = delta_ratio(
        polls, sec, "jitter_buffer_delay_s", "jitter_buffer_emitted_count", scale=1000.0)
    m["jitter_buffer_target_delay_ms"] = delta_ratio(
        polls, sec, "jitter_buffer_target_delay_s", "jitter_buffer_emitted_count",
        scale=1000.0)

    loss = lost_pct_with_clamp(polls, sec)
    m["video_packets_lost_pct"] = loss["pct"]
    m["video_packets_lost_clamp_events"] = loss["clamp_events"] if polls else None
    m["video_packets_lost_clamped_min"] = loss["clamped_min"]

    recv = deltas(polls, sec, "packets_received")
    rtx = deltas(polls, sec, "retransmitted_packets_received")
    m["video_rtx_pct"] = (sum(rtx) / sum(recv) * 100.0) if sum(recv) > 0 else None
    m["video_nack_rate_per_min"] = delta_rate(polls, sec, "nack_count", per=60.0)
    m["pli_rate_per_min"] = delta_rate(polls, sec, "pli_count", per=60.0)
    m["key_frames_decoded_rate"] = delta_rate(polls, sec, "key_frames_decoded", per=60.0)

    m["decode_time_avg_ms"] = delta_ratio(
        polls, sec, "total_decode_time_s", "frames_decoded", scale=1000.0)
    m["assembly_time_avg_ms"] = delta_ratio(
        polls, sec, "total_assembly_time_s", "frames_assembled_from_multiple_packets",
        scale=1000.0)
    m["processing_delay_avg_ms"] = delta_ratio(
        polls, sec, "total_processing_delay_s", "jitter_buffer_emitted_count",
        scale=1000.0)

    impls = [s for s in raw_series(polls, sec, "decoder_implementation") if s]
    m["decoder_implementation"] = impls[-1] if impls else None
    widths = [w for w in raw_series(polls, sec, "frame_width") if w]
    heights = [h for h in raw_series(polls, sec, "frame_height") if h]
    m["frame_width"] = median(widths)
    m["frame_height"] = median(heights)


def extract_video_send(polls: Sequence[dict], m: dict, dist: dict,
                       video_poll_hz: float | None) -> None:
    sec = "video_out"
    byte_deltas = []
    for prev, cur in zip(polls, polls[1:]):
        a, b = prev.get(sec), cur.get(sec)
        if a is None or b is None:
            continue
        d = ((b["bytes_sent"] + b["header_bytes_sent"])
             - (a["bytes_sent"] + a["header_bytes_sent"]))
        byte_deltas.append(max(d, 0))
    dts = interval_dts(polls, sec)
    total_dt = sum(dts)
    m["video_bitrate_bps"] = ((sum(byte_deltas) * 8) / total_dt) if total_dt > 0 else None

    targets = raw_series(polls, sec, "target_bitrate_bps")
    m["video_target_bitrate_bps"] = median(targets)
    m["encode_time_avg_ms"] = delta_ratio(
        polls, sec, "total_encode_time_s", "frames_encoded", scale=1000.0)

    reasons = [r for r in raw_series(polls, sec, "quality_limitation_reason") if r]
    m["quality_limitation_reason"] = _dominant_reason(reasons)

    # quality_limitation_durations keys are optional; a missing key is zero, not an
    # error. The harness flattens them into named seconds fields.
    for key, metric in (("cpu", "quality_limitation_cpu_pct"),
                        ("bandwidth", "quality_limitation_bandwidth_pct")):
        field = f"quality_limitation_{key}_s"
        ds = deltas(polls, sec, field)
        m[metric] = ((sum(ds) / total_dt) * 100.0) if (ds and total_dt > 0) else None

    # Quality at bitrate. Without these, "AV1 is more efficient" and "AV1 quietly
    # dropped quality to fit" are indistinguishable — the Tier 0 sweep showed AV1
    # at quality_limitation_reason `none` for all 106 polls while H264 sat at
    # `bandwidth` for 62 of 106 at a similar target bitrate, which is suggestive
    # but proves nothing on its own because nothing recorded quality.
    #
    # qp_sum is CUMULATIVE, so it is differenced against frames_encoded over the
    # same interval. QP scales differ between codecs and the value is NOT
    # comparable across them (design §8); it is reported per codec and never
    # pooled.
    m["qp_avg"] = delta_ratio(polls, sec, "qp_sum", "frames_encoded")

    # Share of scored POLLS whose limiter was bandwidth, as distinct from the
    # duration share above. This is the form that surfaced the AV1/H264 result,
    # and unlike qp_avg it IS cross-codec comparable: it counts how often the
    # encoder said it was bandwidth-bound, in units no codec defines differently.
    if reasons:
        bw_polls = sum(1 for r in reasons if r == "bandwidth")
        m["quality_limitation_bandwidth_poll_pct"] = bw_polls / len(reasons) * 100.0
        m["quality_limitation_poll_count"] = len(reasons)
    else:
        m["quality_limitation_bandwidth_poll_pct"] = None
        m["quality_limitation_poll_count"] = 0

    res_changes = deltas(polls, sec, "quality_limitation_resolution_changes")
    m["quality_limitation_resolution_changes"] = sum(res_changes) if res_changes else None
    m["fps_ceiling"] = median(raw_series(polls, sec, "frames_per_second"))

    impls = [s for s in raw_series(polls, sec, "encoder_implementation") if s]
    m["encoder_implementation"] = impls[-1] if impls else None
    pe = raw_series(polls, sec, "power_efficient_encoder")
    m["power_efficient_encoder"] = pe[-1] if pe else None

    flags = raw_series(polls, sec, "malformed_bitstream")
    m["malformed_bitstream"] = any(flags) if flags else None

    mimes = [s for s in raw_series(polls, sec, "codec_mime_type") if s]
    if not mimes:
        mimes = [s for s in raw_series(polls, "video_in", "codec_mime_type") if s]
    m["actual_codec"] = _parse_codec(mimes[-1]) if mimes else None

    kf = keyframe_service_polls(polls)
    if kf:
        dist["keyframe_service_polls"] = {
            "unit": "poll_intervals",
            "n": len(kf),
            "values": kf,
            "max": max(kf),
            "note": ("Quantized to the video poll period"
                     + (f" ({video_poll_hz} Hz)" if video_poll_hz else "")
                     + ". Never a millisecond percentile."),
        }


def _dominant_reason(reasons: Sequence[str]) -> str | None:
    """The reason held for the most polls. `none` loses to any real limiter, since
    a limiter present on a minority of polls is the finding, not the majority idle
    state."""
    if not reasons:
        return None
    counts: dict[str, int] = defaultdict(int)
    for r in reasons:
        counts[r] += 1
    real = {k: v for k, v in counts.items() if k.lower() != "none"}
    pool = real or counts
    return max(pool.items(), key=lambda kv: kv[1])[0]


def _parse_codec(mime: str) -> str:
    """'video/AV1' -> 'av1'. Normalizes the H264 profile suffixes libwebrtc emits."""
    tail = mime.split("/")[-1].strip().lower()
    return {"h265": "h265", "hevc": "h265"}.get(tail, tail)


def keyframe_service_polls(polls: Sequence[dict]) -> list[int]:
    """Poll counts between a PLI and the next keyframe encoded.

    Reported in poll intervals and never converted to a millisecond percentile: the
    measurement resolution equals the poll period, so a p95 over a handful of
    quantized values is just the maximum wearing a percentile's name.
    """
    out: list[int] = []
    pending: int | None = None
    for i, (prev, cur) in enumerate(zip(polls, polls[1:]), start=1):
        a, b = prev.get("video_out"), cur.get("video_out")
        if a is None or b is None:
            continue
        if pending is not None and b["key_frames_encoded"] - a["key_frames_encoded"] > 0:
            out.append(i - pending)
            pending = None
        if pending is None and b["pli_count"] - a["pli_count"] > 0:
            pending = i
    return out


def extract_audio(polls: Sequence[dict], m: dict) -> None:
    inb, out, play = "audio_in", "audio_out", "audio_playout"

    m["audio_playout_delay_avg_ms"] = delta_ratio(
        polls, play, "total_playout_delay_s", "total_samples_count", scale=1000.0)
    m["audio_jitter_buffer_delay_ms"] = delta_ratio(
        polls, inb, "jitter_buffer_delay_s", "jitter_buffer_emitted_count", scale=1000.0)
    m["audio_concealment_pct"] = delta_ratio(
        polls, inb, "concealed_samples", "total_samples_received", scale=100.0)
    m["audio_silent_concealment_pct"] = delta_ratio(
        polls, inb, "silent_concealed_samples", "total_samples_received", scale=100.0)
    ce = deltas(polls, inb, "concealment_events")
    m["audio_concealment_events"] = sum(ce) if ce else None

    ins = deltas(polls, inb, "inserted_samples_for_deceleration")
    rem = deltas(polls, inb, "removed_samples_for_acceleration")
    tot = deltas(polls, inb, "total_samples_received")
    m["audio_accel_decel_pct"] = ((sum(ins) + sum(rem)) / sum(tot) * 100.0
                                  if sum(tot) > 0 else None)

    byte_deltas = []
    for prev, cur in zip(polls, polls[1:]):
        a, b = prev.get(out), cur.get(out)
        if a is None or b is None:
            continue
        byte_deltas.append(max((b["bytes_sent"] + b["header_bytes_sent"])
                               - (a["bytes_sent"] + a["header_bytes_sent"]), 0))
    dt = sum(interval_dts(polls, out))
    m["audio_bitrate_bps"] = ((sum(byte_deltas) * 8) / dt) if dt > 0 else None

    m["audio_synthesized_pct"] = delta_ratio(
        polls, play, "synthesized_samples_duration_s", "total_samples_duration_s",
        scale=100.0)

    # Same derivation as video, through the same helper: matrix.yaml specifies
    # "as video, on the audio stream", including the negative-delta clamp, so the
    # two must not be able to drift apart.
    loss = lost_pct_with_clamp(polls, inb)
    m["audio_packets_lost_pct"] = loss["pct"]
    m["audio_packets_lost_clamp_events"] = loss["clamp_events"] if polls else None
    m["audio_packets_lost_clamped_min"] = loss["clamped_min"]

    levels = raw_series(polls, inb, "audio_level")
    # The MAXIMUM, because the validity rule (design §1g rule (ii), §7) is
    # "audio_level == 0 for the WHOLE RUN" — a source that was ever audible was
    # not silent, and only a genuinely silent source makes the concealment
    # figures meaningless.
    #
    # This was a median and it produced a false `silent_audio_source` on
    # T1_video_floor .. av1, uplink_mbps=10, r2: peak level 0.5026 with 42 of 78
    # scored polls at 0.0, so the median read 0.0 while the tone generator was
    # working correctly. The zeros were a reconnect storm (5 reconnects), where a
    # freshly re-subscribed audio track reports zero until samples flow. A median
    # answers "was audio usually flowing", which is a different question and not
    # the one the rule asks.
    m["audio_level"] = max(levels) if levels else None
    # Kept as its own observation: a run audible for only part of its span is
    # worth seeing, it just is not a silent source.
    m["audio_level_median"] = median(levels)


def extract_control(polls: Sequence[dict], m: dict, record: dict,
                    seq_log: list[dict] | None, meta: dict | None,
                    matrix: dict | None = None) -> None:
    scored = polls
    if not scored:
        return
    last = scored[-1]["control"]
    first = scored[0]["control"]

    # The denominator comes from the PUBLISHER, intersected with the scored window.
    # Deriving it from received sequence numbers (max_seq - min_seq + 1) is
    # self-referential and biased toward passing: loss at a window edge shrinks the
    # denominator by exactly the number lost and becomes invisible.
    expected = None
    if seq_log and meta:
        lo = meta["scored_window_start_unix_us"]
        hi = meta["scored_window_end_unix_us"]
        expected = sum(1 for e in seq_log if lo <= e["t_send_unix_us"] <= hi)

    received = last["distinct_seq_received"] - first["distinct_seq_received"]
    floor = min_samples(matrix, "control_delivered_pct") if matrix else None
    if expected and expected > 0:
        m["control_expected_seq_count"] = expected
        if floor is not None and expected < floor:
            # Below the floor the metric cannot resolve the bar it is scored
            # against. At a 99.9% threshold, 0.1% of 4000 samples is four: a
            # single dropped sample moves the figure by 0.025% and the run either
            # clears the bar or misses it on rounding. Reporting a number here
            # would be a confident answer the sample count does not support, so
            # the metric is null and the blocking row is not evaluated.
            m["control_delivered_pct"] = None
            m["control_delivered_undersampled"] = True
            m["control_delivered_sample_floor"] = floor
        else:
            m["control_delivered_pct"] = min(received / expected * 100.0, 100.0)
            m["control_delivered_undersampled"] = False
    else:
        # Without a publisher seq log the metric is null, never estimated.
        m["control_delivered_pct"] = None
        m["control_expected_seq_count"] = None
    m["control_distinct_seq_received"] = received

    rate_hz = record["conditions"].get("control_rate_hz")
    if meta and rate_hz:
        window_s = (meta["scored_window_end_unix_us"]
                    - meta["scored_window_start_unix_us"]) / 1e6
        published = meta.get("seq_published")
        # seq_published is a whole-run count; the scored-window share is what the
        # rate is measured against, so use the seq log when it is available.
        if seq_log:
            lo, hi = meta["scored_window_start_unix_us"], meta["scored_window_end_unix_us"]
            published = sum(1 for e in seq_log if lo <= e["t_send_unix_us"] <= hi)
        if published is not None and window_s > 0:
            nominal = rate_hz * window_s
            m["control_publish_shortfall_pct"] = max(
                (1 - published / nominal) * 100.0, 0.0)

    late = last.get("late_count")
    eligible = last.get("late_eligible_count")
    first_late, first_elig = first.get("late_count"), first.get("late_eligible_count")
    if late is not None and eligible is not None and first_late is not None:
        d_late = late - first_late
        d_elig = eligible - first_elig
        m["control_late_pct"] = (d_late / d_elig * 100.0) if d_elig > 0 else None
    else:
        # Null, not zero: no playout window configured or no valid clock offset
        # means the metric was not measured, and zero is a measurement.
        m["control_late_pct"] = None

    theta_ms = _theta(polls)
    owds: list[float] = []
    for p in scored:
        for us in p["control"].get("owd_raw_us_interval", []):
            owds.append(us / 1000.0)
    if owds and theta_ms is not None:
        corrected = [o - theta_ms for o in owds]
        m["control_owd_p50_ms"] = percentile(corrected, 50)
        m["control_owd_p99_ms"] = percentile(corrected, 99)
    else:
        m["control_owd_p50_ms"] = None
        m["control_owd_p99_ms"] = None

    jit = [p["control"]["jitter_ms"] for p in scored if "jitter_ms" in p["control"]]
    m["control_jitter_ms"] = median(jit)

    intervals = [p["control"]["distinct_seq_received_interval"] for p in scored[1:]]
    dts = interval_dts(scored)
    rates = [n / dt for n, dt in zip(intervals, dts) if dt > 0]
    m["control_effective_rate_hz"] = median(rates)

    # gap_p99 is a true nearest-rank percentile over the whole gap-length
    # distribution, computed by the harness. Take the last reading rather than a
    # percentile of percentiles.
    gaps = [p["control"].get("gap_p99") for p in scored
            if p["control"].get("gap_p99") is not None]
    if gaps:
        m["control_gap_p99"] = gaps[-1]
    else:
        # The harness emits gap_p99 only once a gap has occurred, so its absence
        # means "no gap was observed" — but only if control samples were actually
        # received. With no samples at all, nothing was measured and 0 would read
        # as a perfect run.
        m["control_gap_p99"] = 0 if received > 0 else None
    m["control_max_gap"] = last.get("max_gap")

    # The same two gaps in milliseconds, which is the unit the latency budget is
    # written in. A gap length is a count of consecutive missing sequence numbers,
    # and the publisher emits at a known fixed rate, so the stall it represents is
    # `gap / rate_hz`. Reporting only the count buries a transport stall in units
    # nobody's budget is denominated in.
    #
    # This is OBSERVE, not a bar. It exists because the 2026-08-25 sweep found a
    # 121-sample gap — ~605 ms at 200 Hz — on LiveKit Cloud with NO induced loss
    # and a healthy sampler. A control path that can stall two thirds of a second
    # unprompted on a clean network is directly material to a 90 ms budget, and
    # nothing in the record surfaced it before: delivered-% absorbs it (121 of
    # ~21 000 samples is 0.6%) and the rate metric averages it away.
    rate_hz = (record.get("conditions") or {}).get("control_rate_hz") if record else None
    if rate_hz:
        for src, dst in (("control_max_gap", "control_max_gap_ms"),
                         ("control_gap_p99", "control_gap_p99_ms")):
            gap = m.get(src)
            m[dst] = (gap / float(rate_hz)) * 1000.0 if gap is not None else None
    else:
        m["control_max_gap_ms"] = m["control_gap_p99_ms"] = None

    dc = deltas(polls, "data_channel", "messages_received")
    m["dc_messages_received"] = sum(dc) if dc else None


def _theta(polls: Sequence[dict]) -> float | None:
    """The harness computes theta by pairing each probe's own RTT with the
    contemporaneous OWD-ring minimum. It is consumed here, never recomputed."""
    vals = [p["probe"].get("theta_ms") for p in polls
            if p.get("probe") and p["probe"].get("theta_ms") is not None]
    return vals[-1] if vals else None


def extract_probe(polls: Sequence[dict], m: dict) -> None:
    """Percentiles of the APPLICATION loop round trip, not the network round trip.

    The four-timestamp probe traverses publisher -> SFU -> subscriber -> SFU ->
    publisher over the control transport, and additionally includes control-publisher
    scheduling at the sender (a probe rides the next control sample) and echo
    dispatch at the receiver. It runs roughly 2x the ICE network RTT — measured on
    Q7 av1 r0, app p50 60.6 ms against network p50 31.0 ms.

    The two are separate metrics on purpose. Reporting this figure as "network RTT"
    made Q-7's g2g/RTT ratios read below 1.0, which is impossible: glass-to-glass
    contains a network traversal and cannot be faster than one. See
    `extract_transport` for the network side.
    """
    rtts: list[float] = []
    for p in polls:
        for us in p.get("probe", {}).get("rtt_us_interval", []):
            rtts.append(us / 1000.0)
    if len(rtts) >= 30:
        m["app_rtt_p50_ms"] = percentile(rtts, 50)
        m["app_rtt_p95_ms"] = percentile(rtts, 95)
        m["app_rtt_p99_ms"] = percentile(rtts, 99)
    else:
        # Below the precondition the percentiles are null, not best-effort. A p95
        # over a dozen samples is not a p95.
        m["app_rtt_p50_ms"] = m["app_rtt_p95_ms"] = m["app_rtt_p99_ms"] = None
    m["app_rtt_sample_count"] = len(rtts)

    if polls:
        first, last = polls[0]["probe"], polls[-1]["probe"]
        sent = last["probes_sent"] - first["probes_sent"]
        # The harness's explicit aged-out count where present. `sent - completed`
        # also counts every probe still in flight, and at a probe interval below
        # the round trip that is several probes at any instant — which would read
        # as steady-state loss on a path that lost nothing. Older records carry no
        # `probes_lost`, so the difference remains the fallback for them.
        if "probes_lost" in last and "probes_lost" in first:
            lost = last["probes_lost"] - first["probes_lost"]
            m["probe_loss_legacy_derivation"] = False
        else:
            lost = sent - (last["probes_completed"] - first["probes_completed"])
            # Flagged so the report can mark the figure as a ceiling rather than a
            # measurement. Pre-fix snapshots came from a tracker that retired the
            # outstanding probe whenever the next was issued, so this derivation
            # counts displaced-but-delivered probes as lost.
            m["probe_loss_legacy_derivation"] = True
        m["probe_loss_pct"] = (lost / sent * 100.0) if sent > 0 else None
    m["theta_ms"] = _theta(polls)
    confs = [p["probe"]["clock_sync_confidence"] for p in polls if p.get("probe")]
    m["clock_sync_confidence"] = confs[-1] if confs else "none"


def extract_transport(polls: Sequence[dict], m: dict) -> None:
    """The NETWORK round trip, from the ICE selected candidate pair.

    This is the denominator for Q-7's g2g/RTT ratio and the metric §8.1a's 90 ms
    ceiling is scored against. It carries no application scheduling, which is what
    separates it from `app_rtt_*` — see `extract_probe`.

    `available_outgoing_bitrate` is deliberately NOT extracted as a bandwidth
    estimate. It is read from the subscriber peer connection, which sends only
    RTCP, so its estimator never leaves libwebrtc's 300 000 bps default start
    bitrate. See `TransportSample` in src/snapshot.rs.
    """
    rtt = [v * 1000.0 for v in raw_series(polls, "transport", "candidate_pair_rtt_s")]
    m["network_rtt_p50_ms"] = percentile(rtt, 50)
    m["network_rtt_p95_ms"] = percentile(rtt, 95)
    m["network_rtt_sample_count"] = len(rtt)
    # Same series under its pre-rename name, so older records stay readable.
    m["ice_rtt_ms"] = median(rtt)
    rtcp = [v * 1000.0 for v in raw_series(polls, "transport", "rtcp_rtt_s")]
    m["rtcp_rtt_ms"] = median(rtcp)
    changes = deltas(polls, "transport", "selected_candidate_pair_changes")
    m["ice_selected_pair_changes"] = sum(changes) if changes else None
    states = raw_series(polls, "transport", "ice_state")
    m["ice_state"] = states[-1] if states else None
    dtls = raw_series(polls, "transport", "dtls_state")
    m["dtls_state"] = dtls[-1] if dtls else None


def extract_g2g(polls: Sequence[dict], m: dict, record: dict) -> None:
    lat: list[float] = []
    for p in polls:
        g = p.get("g2g")
        if not g:
            continue
        for us in g.get("latency_us_interval", []):
            lat.append(us / 1000.0)
    m["g2g_p50_ms"] = percentile(lat, 50)
    m["g2g_p99_ms"] = percentile(lat, 99)
    m["g2g_sample_count"] = len(lat)

    g2gs = [p["g2g"] for p in polls if p.get("g2g")]
    if g2gs:
        last = g2gs[-1]
        span = last.get("frame_id_span") or 0
        distinct = last.get("distinct_frame_ids") or 0
        m["g2g_frame_loss_pct"] = ((1 - distinct / span) * 100.0) if span > 0 else None

        # Coverage guards the subscribe_timing_events ordering fault, whose only
        # symptom is empty frame metadata while every other signal looks healthy.
        received = last.get("distinct_frame_ids") or 0
        without = last.get("frames_without_timestamp") or 0
        with_meta = max(received - without, 0)
        m["g2g_metadata_coverage_pct"] = (with_meta / received * 100.0) if received else None
        m["frames_received"] = received
        m["frames_with_metadata"] = with_meta
    else:
        m["g2g_frame_loss_pct"] = None
        m["g2g_metadata_coverage_pct"] = None

    # The run record's own validity block wins when it carries a coverage figure:
    # it is measured against every received frame, not only those the poll
    # snapshots summarized.
    stated = record.get("validity", {}).get("g2g_metadata_coverage_pct")
    if stated is not None:
        m["g2g_metadata_coverage_pct"] = stated
        rec_recv = record["validity"].get("frames_received")
        rec_meta = record["validity"].get("frames_with_metadata")
        if rec_recv is not None:
            m["frames_received"] = rec_recv
        if rec_meta is not None:
            m["frames_with_metadata"] = rec_meta


def extract_session(record: dict, meta: dict | None, m: dict) -> None:
    events = record.get("events") or []
    drops = [e for e in events
             if e["kind"] == "disconnected" and not e.get("harness_initiated", False)]
    m["session_drops"] = len(drops)

    reconnecting = [e for e in events if e["kind"] == "reconnecting"]
    if reconnecting:
        m["reconnect_count"] = len(reconnecting)
    elif meta is not None:
        m["reconnect_count"] = meta.get("reconnect_count", 0)
    else:
        m["reconnect_count"] = None

    recoveries: list[float] = []
    pending: int | None = None
    for e in sorted(events, key=lambda e: e["t_unix_us"]):
        if e["kind"] == "reconnecting":
            pending = e["t_unix_us"]
        elif e["kind"] == "reconnected" and pending is not None:
            recoveries.append((e["t_unix_us"] - pending) / 1000.0)
            pending = None
    m["recovery_p95_ms"] = percentile(recoveries, 95) if len(recoveries) >= 5 else None

    connected = [e for e in events if e["kind"] == "connected"]
    first_video = [e for e in events if e["kind"] == "first_video_frame"]
    origin = meta.get("run_origin_unix_us") if meta else None
    if origin and connected:
        m["join_to_connected_ms"] = (connected[0]["t_unix_us"] - origin) / 1000.0
    if origin and first_video:
        m["join_to_first_video_ms"] = (first_video[0]["t_unix_us"] - origin) / 1000.0


def extract_harness_health(snaps: Snapshots, scored: Sequence[dict], m: dict) -> None:
    if scored:
        last = scored[-1]["sampler"]
        first = scored[0]["sampler"]
        over = last["overbudget_count"] - first["overbudget_count"]
        total = last["polls_total"] - first["polls_total"]
        m["poll_overbudget_pct"] = (over / total * 100.0) if total > 0 else None
        m["poll_interval_p99_ms"] = percentile(
            [p["sampler"]["actual_interval_ms"] for p in scored], 99)
        rpc = last["stats_rpc_failures"] - first["stats_rpc_failures"]
        m["stats_rpc_failures"] = rpc
        m["stats_rpc_failure_pct"] = (rpc / total * 100.0) if total > 0 else None
    else:
        m["poll_overbudget_pct"] = None
        m["poll_interval_p99_ms"] = None
        m["stats_rpc_failures"] = None
        m["stats_rpc_failure_pct"] = None


def extract(record: dict, matrix: dict, *, base_dir: Path) -> dict:
    """Fills `metrics` and `distributions` on a run record from its snapshots.

    Returns the record, mutated. Everything derived lives in the flat `metrics`
    map defined by run_schema.json.
    """
    m: dict = dict(record.get("metrics") or {})
    dist: dict = dict(record.get("distributions") or {})

    snap_path = (record.get("raw") or {}).get("snapshots_jsonl_path")
    # A path that is NAMED but does not resolve is an operator error — a wrong
    # --base-dir, a moved artifact — and must not be absorbed into a fact about
    # the run. Falling through to an empty snapshot set would make the record
    # indistinguishable from a run that ended mid-flight, and the scorer would
    # then report `session_lost_mid_run`: a confident, specific, wrong claim
    # about the network when the actual problem is a misconfigured path.
    snaps = Snapshots([], None, None)
    if snap_path:
        p = Path(snap_path)
        if not p.is_absolute():
            p = base_dir / p
        if not p.exists():
            raise AnalysisError(
                f"{record['run_id']}: snapshots_jsonl_path names "
                f"{snap_path!r}, which resolves to {p} and does not exist. "
                "A missing input file is an operator error, not evidence about "
                "the run — pass --base-dir if the artifacts live elsewhere.")
        snaps = Snapshots.load(p)

    seq_log = None
    seq_path = (record.get("raw") or {}).get("publisher_seq_log_path")
    if seq_path:
        sp = Path(seq_path)
        if not sp.is_absolute():
            sp = base_dir / sp
        if not sp.exists():
            raise AnalysisError(
                f"{record['run_id']}: publisher_seq_log_path names "
                f"{seq_path!r}, which resolves to {sp} and does not exist. "
                "This is the control_delivered_pct denominator; silently "
                "nulling the metric would hide a config error behind an "
                "unmeasured column.")
        seq_log = load_publisher_seq_log(sp)

    scored = snaps.scored()
    meta = snaps.metadata

    m["run_complete"] = snaps.complete
    m["scored_poll_count"] = len(scored)

    if scored:
        video_poll_hz = (record.get("harness") or {}).get("video_poll_hz")
        extract_video_recv(scored, m, dist)
        extract_video_send(scored, m, dist, video_poll_hz)
        extract_audio(scored, m)
        extract_control(scored, m, record, seq_log, meta, matrix)
        extract_probe(scored, m)
        extract_transport(scored, m)
        extract_g2g(scored, m, record)
    extract_session(record, meta, m)
    extract_harness_health(snaps, scored, m)

    # Requested vs actual. The analysis uses actual; the request is kept for the
    # diff. A cell labelled av1 that fell back is the worst data point the matrix
    # can produce, so the mismatch is materialized rather than left to the reader.
    cond = record["conditions"]
    actual = m.get("actual_codec")
    if actual is None and meta is not None:
        actual = meta.get("negotiated_codec")
    cond["video_codec_actual"] = actual
    cond["codec_mismatch"] = bool(actual is not None
                                  and actual != cond["video_codec_requested"])
    m["codec_mismatch"] = cond["codec_mismatch"]

    if m.get("frame_width"):
        cond["video_width_actual"] = int(m["frame_width"])
    if m.get("frame_height"):
        cond["video_height_actual"] = int(m["frame_height"])

    env = record["environment"]
    if meta is not None:
        if meta.get("encoder_tier"):
            env["encoder_tier"] = meta["encoder_tier"]
        if meta.get("encoder_implementation"):
            env["encoder_implementation"] = meta["encoder_implementation"]
        # The runner writes what was *requested*; the harness reports what it
        # actually opened. camera_source is a never_pool_across dimension, so the
        # resolved value is the one that must key the pool -- a run pooled by its
        # request would sit in a cell it did not run.
        if meta.get("camera_source"):
            env["camera_source"] = meta["camera_source"]
        if meta.get("camera_device") is not None:
            env["camera_device"] = meta["camera_device"]
        h = record.setdefault("harness", {})
        h["scored_window_start_unix_us"] = meta.get("scored_window_start_unix_us")
        h["scored_window_end_unix_us"] = meta.get("scored_window_end_unix_us")
        h["publisher_process_id"] = meta.get("publisher_process_id")
        h["subscriber_process_id"] = meta.get("subscriber_process_id",
                                              h.get("subscriber_process_id"))
        h["harness_version"] = meta.get("harness_version")
        if meta.get("playout_units_confirmed") is not None:
            h["playout_units_confirmed"] = meta["playout_units_confirmed"]
    if m.get("encoder_implementation") and not env.get("encoder_implementation"):
        env["encoder_implementation"] = m["encoder_implementation"]
    if m.get("decoder_implementation"):
        env["decoder_implementation"] = m["decoder_implementation"]
    if m.get("power_efficient_encoder") is not None:
        env["power_efficient_encoder"] = m["power_efficient_encoder"]

    v = record.setdefault("validity", {})
    if m.get("clock_sync_confidence"):
        v["clock_sync_confidence"] = m["clock_sync_confidence"]
    if m.get("theta_ms") is not None:
        v["theta_ms"] = m["theta_ms"]
    if m.get("g2g_metadata_coverage_pct") is not None:
        v["g2g_metadata_coverage_pct"] = m["g2g_metadata_coverage_pct"]
    v["samples_scored"] = len(scored)

    record["metrics"] = m
    record["distributions"] = dist
    return record


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


OPS = {
    "<=": lambda a, b: a <= b,
    ">=": lambda a, b: a >= b,
    "==": lambda a, b: a == b,
    "<": lambda a, b: a < b,
    ">": lambda a, b: a > b,
}


def _compare(op: str, actual: Any, threshold: Any) -> bool:
    fn = OPS.get(op)
    if fn is None:
        raise AnalysisError(f"unknown threshold operator {op!r}")
    if isinstance(threshold, bool) or isinstance(actual, bool):
        if op != "==":
            raise AnalysisError(
                f"operator {op!r} is not defined on a boolean threshold; "
                "matrix.yaml must state boolean rows as '=='")
        return bool(actual) == bool(threshold)
    return fn(actual, threshold)


def structural_invalidations(record: dict, matrix: dict) -> list[tuple[str, str]]:
    """Invalidations that come from the run's structure rather than a threshold row.

    Each is a (reason, detail) pair drawn from matrix.yaml's `invalid_reasons`
    vocabulary; a free-text reason is not analyzable.
    """
    out: list[tuple[str, str]] = []
    m = record.get("metrics") or {}
    cond = record["conditions"]

    if m.get("run_complete") is False:
        out.append(("session_lost_mid_run",
                    "run_metadata record absent from the snapshot file: the harness "
                    "did not reach the end of the run"))

    if m.get("session_drops"):
        detail = "; ".join(
            e.get("reason") or "unspecified"
            for e in (record.get("events") or [])
            if e["kind"] == "disconnected" and not e.get("harness_initiated", False))
        out.append(("session_lost_mid_run", f"terminal disconnect during the run: {detail}"))

    # A shaping axis at a non-default value with no shaper applied means a
    # requested condition was silently not applied.
    if cond.get("shaping_applied") is False:
        for axis_name in ("loss_pct", "jitter_ms"):
            if cond.get(axis_name):
                out.append(("session_lost_mid_run",
                            f"{axis_name}={cond[axis_name]} requested but "
                            "shaping_applied is false: the condition was never applied"))
                break

    if m.get("clock_sync_confidence") == "none":
        # Run-level only when the suite's own primary metrics need the clock
        # offset. RTT is skew-immune by construction, so a suite that asks for no
        # one-way figure stays valid with those columns merely suppressed.
        gated = sorted(theta_gated_metrics(matrix)
                       & set((matrix["suites"].get(record["suite"], {})
                              .get("primary")) or []))
        if gated:
            out.append(("clock_unsynchronized",
                        "clock_sync_confidence is `none` and this suite's primary "
                        f"metrics include {', '.join(gated)}, which cannot be "
                        "derived without a clock offset"))

    return out


def theta_gated_metrics(matrix: dict) -> set[str]:
    """Metrics whose `requires` clause names the clock offset.

    Derived from matrix.yaml rather than listed here, so a metric that gains or
    loses a theta dependency changes one file, not two.
    """
    out = set()
    for name, spec in matrix["metrics"].items():
        if not isinstance(spec, dict):
            continue
        requires = str(spec.get("requires", ""))
        if "clock_sync_confidence" in requires or "theta" in requires:
            out.add(name)
    return out


def _needs_one_way(record: dict, matrix: dict) -> bool:
    """Whether this run's suite cannot answer its question without a clock offset.

    Computed as the intersection of the suite's `primary` set with the
    theta-gated metrics, per matrix.yaml's scoping note on `clock_unsynchronized`
    — never a hardcoded suite list, so the rule stays correct when a suite's
    `primary` set changes.

    The intersection is wider than "the suites whose headline question is
    one-way". T-2 and T-4 carry `control_late_pct`, which is blocking under
    §8.2b: a blocking threshold whose value is null cannot be evaluated at all,
    so scoring such a run PASS would assert a bar was cleared that was never
    measured. Only the suites with no theta-gated primary metric survive with the
    one-way columns merely suppressed.
    """
    suite = matrix["suites"].get(record["suite"])
    if not suite:
        return False
    return bool(theta_gated_metrics(matrix) & set(suite.get("primary") or []))


def audio_column_invalid(record: dict) -> bool:
    """A silent audio source invalidates ONLY the audio columns, never the run.

    audio_level == 0 for the whole run makes every concealment figure meaningless,
    but the video and control measurements are untouched.

    `audio_level` is the MAXIMUM across scored polls, so this fires only when the
    source was never audible. A median would fire on a run that was merely
    intermittent — for instance one riding out a reconnect storm, where a
    re-subscribed track reads zero until samples flow — and would report a
    working tone generator as silent.
    """
    m = record.get("metrics") or {}
    if not record["conditions"].get("audio_enabled"):
        return False
    level = m.get("audio_level")
    return level is not None and level == 0


AUDIO_METRICS = {
    "audio_playout_delay_avg_ms", "audio_jitter_buffer_delay_ms",
    "audio_concealment_pct", "audio_silent_concealment_pct",
    "audio_concealment_events", "audio_accel_decel_pct", "audio_bitrate_bps",
    "audio_synthesized_pct", "audio_packets_lost_pct",
}


def score(record: dict, matrix: dict) -> dict:
    """Assigns the four-way verdict and fills `verdict` on the record.

    Order is load-bearing. Invalidation is evaluated FIRST and returns before any
    blocking threshold is examined, so an INVALID run can never also be reported as
    a FAIL. A run that did not measure the thing is not a run that failed.
    """
    m = record.get("metrics") or {}
    vocabulary = invalid_vocabulary(matrix)
    invalid_reasons: list[str] = []
    invalid_detail: list[str] = []

    def add_invalid(reason: str, detail: str) -> None:
        if reason not in vocabulary:
            raise AnalysisError(
                f"invalid_reason {reason!r} is not in matrix.yaml's vocabulary: "
                f"{sorted(vocabulary)}")
        if reason not in invalid_reasons:
            invalid_reasons.append(reason)
        invalid_detail.append(f"{reason}: {detail}")

    for reason, detail in structural_invalidations(record, matrix):
        add_invalid(reason, detail)

    # Column-scoped clock note for the suites that survive without an offset: the
    # one-way columns are empty and the reader must not read that as "measured
    # zero". Recorded in the detail list only; invalid_reasons stays empty.
    if (m.get("clock_sync_confidence") == "none"
            and not _needs_one_way(record, matrix)):
        invalid_detail.append(
            "clock_unsynchronized: clock_sync_confidence is `none`; the one-way "
            "and glass-to-glass columns are suppressed. This suite's primary "
            "metrics do not require a clock offset, so the run remains valid.")

    audio_invalid = audio_column_invalid(record)
    if audio_invalid:
        # Column-scoped, not run-scoped: recorded in the detail list and used to
        # suppress the audio observations, but it does not invalidate the run.
        invalid_detail.append(
            "silent_audio_source: audio_level was 0 at every scored poll, so the "
            "source was never audible; the audio columns are suppressed and the "
            "run remains valid for video and control")

    # Pass one: every invalidating row. Complete before any blocking threshold is
    # examined, so the two verdicts can never be produced from the same run.
    for row in matrix["thresholds"]:
        if row["effect"] != "invalidate":
            continue
        metric = row["metric"]
        actual = (record["conditions"].get("codec_mismatch")
                  if metric == "codec_mismatch" else m.get(metric))
        if actual is None:
            # A validity gate with no reading cannot clear itself. The gate this
            # matters most for is g2g_metadata_coverage_pct: a missing coverage
            # figure must not read as full coverage.
            if metric == "g2g_metadata_coverage_pct" and _subscribed_video(record):
                add_invalid(row["invalid_reason"],
                            "no metadata coverage figure was recorded for a run "
                            "that subscribed video")
            continue
        if not _compare(row["op"], actual, row["value"]):
            add_invalid(row["invalid_reason"],
                        f"{metric}={_fmt(actual)} violates {row['op']} {row['value']}")

    failed: list[dict] = []
    observed: list[dict] = []

    if invalid_reasons:
        # An INVALID run is not scored against any bar. Evaluating thresholds here
        # would report a malformed AV1 bitstream as an fps failure — the run did
        # not measure fps, it measured a broken encoder, and a matrix that says
        # both is a matrix that produces confident wrong answers.
        record["validity"]["valid"] = False
        record["validity"]["invalid_reasons"] = invalid_reasons
        record["validity"]["invalid_detail"] = invalid_detail
        record["verdict"] = {
            "status": INVALID,
            "failed_thresholds": [],
            "observed": [],
            "delta_from_reference": {},
            "notes": ("not scored against any threshold: "
                      + ", ".join(invalid_reasons)),
        }
        return record

    # A theta-gated metric is null BY CONSTRUCTION without a clock offset, and a
    # bar cannot be evaluated against a value that was never derivable. Suppressed
    # here as a property of the metric rather than of the suite: the run-level
    # invalidation above exempts T-5, and without this that suite
    # would hard-FAIL on rows whose inputs the run never had.
    clock_suppressed: set[str] = set()
    if m.get("clock_sync_confidence") == "none":
        clock_suppressed = theta_gated_metrics(matrix)

    # A validation gate is not a data-producing suite: its cells are configured to
    # answer one question, so a blocking bar its own design guarantees it will
    # violate must not become a FAIL. No suite currently declares validation_gate
    # (the playout-units gate is retired), so this is inert until one does.
    gate_only = bool(matrix["suites"].get(record["suite"], {}).get("validation_gate"))

    # Pass two: blocking and observational rows, reached only by a valid run.
    for row in matrix["thresholds"]:
        metric = row["metric"]
        effect = row["effect"]
        if effect == "invalidate":
            continue
        actual = m.get(metric)

        entry = {
            "metric": metric,
            "op": row["op"],
            "threshold": row["value"],
            "actual": actual,
            "clause": row.get("clause"),
            "provenance": row.get("provenance"),
            "effect": effect,
        }

        if metric in clock_suppressed:
            entry["suppressed"] = "clock_sync_confidence is `none`"
            observed.append(entry)
            continue

        if gate_only and effect == "fail":
            entry["suppressed"] = (
                f"{record['suite']} is a validation gate, not a data-producing "
                "suite; its cells are configured to answer one question and are "
                "not scored against the matrix's blocking bars")
            observed.append(entry)
            continue

        if effect == "observe":
            if metric in AUDIO_METRICS and audio_invalid:
                continue
            if actual is not None:
                entry["within"] = _compare(row["op"], actual, row["value"])
                entry["note"] = row.get("note")
                observed.append(entry)
            continue

        if actual is None:
            continue

        within = _compare(row["op"], actual, row["value"])
        if within:
            continue
        if effect == "fail":
            failed.append(entry)
        else:
            # An advisory breach is recorded, never filed as a failure. A
            # consumer reading the raw JSON must not see a PASS run that also
            # lists a failed threshold — the status and the list have to agree.
            entry["within"] = False
            entry["note"] = row.get("note")
            observed.append(entry)

    if any(f["effect"] == "fail" for f in failed):
        status = FAIL
    elif gate_only:
        # OBSERVE, not PASS: the run was never scored against a blocking bar, and
        # PASS would claim it cleared bars it was exempt from. OBSERVE is not a
        # weaker PASS — it is the verdict for a measurement with no threshold.
        status = OBSERVE
    else:
        status = PASS

    record["validity"]["valid"] = True
    record["validity"]["invalid_reasons"] = invalid_reasons
    record["validity"]["invalid_detail"] = invalid_detail
    record["verdict"] = {
        "status": status,
        "failed_thresholds": failed,
        "observed": observed,
        "delta_from_reference": {},
        "notes": "",
    }
    return record


def _subscribed_video(record: dict) -> bool:
    m = record.get("metrics") or {}
    return bool(m.get("frames_received") or m.get("video_fps_p50")
                or m.get("video_fps_delta"))


def _fmt(v: Any) -> str:
    if isinstance(v, float):
        return f"{v:.4g}"
    return str(v)


# ---------------------------------------------------------------------------
# There is NO validation gate in this matrix. V0_playout_units and its
# playout-units gate are retired along with the playout-delay hint buffering
# modes (matrix.yaml buffering_mode.retired_values): the harness never calls
# create_room_with_playout_delay, so there is no API unit left to disambiguate.
# The generic `validation_gate` handling in score() and analyze() is retained and
# simply finds no gate suites; re-adding a hint mode must re-add both the suite
# and a gate function here.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Grouping and aggregation
#
# never_pool_across is enforced in code, not in a comment: the group key always
# carries every non-poolable dimension, so no aggregation can span one.
# ---------------------------------------------------------------------------


def pool_key(record: dict, matrix: dict) -> tuple:
    """The dimensions across which results may never be pooled.

    Uses the ACTUAL codec, not the requested one. Grouping by the request would put
    a fallen-back run in the cell it claimed to be, which is the confusion the
    schema exists to prevent.
    """
    cond, env = record["conditions"], record["environment"]
    parts = []
    for key in never_pool_keys(matrix):
        if key == "video_codec":
            parts.append(("video_codec", cond.get("video_codec_actual")
                          or cond["video_codec_requested"]))
        elif key == "ran_profile":
            # All five fields, not just rlc_mode. The RAN hypothesis turns on the
            # discard and reordering timers as much as on AM-vs-UM, so keying on
            # the mode alone would pool the very configurations a lab RAN varies
            # to test it.
            ran = env.get("ran_profile")
            if isinstance(ran, dict):
                fields = matrix["axes"]["ran_profile"]["fields"]
                parts.append(("ran_profile",
                              tuple(str(ran.get(f, "unknown")) for f in fields)))
            else:
                parts.append(("ran_profile", str(ran)))
        elif key in cond:
            parts.append((key, cond[key]))
        elif key in env:
            parts.append((key, env[key]))
        else:
            parts.append((key, None))
    return tuple(parts)


class Cell:
    """Repeats of one cell within one non-poolable group."""

    def __init__(self, suite: str, cell_id: str, key: tuple):
        self.suite = suite
        self.cell_id = cell_id
        self.key = key
        self.runs: list[dict] = []

    @property
    def valid(self) -> list[dict]:
        return [r for r in self.runs if r["verdict"]["status"] != INVALID]

    @property
    def invalid(self) -> list[dict]:
        return [r for r in self.runs if r["verdict"]["status"] == INVALID]

    def values(self, metric: str) -> list[float]:
        return [r["metrics"][metric] for r in self.valid
                if r["metrics"].get(metric) is not None]

    def stat(self, metric: str) -> dict:
        vals = self.values(metric)
        lo, hi = spread(vals)
        return {"median": median(vals), "min": lo, "max": hi, "n": len(vals)}

    def verdict(self) -> str:
        """A cell's verdict across repeats.

        INVALID only when EVERY repeat was invalid — a cell with one usable repeat
        still measured something. Any valid FAIL makes the cell FAIL: a threshold
        breach that reproduces in one repeat out of three is still a breach, and
        the dispersion columns show the disagreement.
        """
        if not self.valid:
            return INVALID
        statuses = {r["verdict"]["status"] for r in self.valid}
        if FAIL in statuses:
            return FAIL
        if statuses == {OBSERVE}:
            return OBSERVE
        return PASS

    def repeats_disagree(self) -> bool:
        return len({r["verdict"]["status"] for r in self.valid}) > 1


def group_cells(records: Sequence[dict], matrix: dict) -> dict[str, list[Cell]]:
    """Groups runs into cells, per suite, never pooling across a forbidden axis."""
    by: dict[tuple, Cell] = {}
    for r in records:
        key = pool_key(r, matrix)
        ident = (r["suite"], r["cell_id"], key)
        cell = by.get(ident)
        if cell is None:
            cell = by[ident] = Cell(r["suite"], r["cell_id"], key)
        cell.runs.append(r)
    out: dict[str, list[Cell]] = defaultdict(list)
    for cell in by.values():
        out[cell.suite].append(cell)
    for cells in out.values():
        cells.sort(key=lambda c: (str(c.key), c.cell_id))
    return dict(out)


# ---------------------------------------------------------------------------
# Breakpoints
# ---------------------------------------------------------------------------


def _axis_value(record: dict, axis_name: str) -> Any:
    cond = record["conditions"]
    if axis_name == "video_codec":
        return cond.get("video_codec_actual") or cond["video_codec_requested"]
    return cond.get(axis_name)


def breakpoints(records: Sequence[dict], matrix: dict, suite_name: str,
                axis_name: str, metric: str) -> list[dict]:
    """Where a swept axis first crosses a blocking threshold, per non-poolable group.

    INVALID runs are excluded entirely — a run that did not measure the thing
    cannot mark the point where the thing broke. Both bracketing cells are
    reported, never just the crossing point, and when repeats disagree about which
    step crosses, the answer is a RANGE.
    """
    row = next((t for t in matrix["thresholds"]
                if t["metric"] == metric and t["effect"] == "fail"), None)
    if row is None:
        raise AnalysisError(
            f"breakpoint requested on {metric!r}, which has no blocking threshold "
            "in matrix.yaml; a breakpoint is only defined against a bar")
    if not _ordered_axis(matrix, axis_name):
        raise AnalysisError(
            f"breakpoint requested on axis {axis_name!r}, whose values are not "
            "ordered. 'First crossing' has no meaning without an ordering: the "
            "answer would depend on how the values happened to sort")

    # Every OTHER axis the suite sweeps is held fixed within a group. Without
    # this, T-3's three playout windows collapse into one curve and the sweep
    # reports a single crossing where the answer is a curve over the window —
    # the same pooling error never_pool_across prevents on the codec axis.
    suite = matrix["suites"].get(suite_name) or {}
    held = [a for a in (suite.get("sweep") or []) if a != axis_name]

    groups: dict[tuple, list[dict]] = defaultdict(list)
    for r in records:
        if r["suite"] != suite_name:
            continue
        if r["verdict"]["status"] == INVALID:
            continue
        key = pool_key(r, matrix) + tuple(
            (a, _axis_value(r, a)) for a in held if _axis_value(r, a) is not None)
        groups[key].append(r)

    results: list[dict] = []
    for key, runs in sorted(groups.items(), key=lambda kv: str(kv[0])):
        by_step: dict[Any, list[dict]] = defaultdict(list)
        for r in runs:
            v = _axis_value(r, axis_name)
            if v is not None:
                by_step[v].append(r)
        if not by_step:
            continue
        steps = sorted(by_step, key=_sortable)

        # Per-repeat crossing: the first step at which THAT repeat breaches.
        per_repeat: dict[int, Any] = {}
        for r in runs:
            per_repeat.setdefault(r["repeat_index"], None)
        for idx in list(per_repeat):
            for step in steps:
                run = next((r for r in by_step[step] if r["repeat_index"] == idx), None)
                if run is None:
                    continue
                val = run["metrics"].get(metric)
                if val is None:
                    continue
                if not _compare(row["op"], val, row["value"]):
                    per_repeat[idx] = step
                    break

        crossings = [s for s in per_repeat.values() if s is not None]
        entry: dict[str, Any] = {
            "group": dict(key),
            "axis": axis_name,
            "metric": metric,
            "threshold": f"{row['op']} {row['value']}",
            "provenance": row.get("provenance"),
            "clause": row.get("clause"),
            "steps": [
                {"value": s,
                 "median": median([r["metrics"].get(metric) for r in by_step[s]
                                   if r["metrics"].get(metric) is not None]),
                 "n": len(by_step[s]),
                 "invalid_excluded": 0}
                for s in steps
            ],
            "repeat_crossings": {k: v for k, v in per_repeat.items()},
        }

        if not crossings:
            entry["breakpoint"] = None
            entry["kind"] = "not_reached"
            entry["note"] = (f"no step in the swept range breached {metric} "
                             f"{row['op']} {row['value']}; the breakpoint lies beyond "
                             f"{steps[-1]} or does not exist")
        else:
            lo, hi = min(crossings, key=_sortable), max(crossings, key=_sortable)
            first = lo
            prior_idx = steps.index(first) - 1
            entry["last_passing"] = steps[prior_idx] if prior_idx >= 0 else None
            entry["first_failing"] = first
            if lo == hi:
                entry["breakpoint"] = lo
                entry["kind"] = "point"
            else:
                entry["breakpoint"] = [lo, hi]
                entry["kind"] = "range"
                entry["note"] = ("repeats disagree on the crossing step; reported as "
                                 "a range, not a point")
        results.append(entry)
    return results


def _ordered_axis(matrix: dict, axis_name: str) -> bool:
    """Whether an axis's values carry a natural ordering.

    A breakpoint is 'the value of the swept axis at which a threshold is first
    crossed', which requires the axis to be ordered. `loss_pct` and `concurrency`
    are; `video_codec` and `control_transport` are not, and asking for a
    breakpoint along them would produce a number that depends on nothing but
    alphabetical order.
    """
    ax = matrix["axes"].get(axis_name)
    if ax is None:
        return False
    vals = ax.get("values") or []
    return all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in vals)


def _sortable(v: Any) -> Any:
    if isinstance(v, bool):
        return (2, str(v))
    if isinstance(v, (int, float)):
        return (0, v)
    return (1, str(v))


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def _n(v: Any, digits: int = 1) -> str:
    if v is None:
        return "—"
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, float):
        if abs(v) >= 1e6:
            return f"{v/1e6:.2f}M"
        return f"{v:.{digits}f}"
    return str(v)


def _spread_cell(stat: dict, digits: int = 1) -> str:
    if stat["n"] == 0:
        return "—"
    if stat["n"] == 1:
        return _n(stat["median"], digits)
    if stat["min"] == stat["max"]:
        return _n(stat["median"], digits)
    return f"{_n(stat['median'], digits)} [{_n(stat['min'], digits)}–{_n(stat['max'], digits)}]"


def _table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    if not rows:
        return "_No scorable cells._\n"
    out = ["| " + " | ".join(headers) + " |",
           "|" + "|".join("---" for _ in headers) + "|"]
    for r in rows:
        out.append("| " + " | ".join(str(c) for c in r) + " |")
    return "\n".join(out) + "\n"


def _group_label(key: tuple) -> str:
    out = []
    for k, v in key:
        if v is None:
            continue
        if isinstance(v, tuple):
            # An all-identical RAN profile ("n/a" everywhere, or all "unknown")
            # reads as one value; a genuinely mixed one is shown in full, because
            # then the individual settings are the finding.
            v = v[0] if len(set(v)) == 1 else "/".join(v)
        out.append(f"{k}={v}")
    return ", ".join(out)


def _codec_of(key: tuple) -> Any:
    return dict(key).get("video_codec")


class Report:
    def __init__(self, records: Sequence[dict], matrix: dict):
        self.records = list(records)
        self.matrix = matrix
        self.cells = group_cells(self.records, matrix)

    def render(self) -> str:
        parts = [self._header()]
        order = ["T1_video_floor", "T2_loss_collapse", "T3_jitter_tolerance",
                 "T4_capacity", "T5_availability", "Q7_latency_definition"]
        for suite in order:
            if suite in self.cells:
                parts.append(getattr(self, f"_{suite.split('_')[0].lower()}_section")(suite))
        parts.append(self._validity_appendix())
        return "\n".join(p for p in parts if p)

    # -- header ------------------------------------------------------------

    def _header(self) -> str:
        meta = self.matrix["meta"]
        ref = self.matrix["reference_config"]
        counts = defaultdict(int)
        for r in self.records:
            counts[r["verdict"]["status"]] += 1
        tiers = sorted({r.get("tier") for r in self.records if r.get("tier") is not None})
        paths = sorted({r["environment"]["path"] for r in self.records})
        codecs = sorted({(r["conditions"].get("video_codec_actual")
                          or r["conditions"]["video_codec_requested"])
                         for r in self.records})
        tier_note = ""
        if paths and set(paths) <= {"loopback", "lan"}:
            tier_note = ("\n**Boundary:** every run in this report is on a "
                         f"{'/'.join(paths)} path. These results do not transfer to "
                         "cellular, and no RAN profile was in effect.\n")
        return f"""# Teleoperation test matrix — results

Sources: PRD {meta['sources']['prd']['page_id']} v{meta['sources']['prd']['version']},
Test Matrix {meta['sources']['test_matrix']['page_id']}
v{meta['sources']['test_matrix']['version']}, both fetched
{meta['sources']['prd']['fetched']}.

**Reference configuration** — the baseline every result is a delta from:
`{ref['video']['codec']}` at profile `{ref['video']['profile']}`, buffering
`{ref['buffering_mode']}`, control `{ref['control']['transport']}` at
{ref['control']['rate_hz']} Hz, audio {'on' if ref['audio']['enabled'] else 'off'}.
Buffering is `zero_jitter` for **every** run — the room-level playout-delay hint modes
are retired, per LiveKit's robotics low-latency guidance, so there is no buffering
delta in this report and no result should be read as one. The reference codec is still
chosen for portability (`h264` has a hardware encoder on every platform in the matrix,
AV1 does not), so codec deltas remain comparable across a MacBook, a Linux host and a
Jetson.

Runs: {len(self.records)} — {counts[PASS]} PASS, {counts[FAIL]} FAIL,
{counts[INVALID]} INVALID. Tier {', '.join(str(t) for t in tiers) or 'unrecorded'};
path {', '.join(paths) or 'unrecorded'}; codecs {', '.join(str(c) for c in codecs)}.
{tier_note}
Results are never pooled across {', '.join(never_pool_keys(self.matrix))} — each is a
separate experiment, and every table below is grouped accordingly.
"""

    # -- suites ------------------------------------------------------------

    def _t1_section(self, suite: str) -> str:
        cells = self.cells[suite]
        answer = self._t1_answer(cells)
        rows = []
        for c in sorted(cells, key=lambda c: (str(_codec_of(c.key)), c.cell_id)):
            g = dict(c.key)
            rows.append([
                str(g.get("video_codec")),
                str(g.get("encoder_tier")),
                # An all-INVALID cell has no measured resolution, so the cell id
                # is what distinguishes it from its neighbours in the table.
                (self._cell_axis(c, "video_profile") if c.valid
                 else f"{self._cell_axis(c, 'video_profile')} ({c.cell_id})"),
                # qp_avg sits immediately beside the bitrate it qualifies. A
                # bitrate figure must never be legible without its quantizer: a
                # low bitrate at a high QP is a smaller picture, not a more
                # efficient codec, and separating the columns invites exactly
                # that misreading.
                _spread_cell(c.stat("video_bitrate_bps")),
                _spread_cell(c.stat("qp_avg"), 1),
                _spread_cell(c.stat("quality_limitation_bandwidth_poll_pct"), 1),
                _spread_cell(c.stat("video_fps_p50")),
                f"{_n(c.stat('frame_width')['median'], 0)}x"
                f"{_n(c.stat('frame_height')['median'], 0)}",
                _spread_cell(c.stat("encode_time_avg_ms"), 2),
                _spread_cell(c.stat("g2g_p50_ms")),
                c.verdict(),
            ])
        body = (f"## T-1 — video floor\n\n{answer}\n\n"
                + _table(["codec", "encoder", "profile", "bitrate bps", "qp avg",
                          "bw-limited polls %", "fps p50", "actual res", "encode ms",
                          "g2g p50 ms", "verdict"], rows))
        bps = self._bp(suite, "video_bitrate_bps", ["uplink_mbps"])
        if bps:
            body += "\n" + bps
        body += ("\n**Caveats.** The bar is bitrate ≤ 5 Mbps (PRD §8.0b) at fps ≥ 27 "
                 "(governing-page-derived — PRD §7.1a states the 30 fps ladder but no "
                 "frame-rate threshold, so 27 must not be cited as a PRD requirement). "
                 "Rows are scored on the ACTUAL resolution, which the SDK may downscale "
                 "under bandwidth limitation. Encode time and fps ceiling are "
                 "encoder-tier-sensitive and every `sw`/`videotoolbox` row is provisional "
                 "until re-run on NVENC-class hardware. This table cannot say whether any "
                 "rung supports fine manipulation — that is a human judgment.\n\n"
                 "**A BITRATE COMPARISON ACROSS CODECS IS MEANINGLESS WITHOUT "
                 "MATCHED QUALITY, AND THESE ROWS ARE NOT MATCHED.** Every cell "
                 "here is encoded to a *bitrate target*, so each codec's rate "
                 "control chose its own quality to meet that target — the "
                 "quantizer is an output of the experiment, not a control. Two "
                 "rows at different QP are two different pictures, and the row "
                 "with fewer bits is not thereby the more efficient codec. On the "
                 "2026-08-25 Tier 0 sweep this was not hypothetical: AV1 ran at "
                 "QP 40.0 while H264 ran at QP 25-27 on comparable frame counts, "
                 "so the AV1 bitrate advantage on that sweep is a quality "
                 "difference, not an efficiency result, and no efficiency claim "
                 "may be drawn from it. Compare bitrate BETWEEN ROWS OF THE SAME "
                 "CODEC, where rate control faced the same problem; across "
                 "codecs, read the QP column first and treat the bitrates as "
                 "incomparable unless the quantizers happen to match.\n\n"
                 "Answering the codec question properly needs fixed-QUALITY "
                 "encoding — pin QP or CQ across codecs and measure the resulting "
                 "bitrate, inverting what is controlled and what is measured. "
                 "That is a suite redesign, not a threshold change, and this SDK "
                 "does not currently expose a quality target: `VideoEncoding` "
                 "carries only `max_bitrate` and `max_framerate`, and "
                 "`RtpEncodingParameters` adds only resolution scaling and "
                 "scalability mode. See MEASUREMENT-DESIGN and SDK-FINDINGS.\n\n"
                 "**Reading the two quality columns.** They exist so that "
                 "\"this codec is more efficient\" and \"this codec quietly dropped "
                 "quality to fit\" are distinguishable; a low bitrate alone cannot tell "
                 "them apart. `qp avg` is the mean quantizer, from the cumulative "
                 "`qp_sum` differenced against `frames_encoded` — **higher means more "
                 "compression and lower quality**. QP scales are codec-specific, so it "
                 "is comparable BETWEEN ROWS OF THE SAME CODEC ONLY and must never be "
                 "pooled or compared across codecs (design §8). `bw-limited polls %` is "
                 "the share of scored polls whose `quality_limitation_reason` was "
                 "`bandwidth`; unlike QP it counts an encoder-reported condition in "
                 "units no codec defines differently, so it IS cross-codec comparable, "
                 "and it is the column that makes a bandwidth-starved row visible. "
                 "Together with `video_target_bitrate_bps` these are the ONLY "
                 "publisher-side bandwidth signals available: the transport's "
                 "`subscriber_available_outgoing_bitrate_bps` is the SUBSCRIBER "
                 "peer connection's estimate, which sends only RTCP and so sits "
                 "permanently at libwebrtc's 300 kbps default. It is recorded "
                 "under that name, never extracted as a metric, and must not be "
                 "read as an uplink estimate.\n")
        return body

    def _t1_answer(self, cells: Sequence[Cell]) -> str:
        fitting: dict[Any, list[str]] = defaultdict(list)
        for c in cells:
            if c.verdict() != PASS:
                continue
            codec = _codec_of(c.key)
            prof = self._cell_axis(c, "video_profile")
            if prof:
                fitting[codec].append(prof)
        if not fitting:
            return ("**No profile x codec combination cleared both bars in this data.** "
                    "See the validity appendix before reading that as a finding.")
        parts = [f"**{k}**: {', '.join(sorted(set(v)))}" for k, v in sorted(
            fitting.items(), key=lambda kv: str(kv[0]))]
        return ("Profiles whose emitted bitrate fell under the 5 Mbps ceiling at "
                "≥ 27 fps, per codec, **at whatever quality each codec's rate "
                "control chose** — read the qp avg column before comparing any two "
                "codecs, and see the caveat below — "
                + "; ".join(parts) + ". Listed per codec and never averaged — an "
                "average across codecs describes no configuration that exists, and "
                "these rows are not at matched quality, so the bitrates are not "
                "comparable across codecs at all.")

    def _t2_section(self, suite: str) -> str:
        cells = self.cells[suite]
        rows = []
        for c in self._by_axis(cells, "loss_pct"):
            g = dict(c.key)
            rows.append([
                str(g.get("control_transport")), str(g.get("video_codec")),
                str(g.get("buffering_mode")),
                self._cell_axis(c, "loss_pct"),
                _spread_cell(c.stat("control_delivered_pct"), 3),
                _spread_cell(c.stat("control_effective_rate_hz")),
                _spread_cell(c.stat("control_late_pct"), 3),
                _spread_cell(c.stat("control_gap_p99_ms"), 1),
                _spread_cell(c.stat("control_max_gap_ms"), 1),
                _spread_cell(c.stat("video_freeze_count"), 0),
                self._kf_summary(c),
                c.verdict(),
            ])
        body = ("## T-2 — loss collapse\n\n"
                + self._t2_answer(suite) + "\n\n"
                + _table(["transport", "codec", "buffering", "loss %", "delivered %",
                          "rate Hz", "late %", "gap p99 ms", "max gap ms",
                          "freezes", "kf polls", "verdict"], rows))
        for metric in ("control_delivered_pct", "control_late_pct"):
            bp = self._bp(suite, metric, ["loss_pct"])
            if bp:
                body += "\n" + bp
        body += ("\n**Caveats.** The transports have opposite failure signatures and are "
                 "never pooled: on `dc_reliable` SCTP retransmits, so loss appears as "
                 "latency (delivered stays near 100% while late % explodes); on "
                 "`dc_lossy` and `data_track_buf1` samples vanish outright. Keyframe "
                 "recovery is reported in POLL INTERVALS, not milliseconds — the "
                 "measurement resolution equals the poll period. The two gap columns "
                 "are in milliseconds (gap length divided by the publisher rate) and "
                 "are OBSERVE, not scored: `max gap ms` is a single worst-case event, "
                 "not a distribution, and it is reported because a stall short enough "
                 "to vanish into `delivered %` can still be long enough to matter — a "
                 "121-sample gap is 605 ms at 200 Hz but only 0.58% of samples. This "
                 "suite resolves what the collapse loss IS, not what the PRD's "
                 "historical figure was.\n")
        return body

    def _t2_answer(self, suite: str) -> str:
        bps = breakpoints(self.records, self.matrix, suite, "loss_pct",
                          "control_delivered_pct")
        found = [b for b in bps if b.get("breakpoint") is not None]
        if not found:
            return ("**No loss step in the swept range broke control delivery below "
                    "99.9%.** The collapse point lies beyond the sweep or the sweep "
                    "did not run — check the step table and the validity appendix.")
        parts = []
        for b in found:
            g = b["group"]
            bp = b["breakpoint"]
            label = f"{bp[0]}–{bp[1]}%" if isinstance(bp, list) else f"{bp}%"
            parts.append(f"{g.get('control_transport')}/{g.get('video_codec')}: {label}")
        return ("Control delivery first falls below 99.9% at — " + "; ".join(parts)
                + ". Per transport and per codec; the bracketing steps are in the "
                "breakpoint tables below.")

    def _t3_section(self, suite: str) -> str:
        cells = self.cells[suite]
        rows = []
        # Window first, then jitter: each window's curve then reads as a
        # contiguous block, which is the shape of T-3's answer.
        for c in self._by_axis(cells, "playout_window_ms", "jitter_ms"):
            g = dict(c.key)
            rows.append([
                str(g.get("control_transport")),
                self._cell_axis(c, "playout_window_ms"),
                self._cell_axis(c, "jitter_ms"),
                _spread_cell(c.stat("control_late_pct"), 3),
                _spread_cell(c.stat("control_jitter_ms"), 2),
                _spread_cell(c.stat("control_owd_p50_ms")),
                _spread_cell(c.stat("control_owd_p99_ms")),
                c.verdict(),
            ])
        body = ("## T-3 — jitter tolerance\n\n"
                "T-3's answer is a curve, not a number: the maximum tolerable jitter "
                "depends on the playout window, and the window is Figure's "
                "configuration choice (§8.2a). The curve is the table below, read "
                "along `jitter ms` at each window.\n\n"
                + _table(["transport", "window ms", "jitter ms", "late %",
                          "measured jitter ms", "owd p50", "owd p99", "verdict"], rows))
        bp = self._bp(suite, "control_late_pct", ["jitter_ms"])
        if bp:
            body += "\n" + bp
        body += ("\n**Caveats.** `control_late_pct` requires a valid clock offset; runs "
                 "with `clock_sync_confidence: none` are INVALID for this suite rather "
                 "than reported as zero late. The one-way figures carry the "
                 "(d_in−d_out)/2 residual and are not exact on an asymmetric link.\n")
        return body

    def _t4_section(self, suite: str) -> str:
        cells = self.cells[suite]
        rows = []
        for c in self._by_axis(cells, "concurrency"):
            rows.append([
                self._cell_axis(c, "concurrency"),
                _spread_cell(c.stat("network_rtt_p95_ms")),
                _spread_cell(c.stat("app_rtt_p95_ms")),
                _spread_cell(c.stat("control_delivered_pct"), 3),
                _spread_cell(c.stat("control_late_pct"), 3),
                _spread_cell(c.stat("video_fps_p50")),
                _spread_cell(c.stat("audio_playout_delay_avg_ms")),
                _spread_cell(c.stat("audio_concealment_pct"), 2),
                _spread_cell(c.stat("poll_overbudget_pct"), 2),
                c.verdict(),
            ])
        body = ("## T-4 — capacity\n\n"
                + self._t4_answer(suite) + "\n\n"
                + _table(["sessions", "network rtt p95 ms", "app rtt p95 ms",
                          "delivered %", "late %", "fps p50",
                          "audio playout ms", "conceal %", "poll over %", "verdict"],
                         rows))
        bp = self._bp(suite, "network_rtt_p95_ms", ["concurrency"])
        if bp:
            body += "\n" + bp
        body += ("\n**Caveats.** This is an SFU-side session limit on one instance. It is "
                 "**not** cell uplink capacity and therefore not a per-site number — "
                 "§8.7a requires the tighter of two limits and this harness measures "
                 "one. Load generators must run on separate hosts; if they share a NIC "
                 "with the probe, `poll_overbudget_pct` rises and client saturation is "
                 "misattributed to the SFU. The 90 ms bar is scored against the "
                 "**network** round trip; the app round trip is the same loop plus "
                 "control-publisher scheduling at both ends and is reported for "
                 "contrast, never against the bar. Audio playout delay is the "
                 "playout-side share only, not mouth-to-ear.\n")
        return body

    def _t4_answer(self, suite: str) -> str:
        bps = breakpoints(self.records, self.matrix, suite, "concurrency", "network_rtt_p95_ms")
        found = [b for b in bps if b.get("breakpoint") is not None]
        if not found:
            return ("**No concurrency step in the swept range breached the 90 ms "
                    "network RTT p95 bar.** The limit lies beyond the sweep.")
        b = found[0]
        bp = b["breakpoint"]
        label = f"{bp[0]}–{bp[1]}" if isinstance(bp, list) else str(bp)
        return (f"RTT p95 first exceeds 90 ms at **{label} concurrent sessions** "
                f"(last passing: {b.get('last_passing')}).")

    def _t5_section(self, suite: str) -> str:
        cells = self.cells[suite]
        rows = []
        for c in sorted(cells, key=lambda c: c.cell_id):
            rows.append([
                self._cell_axis(c, "fault_injection"),
                _spread_cell(c.stat("session_drops"), 0),
                _spread_cell(c.stat("reconnect_count"), 0),
                _spread_cell(c.stat("recovery_p95_ms"), 0),
                _spread_cell(c.stat("ice_selected_pair_changes"), 0),
                _spread_cell(c.stat("control_delivered_pct"), 3),
                _spread_cell(c.stat("join_to_first_video_ms"), 0),
                c.verdict(),
            ])
        return ("## T-5 — availability and recovery\n\n"
                "A survived reconnect is not a drop: §8.6b explicitly permits it, so "
                "`reconnect_count` is reported alongside `session_drops` and never "
                "scored as one.\n\n"
                + _table(["fault", "drops", "reconnects", "recovery p95 ms",
                          "ICE pair changes", "delivered %", "join→video ms",
                          "verdict"], rows)
                + "\n**Caveats.** Netem fault injection approximates a fade; it is not a "
                  "fade, and nothing here speaks to real radio events. ICE failures "
                  "shorter than the poll period are invisible — the state is sampled, "
                  "not evented.\n")

    def _q7_section(self, suite: str) -> str:
        cells = self.cells[suite]
        rows = []
        for c in sorted(cells, key=lambda c: (str(_codec_of(c.key)), c.cell_id)):
            g = dict(c.key)
            # The ratio denominator is the NETWORK round trip. Using the probe's
            # application-loop RTT here produced ratios below 1.0 (av1 read 0.82x),
            # which is impossible: glass-to-glass contains a network traversal and
            # cannot be faster than one. The app loop simply is not a network RTT.
            net_rtt = c.stat("network_rtt_p50_ms")
            app_rtt = c.stat("app_rtt_p50_ms")
            owd = c.stat("control_owd_p50_ms")
            g2g = c.stat("g2g_p50_ms")
            ratio = "—"
            # `is not None` rather than truthiness: an RTT of 0 is a real
            # loopback reading, not a missing one. Zero is excluded only because
            # the ratio is undefined there, and that is stated rather than shown
            # as an em dash indistinguishable from "not measured".
            if net_rtt["median"] is not None and g2g["median"] is not None:
                ratio = (f"{g2g['median'] / net_rtt['median']:.2f}x"
                         if net_rtt["median"] > 0 else "n/a (RTT 0)")
            rows.append([
                str(g.get("video_codec")), str(g.get("buffering_mode")),
                self._cell_axis(c, "owd_ms"),
                _spread_cell(net_rtt), _spread_cell(app_rtt),
                _spread_cell(owd), _spread_cell(g2g), ratio,
                _spread_cell(c.stat("encode_time_avg_ms"), 2),
                _spread_cell(c.stat("assembly_time_avg_ms"), 2),
                _spread_cell(c.stat("decode_time_avg_ms"), 2),
                _spread_cell(c.stat("jitter_buffer_delay_avg_ms"), 2),
                c.verdict(),
            ])
        return ("## Q-7 — which latency measure the 20–90 ms range applies to\n\n"
                + self._q7_answer(cells) + "\n\n"
                + _table(["codec", "buffering", "added owd ms", "network RTT p50",
                          "app RTT p50", "one-way p50", "g2g p50", "g2g/net RTT",
                          "encode", "assembly", "decode", "jitter buf", "verdict"], rows)
                + "\n**Caveats.** The two RTT columns measure different paths and "
                  "differ by roughly 2x, so they are never interchanged. "
                  "`network_rtt_p50_ms` is the ICE consent round trip on the selected "
                  "candidate pair — the network path, and the denominator of the ratio "
                  "column. `app_rtt_p50_ms` is the four-timestamp probe: publisher → "
                  "SFU → subscriber → SFU → publisher over the control transport, "
                  "including control-publisher scheduling at the sender and echo "
                  "dispatch at the receiver. Only the network column is scored against "
                  "§8.1a. "
                  "`g2g_p50_ms` is capture → app-delivery, not camera → "
                  "photons: it excludes display and compositor latency, and the pixel "
                  "measurement is a manual Tier 2 procedure. The decomposition columns "
                  "are codec- and encoder-tier-sensitive and are reported per codec "
                  "precisely because their ratio is the answer — an average across AV1 "
                  "and H264 describes no configuration that exists. The 100 ms G2G bar "
                  "is a PRD SHOULD promoted to blocking by the governing test spec, not "
                  "a PRD MUST. Where a row looks bandwidth-constrained, read "
                  "`quality_limitation_bandwidth_poll_pct` and "
                  "`video_target_bitrate_bps` — the transport's "
                  "`subscriber_available_outgoing_bitrate_bps` describes the "
                  "subscriber peer connection, which carries no media upstream, and "
                  "is not an uplink estimate. This suite supplies the evidence for "
                  "which measure the PRD meant; it cannot say what the authors "
                  "intended.\n")

    def _q7_answer(self, cells: Sequence[Cell]) -> str:
        per_codec = []
        for c in cells:
            net = c.stat("network_rtt_p50_ms")["median"]
            app = c.stat("app_rtt_p50_ms")["median"]
            g2g = c.stat("g2g_p50_ms")["median"]
            if net is not None and g2g is not None and net > 0:
                per_codec.append((_codec_of(c.key), net, app, g2g, g2g / net))
        if not per_codec:
            return ("**Not settled by this data**: no cell produced both an RTT and a "
                    "G2G figure. A run with `clock_sync_confidence >= probe` and G2G "
                    "metadata coverage ≥ 95% on at least one cell per codec would "
                    "settle it.")
        parts = []
        for codec, net, app, g2g, ratio in sorted(per_codec, key=lambda x: str(x[0])):
            app_txt = f", app loop {app:.0f} ms" if app is not None else ""
            parts.append(f"{codec}: network RTT {net:.0f} ms{app_txt} → "
                         f"G2G {g2g:.0f} ms ({ratio:.1f}x network RTT)")
        return ("Glass-to-glass runs well above network RTT, per codec — "
                + "; ".join(parts) + ". The ratio is taken against the **network** "
                "round trip; against the application loop it would understate the gap "
                "roughly twofold, because that loop already contains a network "
                "traversal plus scheduling at both ends. Whichever measure the "
                "20–90 ms range applies to changes the verdict, and the ratio between "
                "the columns is codec-dependent, which is why they are never pooled.")

    # -- helpers -----------------------------------------------------------

    def _by_axis(self, cells: Sequence[Cell], *axis_names: str) -> list[Cell]:
        """Cells ordered along one or more swept axes, in the order given.

        Numeric sweeps read in numeric order (0, 0.5, 1, 2, 5, 10 — not
        0, 0.5, 1, 10, 2). With several axes the first is major, so T-3's
        per-window curves each read as a contiguous block.
        """
        def axis_key(c: Cell, axis_name: str):
            vals = [v for v in (_axis_value(r, axis_name) for r in c.runs)
                    if v is not None]
            return _sortable(min(vals, key=_sortable)) if vals else (3, "")

        def key(c: Cell):
            return (tuple(axis_key(c, a) for a in axis_names),
                    str(c.key), c.cell_id)
        return sorted(cells, key=key)

    def _cell_axis(self, cell: Cell, axis_name: str) -> str:
        vals = {_axis_value(r, axis_name) for r in cell.runs}
        vals.discard(None)
        if not vals:
            return "—"
        return ", ".join(str(v) for v in sorted(vals, key=_sortable))

    def _kf_summary(self, cell: Cell) -> str:
        allv: list[float] = []
        mx = None
        for r in cell.valid:
            d = (r.get("distributions") or {}).get("keyframe_service_polls")
            if d:
                allv.extend(d["values"])
                mx = d["max"] if mx is None else max(mx, d["max"])
        if not allv:
            return "—"
        return f"med {median(allv):.0f}, max {mx:.0f}"

    def _bp(self, suite: str, metric: str, axes: Sequence[str]) -> str:
        out = []
        for axis_name in axes:
            try:
                bps = breakpoints(self.records, self.matrix, suite, axis_name, metric)
            except AnalysisError:
                continue
            for b in bps:
                if len(b["steps"]) < 2:
                    continue
                rows = [[_n(s["value"], 2), _n(s["median"], 3), str(s["n"])]
                        for s in b["steps"]]
                title = (f"**Breakpoint — {metric} vs {axis_name}** "
                         f"({_group_label(tuple(b['group'].items()))})")
                if b["breakpoint"] is None:
                    verdict = f"Not reached within the sweep. {b.get('note', '')}"
                elif b["kind"] == "range":
                    verdict = (f"**Range {b['breakpoint'][0]}–{b['breakpoint'][1]}** — "
                               "repeats disagree on the crossing step, so this is "
                               "reported as a range, not a point. Last passing step: "
                               f"{b.get('last_passing')}.")
                else:
                    verdict = (f"**{b['breakpoint']}** — first step breaching "
                               f"{metric} {b['threshold']}. Last passing step: "
                               f"{b.get('last_passing')}; both bracketing cells are in "
                               "the table.")
                out.append(title + "\n\n" + verdict + "\n\n"
                           + _table([axis_name, f"{metric} (median)", "n"], rows))
        return "\n".join(out)

    def _validity_appendix(self) -> str:
        invalid = [r for r in self.records if r["verdict"]["status"] == INVALID]
        body = "## Validity appendix\n\n"
        if not invalid:
            body += "No run scored INVALID.\n\n"
        else:
            rows = []
            for r in sorted(invalid, key=lambda r: r["run_id"]):
                rows.append([
                    r["run_id"], r["suite"], r["cell_id"],
                    ", ".join(r["validity"]["invalid_reasons"]),
                    "; ".join(r["validity"]["invalid_detail"])[:200],
                ])
            body += (f"{len(invalid)} run(s) scored INVALID. An INVALID run did not "
                     "measure the thing: it is excluded from every breakpoint and is "
                     "never counted as a failure.\n\n"
                     + _table(["run", "suite", "cell", "reason", "detail"], rows))
        counts = defaultdict(int)
        for r in invalid:
            for reason in r["validity"]["invalid_reasons"]:
                counts[reason] += 1
        if counts:
            body += "\n" + _table(
                ["reason", "runs"],
                [[k, str(v)] for k, v in sorted(counts.items(), key=lambda kv: -kv[1])])

        audio_suppressed = [r for r in self.records if audio_column_invalid(r)]
        if audio_suppressed:
            body += (f"\n{len(audio_suppressed)} run(s) never registered any audio: "
                     "`audio_level` was 0 at every scored poll, so the source was "
                     "never audible. Their AUDIO columns are suppressed; the runs "
                     "remain valid for video and control, and are not INVALID. The "
                     "test is on the maximum, not the median, so a run that was "
                     "merely intermittent — a reconnect storm, say — is not listed "
                     "here.\n")

        clamped = [r for r in self.records
                   if (r["metrics"] or {}).get("video_packets_lost_clamp_events")]
        if clamped:
            rows = [[r["run_id"],
                     str(r["metrics"]["video_packets_lost_clamp_events"]),
                     _n(r["metrics"].get("video_packets_lost_clamped_min"), 0)]
                    for r in sorted(clamped, key=lambda r: r["run_id"])]
            body += ("\n**Negative packets_lost deltas (reorder/duplicate).** The "
                     "harness clamps the delta at zero and surfaces the pre-clamp "
                     "value; a negative delta is a reorder artifact, not a gain. "
                     "Loss percentages in these runs are lower bounds.\n\n"
                     + _table(["run", "clamp events", "most negative delta"], rows))

        # A control stall large enough to matter against the latency budget, on a
        # run that had no induced loss to explain it. Surfaced unconditionally
        # because no other column shows it: a 605 ms gap was 0.58% of samples on
        # the run that prompted this, which delivered-% absorbs without a trace.
        STALL_MS = 100.0
        stalls = []
        for r in self.records:
            gap_ms = (r["metrics"] or {}).get("control_max_gap_ms")
            if gap_ms is None or gap_ms < STALL_MS:
                continue
            induced = (r["conditions"] or {}).get("loss_pct") or 0
            stalls.append((r, gap_ms, induced))
        unexplained = [t for t in stalls if not t[2]]
        if unexplained:
            rows = [[r["run_id"], _n(gap_ms, 0),
                     _n((r["metrics"] or {}).get("control_gap_p99_ms"), 1),
                     _n((r["metrics"] or {}).get("control_delivered_pct"), 3)]
                    for r, gap_ms, _ in sorted(unexplained, key=lambda t: -t[1])[:10]]
            body += (f"\n**Control-path stalls with no induced loss.** "
                     f"{len(unexplained)} run(s) saw the control stream stop for "
                     f"≥{STALL_MS:.0f} ms on a path where the matrix injected no "
                     "loss at all. These are transport events, not harness stalls — "
                     "check `poll_overbudget_pct` in the same runs before reading "
                     "them any other way. They are reported because a stall of this "
                     "size is material to a 90 ms budget while being far too small "
                     "to move `delivered %`, which is why the two columns are shown "
                     "together below. OBSERVE: a worst-case event is not a "
                     "distribution and nothing here is scored against a bar.\n\n"
                     + _table(["run", "max gap ms", "gap p99 ms", "delivered %"], rows))

        # Runs recorded before the probe tracker gained an explicit aged-out count
        # have no `probes_lost` in their snapshots, so their probe loss could only
        # be derived as `sent - completed`. That form counts DISPLACEMENT as loss.
        legacy_loss = [r for r in self.records
                       if (r["metrics"] or {}).get("probe_loss_legacy_derivation")
                       and (r["metrics"] or {}).get("probe_loss_pct")]
        if legacy_loss:
            rows = [[r["run_id"], _n(r["metrics"]["probe_loss_pct"], 1)]
                    for r in sorted(legacy_loss,
                                    key=lambda r: -(r["metrics"]["probe_loss_pct"] or 0))[:10]]
            body += ("\n**Probe loss in these runs is an OVERSTATEMENT, not a "
                     "measurement.** Their snapshots predate the explicit aged-out "
                     "count, so `probe_loss_pct` could only be derived as "
                     "`sent - completed`. The tracker that produced them held a "
                     "single outstanding probe and retired it the moment the next "
                     "was issued, so an echo that merely arrived after its successor "
                     "left was counted lost even though the network delivered it. "
                     "Across the 2026-08-25 Tier 0 sweep, the 56 runs whose control "
                     "path was delivering normally showed a pooled 2.2% by this "
                     "derivation against a true loss near zero. Read these figures "
                     "as a ceiling. Runs whose control path was itself degraded are "
                     "the exception — there the loss is real, and it is visible in "
                     "`control_effective_rate_hz` rather than here.\n\n"
                     + _table(["run", "probe loss % (ceiling)"], rows))

        rpc = [r for r in self.records if (r["metrics"] or {}).get("stats_rpc_failures")]
        if rpc:
            rows = [[r["run_id"], str(r["metrics"]["stats_rpc_failures"]),
                     _n(r["metrics"].get("stats_rpc_failure_pct"), 1)]
                    for r in sorted(rpc, key=lambda r: r["run_id"])]
            body += ("\n**Stats RPC failures.** A failed stats poll is not the same as "
                     "an empty one; these runs have thinner data than their poll count "
                     "suggests.\n\n"
                     + _table(["run", "failures", "% of polls"], rows))

        thin = [r for r in self.records
                if r["verdict"]["status"] != INVALID
                and (r["metrics"] or {}).get("app_rtt_sample_count") is not None
                and r["metrics"]["app_rtt_sample_count"] < 30]
        if thin:
            body += (f"\n{len(thin)} valid run(s) had fewer than 30 completed probes, so "
                     "their app RTT percentiles are null rather than computed from a "
                     "thin sample. Those cells show no app RTT figure; they are not "
                     "failures. The network RTT columns are unaffected — they come from "
                     "the stats poll, not the probe.\n")
        return body


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


def default_base_dir(runs: Path) -> Path:
    """Root for resolving the relative artifact paths inside a run record.

    THE INVARIANT, from run_matrix.py:64-65: `runs/` and `snapshots/` are
    SIBLINGS under one root, and a record's `snapshots_jsonl_path` is written
    relative to that root ("snapshots/<run_id>.jsonl"). So the base directory is
    always the parent of `runs/`, and the two argument forms sit at different
    depths:

        --runs <root>/runs/            -> root is runs.parent
        --runs <root>/runs/T2.jsonl    -> root is runs.parent.parent

    Getting this wrong resolves every snapshot path into a directory that does
    not exist. That used to surface as a whole matrix of INVALID runs blaming
    the network; it now raises, but the depth still has to be right.
    """
    return runs.parent if runs.is_dir() else runs.parent.parent


def load_run_records(path: Path) -> list[dict]:
    """Reads run records from a JSONL file or every *.jsonl in a directory."""
    files = sorted(path.glob("*.jsonl")) if path.is_dir() else [path]
    records = []
    for f in files:
        if f.name.endswith(".seq.jsonl"):
            continue
        with open(f) as fh:
            for lineno, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError as e:
                    raise AnalysisError(f"{f}:{lineno}: malformed JSON: {e}") from e
                if "run_id" not in rec:
                    continue
                rec["_source_file"] = str(f)
                records.append(rec)
    return records


def analyze(records: Sequence[dict], matrix: dict, base_dir: Path) -> list[dict]:
    """Extracts and scores every run, gate suites genuinely first.

    This matrix currently declares NO validation_gate suite — the playout-units
    gate is retired with the hint buffering modes — so gate_suites is empty and
    the ordering below is a no-op. It is retained rather than deleted because a
    gate that runs after the matrix it validates is not a gate, and re-adding one
    must not require rediscovering that.
    """
    gate_suites = {name for name, s in matrix["suites"].items()
                   if s.get("validation_gate")}

    ordered = ([r for r in records if r["suite"] in gate_suites]
               + [r for r in records if r["suite"] not in gate_suites])

    out = []
    for rec in ordered:
        extract(rec, matrix, base_dir=base_dir)
        out.append(rec)

    for rec in out:
        score(rec, matrix)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--runs", type=Path, required=True,
                    help="run-record JSONL file, or a directory of them")
    ap.add_argument("--matrix", type=Path, default=MATRIX_PATH)
    ap.add_argument("--report", type=Path,
                    help="write the markdown report here (default: stdout)")
    ap.add_argument("--json", type=Path,
                    help="write scored run records here as JSONL")
    ap.add_argument("--base-dir", type=Path,
                    help="root for relative snapshot paths (default: --runs' parent)")
    args = ap.parse_args()

    matrix = load_matrix(args.matrix)
    records = load_run_records(args.runs)
    if not records:
        print(f"no run records found under {args.runs}", file=sys.stderr)
        return 2

    base = args.base_dir or default_base_dir(args.runs)
    try:
        scored = analyze(records, matrix, base)
    except AnalysisError as e:
        # An operator error, reported as one. A traceback here would read as a
        # crash in the analysis rather than a wrong path on the command line.
        print(f"error: {e}", file=sys.stderr)
        print(f"       (base directory resolved to {base})", file=sys.stderr)
        return 2

    if args.json:
        with open(args.json, "w") as f:
            for r in scored:
                r.pop("_source_file", None)
                f.write(json.dumps(r) + "\n")

    report = Report(scored, matrix)
    text = report.render()
    if args.report:
        args.report.write_text(text)
        print(f"wrote {args.report}")
    else:
        print(text)

    return 0


if __name__ == "__main__":
    sys.exit(main())
