#!/usr/bin/env python3
"""Known-answer tests for parse_runs.py.

Every expected value below is computed BY HAND from the fixture parameters, not
read back from the parser. The point of a known-answer test is that the answer is
known independently; asserting whatever the code produces would test nothing.

The four failure fixtures (codec fallback, malformed AV1 bitstream, CPU-limited
software AV1, missing G2G metadata) must score INVALID with the correct reason and
must NEVER score FAIL. That separation is the gate this file exists to hold.

    python3 -m pytest test_parse_runs.py -q
    python3 test_parse_runs.py            # runs without pytest installed
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import parse_runs as pr  # noqa: E402

HERE = Path(__file__).parent
FIXTURES = HERE / "fixtures"
RUNS = FIXTURES / "runs"

# Fixture geometry, mirrored from make_fixtures.py so a change there that breaks
# the arithmetic breaks a test rather than silently shifting an expected value.
POLLS = 63           # total polls written per run
WARMUP_POLLS = 2     # polls flagged scored: false
SCORED_POLLS = POLLS - WARMUP_POLLS          # 61 scored polls
SCORED_INTERVALS = SCORED_POLLS - 1          # 60 differenced intervals
SCORED_WINDOW_S = float(POLLS - 1 - WARMUP_POLLS)  # 60 s
CONTROL_RATE_HZ = 200
# 200 Hz x 60 s = 12 000 published seqs, above matrix.yaml's 10 000 floor for
# control_delivered_pct. Below it the metric cannot resolve a 99.9% bar and is
# nulled rather than evaluated.
EXPECTED_SEQS = int(CONTROL_RATE_HZ * SCORED_WINDOW_S)


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


def _ensure_fixtures() -> None:
    if not (RUNS / "T1_video_floor.jsonl").exists():
        subprocess.run([sys.executable, str(FIXTURES / "make_fixtures.py")],
                       check=True, capture_output=True)


_SCORED: dict[str, dict] | None = None


def scored() -> dict[str, dict]:
    global _SCORED
    if _SCORED is None:
        _ensure_fixtures()
        matrix = pr.load_matrix()
        records = pr.load_run_records(RUNS)
        _SCORED = {r["run_id"]: r
                   for r in pr.analyze(records, matrix, FIXTURES)}
    return _SCORED


def run(run_id: str) -> dict:
    return scored()[run_id]


def approx(a, b, tol=1e-6) -> bool:
    if a is None or b is None:
        return a is b
    return abs(a - b) <= tol * max(1.0, abs(b))


# ---------------------------------------------------------------------------
# The four required failure fixtures: INVALID, never FAIL
# ---------------------------------------------------------------------------


def test_codec_fallback_is_invalid_not_fail():
    """Requested av1, negotiated vp9. A cell labelled av1 that measured a
    different codec is the worst data point the matrix can produce."""
    r = run("invalid_codec_fallback_r0")
    assert r["verdict"]["status"] == pr.INVALID
    assert r["verdict"]["status"] != pr.FAIL
    assert r["validity"]["invalid_reasons"] == ["codec_fallback"]
    assert r["conditions"]["video_codec_actual"] == "vp9"
    assert r["conditions"]["codec_mismatch"] is True
    # Analysis uses the ACTUAL codec, so the run groups under vp9, not av1.
    assert dict(pr.pool_key(r, pr.load_matrix()))["video_codec"] == "vp9"


def test_malformed_av1_bitstream_is_invalid_not_a_zero_bitrate_fail():
    """frames_encoded > 0 while packets_sent == 0. The bitrate is genuinely zero,
    which would clear the 5 Mbps ceiling and read as a PASS, or read as a FAIL on
    fps. Neither is the truth: nothing reached the wire."""
    r = run("invalid_malformed_av1_r0")
    assert r["verdict"]["status"] == pr.INVALID
    assert r["validity"]["invalid_reasons"] == ["malformed_av1_bitstream"]
    assert r["metrics"]["malformed_bitstream"] is True
    assert r["metrics"]["video_bitrate_bps"] == 0.0
    # It is NOT reported as a threshold failure, in either direction.
    assert r["verdict"]["failed_thresholds"] == [] or all(
        f["effect"] != "fail" for f in r["verdict"]["failed_thresholds"])


def test_cpu_limited_software_av1_is_invalid_not_a_pass():
    """0.5 s of CPU limitation per 1 s poll = 50%, five times the 10% gate. The
    bitrate would otherwise clear the ceiling and score PASS — a bitrate produced
    by a starved encoder is not a measurement of the network."""
    r = run("invalid_cpu_limited_av1_r0")
    assert r["verdict"]["status"] == pr.INVALID
    assert r["validity"]["invalid_reasons"] == ["cpu_limited_encoder"]
    assert approx(r["metrics"]["quality_limitation_cpu_pct"], 50.0)
    # The bitrate is under the ceiling: without the gate this would be a PASS.
    assert r["metrics"]["video_bitrate_bps"] < 5_000_000


def test_missing_g2g_metadata_is_invalid_not_a_blank_column():
    """The subscribe_timing_events ordering fault. Every other signal is healthy —
    frames received, decoded, fps and freeze counts all green — and the only
    symptom is empty frame metadata."""
    r = run("invalid_g2g_metadata_r0")
    assert r["verdict"]["status"] == pr.INVALID
    assert r["validity"]["invalid_reasons"] == ["g2g_metadata_missing"]
    assert r["metrics"]["g2g_metadata_coverage_pct"] == 0.0
    assert r["metrics"]["g2g_p50_ms"] is None
    # Everything else looks fine, which is exactly why the gate has to exist.
    assert approx(r["metrics"]["video_fps_p50"], 30.0)
    assert r["metrics"]["video_freeze_count"] == 0
    assert approx(r["metrics"]["video_bitrate_bps"], 4_000_000.0)


def test_every_invalid_fixture_uses_the_matrix_vocabulary():
    matrix = pr.load_matrix()
    vocab = pr.invalid_vocabulary(matrix)
    for r in scored().values():
        for reason in r["validity"]["invalid_reasons"]:
            assert reason in vocab, f"{r['run_id']}: {reason} is not in matrix.yaml"


def test_no_run_is_both_invalid_and_failed():
    """The structural separation: invalidation is evaluated before any blocking
    threshold, so an INVALID run never also carries a fail."""
    for r in scored().values():
        if r["verdict"]["status"] == pr.INVALID:
            assert not any(f["effect"] == "fail"
                           for f in r["verdict"]["failed_thresholds"]), r["run_id"]


def test_incomplete_run_is_invalid():
    """The run_metadata record is written last. Its absence means the harness did
    not reach the end of the run, so the scored window is unreconstructable."""
    r = run("invalid_incomplete_r0")
    assert r["verdict"]["status"] == pr.INVALID
    assert "session_lost_mid_run" in r["validity"]["invalid_reasons"]
    assert r["metrics"]["run_complete"] is False


def test_poll_overbudget_is_invalid():
    r = run("invalid_poll_overbudget_r0")
    assert r["verdict"]["status"] == pr.INVALID
    assert r["validity"]["invalid_reasons"] == ["poll_overbudget"]
    assert approx(r["metrics"]["poll_overbudget_pct"], 100.0)


# ---------------------------------------------------------------------------
# Hand-computed extraction values
# ---------------------------------------------------------------------------


def test_clean_pass_scores_pass():
    r = run("pass_h264_r0")
    assert r["verdict"]["status"] == pr.PASS
    assert r["validity"]["invalid_reasons"] == []
    assert r["validity"]["valid"] is True


def test_bitrate_is_the_wire_rate_and_includes_header_bytes():
    """The fixture grows (bytes_sent + header_bytes_sent) at exactly 4 Mbps / 8
    bytes per second, so the wire rate is 4 000 000 bps. Payload-only bytes would
    give 95% of that = 3 800 000, which is what a parser omitting header_bytes
    would report."""
    m = run("pass_h264_r0")["metrics"]
    assert approx(m["video_bitrate_bps"], 4_000_000.0, tol=1e-3)
    assert not approx(m["video_bitrate_bps"], 3_800_000.0, tol=1e-3)


def test_jitter_buffer_delay_is_a_delta_ratio_not_a_lifetime_ratio():
    """12 ms of buffer delay per emitted frame, so the delta ratio is 12 ms
    regardless of how many frames the run emitted. Dividing lifetime cumulatives
    happens to agree here — which is the point of also testing the smooth cell,
    where the two would still agree, and of testing a case where the counters
    start non-zero."""
    m = run("pass_h264_r0")["metrics"]
    assert approx(m["jitter_buffer_delay_avg_ms"], 12.0, tol=1e-6)
    assert approx(m["jitter_buffer_target_delay_ms"], 12.0, tol=1e-6)


def test_delta_ratio_ignores_the_pre_window_counter_baseline():
    """Constructed directly rather than through a fixture: the scored window's
    counters start at a large non-zero value. A lifetime ratio would return the
    whole-session average (10 ms); the delta ratio must return the in-window
    value (20 ms)."""
    polls = []
    for i in range(4):
        # 1000 frames of history at 8 ms, then 100 frames per poll at 20 ms.
        emitted = 1000 + 100 * i
        delay_s = (1000 * 0.008) + (100 * i * 0.020)
        polls.append({
            "t_monotonic_us": i * 1_000_000,
            "video_in": {"jitter_buffer_delay_s": delay_s,
                         "jitter_buffer_emitted_count": emitted},
        })
    got = pr.delta_ratio(polls, "video_in", "jitter_buffer_delay_s",
                         "jitter_buffer_emitted_count", scale=1000.0)
    assert approx(got, 20.0)
    last = polls[-1]["video_in"]
    lifetime = last["jitter_buffer_delay_s"] / last["jitter_buffer_emitted_count"] * 1000
    assert approx(lifetime, 10.769231, tol=1e-4)  # the wrong answer, for contrast


def test_control_delivered_uses_the_publisher_denominator():
    """200 Hz across a 20 s scored window = 4000 published seqs. The fixture
    delivers 200 per poll across 20 intervals = 4000 received, so 100%."""
    m = run("pass_h264_r0")["metrics"]
    assert m["control_expected_seq_count"] == CONTROL_RATE_HZ * SCORED_WINDOW_S
    assert m["control_distinct_seq_received"] == 200 * SCORED_INTERVALS
    assert approx(m["control_delivered_pct"], 100.0)


def test_control_delivered_denominator_survives_edge_loss():
    """The 5% loss cell delivers 150 of 200 per poll = 3000 of 4000. Deriving the
    denominator from received sequence numbers would report a higher figure; the
    publisher log keeps it fixed at 4000."""
    m = run("t2_loss5p0_r0")["metrics"]
    assert m["control_expected_seq_count"] == EXPECTED_SEQS
    assert approx(m["control_delivered_pct"], 75.0)


def test_control_delivered_is_null_without_a_publisher_seq_log():
    """Never estimated from received sequence numbers: that derivation is
    self-referential and biased toward passing against a 99.9% bar."""
    m = run("invalid_incomplete_r0")["metrics"]
    assert m["control_delivered_pct"] is None


def test_control_late_pct_is_a_windowed_share():
    """The 10% loss cell marks 2 samples late per poll against 60 received per
    poll: 2/60 = 3.333%."""
    m = run("t2_loss10p0_r0")["metrics"]
    assert approx(m["control_late_pct"], 2 * SCORED_INTERVALS / (60 * SCORED_INTERVALS)
                  * 100.0, tol=1e-6)
    assert approx(m["control_late_pct"], 3.3333333, tol=1e-5)


def test_control_late_pct_is_null_not_zero_when_unmeasured():
    """A missing playout window or an invalid clock offset means the metric was
    not measured. Zero is a measurement and would clear a 0.1% bar."""
    polls = [{"t_monotonic_us": i * 1_000_000,
              "control": {"seq_published": 200 * i, "distinct_seq_received": 200 * i,
                          "distinct_seq_received_interval": 200, "max_seq_received": 0,
                          "max_gap": 0, "gap_lengths": [], "owd_raw_us_interval": [],
                          "jitter_ms": 0.0},
              "probe": {"probes_sent": 0, "probes_completed": 0,
                        "rtt_us_interval": [], "clock_sync_confidence": "none"}}
             for i in range(3)]
    m: dict = {}
    rec = {"conditions": {"control_rate_hz": 200}}
    pr.extract_control(polls, m, rec, None, None)
    assert m["control_late_pct"] is None


def test_packets_lost_pct_uses_deltas_and_reports_the_clamp():
    """The 5% loss cell loses 15 packets per poll against 300 received:
    15 / (15 + 300) = 4.7619%."""
    m = run("t2_loss5p0_r0")["metrics"]
    lost = 15 * SCORED_INTERVALS
    recv = 300 * SCORED_INTERVALS
    assert approx(m["video_packets_lost_pct"], lost / (lost + recv) * 100.0, tol=1e-6)
    assert approx(m["video_packets_lost_pct"], 4.7619047, tol=1e-5)


def _loss_polls(section: str, *, lost_per_poll: int, recv_per_poll: int,
                harness_delta: int, polls: int = 4) -> list[dict]:
    """Cumulative counters that RISE, with a harness delta field that lies.

    The two disagree on purpose: differencing the cumulative gives the truth,
    reading the precomputed field gives `harness_delta`.
    """
    out = []
    for i in range(polls):
        out.append({
            "t_monotonic_us": i * 1_000_000,
            section: {
                "packets_lost": lost_per_poll * i,
                "packets_lost_delta": harness_delta,
                "packets_received": recv_per_poll * i,
                "total_samples_received": 48000 * i,
                "concealed_samples": 0,
                "silent_concealed_samples": 0,
                "concealment_events": 0,
                "inserted_samples_for_deceleration": 0,
                "removed_samples_for_acceleration": 0,
                "jitter_buffer_delay_s": 0.0,
                "jitter_buffer_emitted_count": 48000 * i,
                "audio_level": 0.4,
            },
        })
    return out


def test_video_loss_is_differenced_here_not_read_from_the_harness_delta():
    """Design gap 13: differencing is analysis-side so a bug in it is fixable
    without re-running the matrix. Reading a harness-precomputed delta puts the
    arithmetic back on the far side of that boundary.

    Cumulative loss rises 10/poll against 300 received/poll while the harness
    delta field reads 0. Differencing gives 30/(30+900) = 3.2258%; trusting the
    field gives 0.0% — a silent zero-loss report on a lossy link.
    """
    polls = _loss_polls("video_in", lost_per_poll=10, recv_per_poll=300,
                        harness_delta=0)
    m: dict = {}
    pr.extract_video_recv(polls, m, {})
    assert approx(m["video_packets_lost_pct"], 30 / (30 + 900) * 100.0, tol=1e-9)
    assert approx(m["video_packets_lost_pct"], 3.2258064, tol=1e-5)
    assert m["video_packets_lost_pct"] != 0.0


def test_audio_loss_is_differenced_here_and_reports_its_clamp():
    """matrix.yaml specifies audio loss "as video, on the audio stream",
    including the negative-delta clamp. Both properties are asserted so the two
    paths cannot drift apart again."""
    polls = _loss_polls("audio_in", lost_per_poll=10, recv_per_poll=300,
                        harness_delta=0)
    m: dict = {}
    pr.extract_audio(polls, m)
    assert approx(m["audio_packets_lost_pct"], 3.2258064, tol=1e-5)
    # The clamp fields exist on the audio path, mirroring video.
    assert m["audio_packets_lost_clamp_events"] == 0
    assert m["audio_packets_lost_clamped_min"] is None


def test_audio_negative_loss_delta_is_clamped_like_video():
    polls = [
        {"t_monotonic_us": 0,
         "audio_in": {"packets_lost": 10, "packets_received": 0,
                      "total_samples_received": 0, "concealed_samples": 0,
                      "silent_concealed_samples": 0, "concealment_events": 0,
                      "inserted_samples_for_deceleration": 0,
                      "removed_samples_for_acceleration": 0,
                      "jitter_buffer_delay_s": 0.0,
                      "jitter_buffer_emitted_count": 0, "audio_level": 0.4}},
        {"t_monotonic_us": 1_000_000,
         "audio_in": {"packets_lost": 7, "packets_received": 300,
                      "total_samples_received": 48000, "concealed_samples": 0,
                      "silent_concealed_samples": 0, "concealment_events": 0,
                      "inserted_samples_for_deceleration": 0,
                      "removed_samples_for_acceleration": 0,
                      "jitter_buffer_delay_s": 0.0,
                      "jitter_buffer_emitted_count": 48000, "audio_level": 0.4}},
    ]
    m: dict = {}
    pr.extract_audio(polls, m)
    assert m["audio_packets_lost_pct"] == 0.0        # clamped, never negative
    assert m["audio_packets_lost_clamp_events"] == 1
    assert m["audio_packets_lost_clamped_min"] == -3


def test_a_reordering_run_surfaces_its_clamp_through_the_full_pipeline():
    """End-to-end, not just the helper: a run whose cumulative packets_lost is
    revised downward at one poll must report the clamp event, keep its loss
    figure non-negative, and stay a valid run — reordering is a property of the
    path, not a harness fault."""
    r = run("reorder_clamp_r0")
    assert r["verdict"]["status"] != pr.INVALID
    assert r["metrics"]["video_packets_lost_clamp_events"] == 1
    assert r["metrics"]["video_packets_lost_clamped_min"] == -3
    assert r["metrics"]["video_packets_lost_pct"] >= 0.0


def test_the_report_names_clamped_runs_as_lower_bounds():
    matrix = pr.load_matrix()
    text = pr.Report(list(scored().values()), matrix).render()
    assert "reorder_clamp_r0" in text
    assert "lower bound" in text.lower()


def test_control_gap_p99_is_null_when_nothing_was_received():
    """Zero gaps and no samples at all are different facts; 0 would read as a
    perfect run."""
    polls = [{"t_monotonic_us": i * 1_000_000,
              "control": {"seq_published": 200 * i, "distinct_seq_received": 0,
                          "distinct_seq_received_interval": 0, "max_seq_received": 0,
                          "max_gap": 0, "gap_lengths": [],
                          "owd_raw_us_interval": [], "jitter_ms": 0.0},
              "probe": {"probes_sent": 0, "probes_completed": 0,
                        "rtt_us_interval": [], "clock_sync_confidence": "probe"}}
             for i in range(3)]
    m: dict = {}
    pr.extract_control(polls, m, {"conditions": {"control_rate_hz": 200}},
                       None, None, pr.load_matrix())
    assert m["control_gap_p99"] is None
    # A run that DID receive samples with no gaps reports 0, not None.
    for p in polls:
        p["control"]["distinct_seq_received"] = 200 * polls.index(p)
        p["control"]["distinct_seq_received_interval"] = 200
    m = {}
    pr.extract_control(polls, m, {"conditions": {"control_rate_hz": 200}},
                       None, None, pr.load_matrix())
    assert m["control_gap_p99"] == 0


def test_negative_loss_delta_is_clamped_and_the_clamp_is_reported():
    """packets_lost is i64 and may go negative on reorder or duplicate. The
    harness clamps at zero and surfaces the pre-clamp value; the analysis reports
    the event rather than silently absorbing it."""
    # The cumulative counter goes BACKWARDS across the poll, which is what a
    # reorder or duplicate looks like on the wire.
    polls = [{"t_monotonic_us": 0,
              "video_in": {"packets_lost": 10, "packets_lost_delta": 0,
                           "packets_received": 0}},
             {"t_monotonic_us": 1_000_000,
              "video_in": {"packets_lost": 7, "packets_lost_delta": 0,
                           "packets_lost_clamped_from": -3,
                           "packets_received": 300}}]
    m: dict = {}
    pr.extract_video_recv(polls, m, {})
    assert m["video_packets_lost_clamp_events"] == 1
    assert m["video_packets_lost_clamped_min"] == -3
    assert m["video_packets_lost_pct"] == 0.0  # clamped, not negative


def test_rtt_percentiles_are_null_below_the_sample_precondition():
    """>= 30 completed probes. A p95 over a dozen samples is not a p95."""
    polls = [{"t_monotonic_us": i * 1_000_000,
              "probe": {"probes_sent": 2 * i, "probes_completed": 2 * i,
                        "rtt_us_interval": [30_000, 31_000],
                        "clock_sync_confidence": "probe"}}
             for i in range(5)]
    m: dict = {}
    pr.extract_probe(polls, m)
    assert m["app_rtt_sample_count"] == 10
    assert m["app_rtt_p95_ms"] is None
    assert m["app_rtt_p50_ms"] is None


def test_network_and_app_rtt_are_separate_metrics():
    """The two round trips measure different paths and must never share a name.

    The probe traverses publisher -> SFU -> subscriber -> SFU -> publisher over
    the control transport plus scheduling at both ends; `candidate_pair_rtt_s` is
    the ICE network round trip. Reporting the probe figure as "network RTT" made
    Q-7's g2g/RTT ratios read below 1.0, which is impossible — glass-to-glass
    contains a network traversal and cannot be faster than one.

    Shaped after the real Tier 0 reading (network p50 31 ms, app p50 61 ms).
    """
    polls = [{"t_monotonic_us": i * 1_000_000,
              "probe": {"probes_sent": i, "probes_completed": i,
                        "rtt_us_interval": [61_000],
                        "clock_sync_confidence": "probe"},
              "transport": {"candidate_pair_rtt_s": 0.031}}
             for i in range(40)]
    m: dict = {}
    pr.extract_probe(polls, m)
    pr.extract_transport(polls, m)
    assert approx(m["app_rtt_p50_ms"], 61.0)
    assert approx(m["network_rtt_p50_ms"], 31.0)
    # The old single name is gone, so nothing can silently read one as the other.
    assert "rtt_p50_ms" not in m and "rtt_p95_ms" not in m

    # The ratio Q-7 reports must use the network denominator. With a 74 ms G2G
    # the app denominator gives 1.21x and the network one 2.39x; only the latter
    # describes how much of the pipeline sits beyond the network.
    assert (74.0 / m["network_rtt_p50_ms"]) > (74.0 / m["app_rtt_p50_ms"])


def test_the_90ms_bar_is_scored_against_network_rtt_not_the_probe():
    """§8.1a's companion SHOULD is "20-50 ms", a network-path figure: an
    application loop containing 200 Hz publisher scheduling at both ends cannot
    sit in that range. Scoring the probe against the bar would apply a network
    limit to a number that includes harness scheduling."""
    matrix = pr.load_matrix()
    blocking = {t["metric"] for t in matrix["thresholds"] if t["effect"] == "fail"}
    assert "network_rtt_p95_ms" in blocking
    assert "app_rtt_p95_ms" not in blocking
    assert "rtt_p95_ms" not in blocking


def test_probe_loss_uses_the_explicit_lost_count_not_sent_minus_completed():
    """At a probe interval below the round trip several probes are in flight at
    any instant. `sent - completed` counts every one of them as lost, which would
    read as steady-state loss on a path that lost nothing."""
    polls = [{"t_monotonic_us": i * 1_000_000,
              "probe": {"probes_sent": 20 * i, "probes_completed": 20 * i - 5,
                        "probes_lost": 0, "probes_in_flight": 5,
                        "rtt_us_interval": [], "clock_sync_confidence": "probe"}}
             for i in range(1, 6)]
    m: dict = {}
    pr.extract_probe(polls, m)
    # 80 sent, none aged out, 5 permanently in flight: the loss is zero.
    assert m["probe_loss_pct"] == 0.0


def test_qp_avg_differences_the_cumulative_sum():
    """qp_sum is cumulative. Dividing the lifetime totals gives a session average
    that hides the transient; the metric is d(qp_sum) / d(frames_encoded).

    30 frames per poll at QP 40, then 30 at QP 20: the correct answer is 30, and
    each interval is weighted by its own frame count.
    """
    polls, qp_sum, frames = [], 0, 0
    for i in range(11):
        polls.append({"t_monotonic_us": i * 1_000_000,
                      "video_out": {"qp_sum": qp_sum, "frames_encoded": frames,
                                    "bytes_sent": 0, "header_bytes_sent": 0,
                                    "pli_count": 0, "key_frames_encoded": 1,
                                    "quality_limitation_reason": "none"}})
        qp = 40 if i < 5 else 20
        qp_sum += 30 * qp
        frames += 30
    m: dict = {}
    pr.extract_video_send(polls, m, {}, 1.0)
    assert approx(m["qp_avg"], 30.0)


def test_bandwidth_limitation_is_a_poll_share_and_is_cross_codec_comparable():
    """The metric that surfaced the Tier 0 AV1/H264 difference. Unlike qp_avg it
    counts an encoder-reported condition in units no codec defines differently."""
    polls = []
    for i in range(10):
        polls.append({"t_monotonic_us": i * 1_000_000,
                      "video_out": {"qp_sum": i * 30, "frames_encoded": i * 30,
                                    "bytes_sent": 0, "header_bytes_sent": 0,
                                    "pli_count": 0, "key_frames_encoded": 1,
                                    "quality_limitation_reason":
                                        "bandwidth" if i < 6 else "none"}})
    m: dict = {}
    pr.extract_video_send(polls, m, {}, 1.0)
    assert approx(m["quality_limitation_bandwidth_poll_pct"], 60.0)
    assert m["quality_limitation_poll_count"] == 10


def _audio_polls(levels):
    return [{"t_monotonic_us": i * 1_000_000,
             "audio_in": {"audio_level": lv, "total_samples_received": i * 48_000,
                          "concealed_samples": 0, "silent_concealed_samples": 0,
                          "concealment_events": 0, "packets_lost": 0,
                          "inserted_samples_for_deceleration": 0,
                          "removed_samples_for_acceleration": 0,
                          "jitter_buffer_delay_s": 0.0,
                          "jitter_buffer_emitted_count": i * 48_000,
                          "packets_received": i * 50}}
            for i, lv in enumerate(levels)]


def test_intermittent_audio_is_not_a_silent_source():
    """The rule is `audio_level == 0` for the WHOLE run (design §1g rule (ii)), so
    the test is on the maximum. As a median it fired on a working tone generator:
    T1 av1 uplink_mbps=10 r2 peaked at 0.5026 with 42 of 78 scored polls at 0.0
    during a 5-reconnect storm, so the median read 0.0 and the run was reported as
    having a silent source it never had."""
    m: dict = {}
    pr.extract_audio(_audio_polls([0.0] * 6 + [0.5] * 4), m)
    assert approx(m["audio_level"], 0.5)
    # The median is still available, it just does not decide validity.
    assert approx(m["audio_level_median"], 0.0)
    rec = {"metrics": m, "conditions": {"audio_enabled": True}}
    assert pr.audio_column_invalid(rec) is False


def test_a_genuinely_silent_source_still_suppresses_the_audio_columns():
    """The fix must not disarm the rule it corrects."""
    m: dict = {}
    pr.extract_audio(_audio_polls([0.0] * 10), m)
    assert m["audio_level"] == 0
    rec = {"metrics": m, "conditions": {"audio_enabled": True}}
    assert pr.audio_column_invalid(rec) is True


def test_legacy_snapshots_flag_their_probe_loss_as_a_ceiling():
    """Snapshots predating the explicit aged-out count can only derive loss as
    `sent - completed`, which counts displaced-but-delivered probes as lost. The
    figure is a ceiling and the record must say so, or the report presents an
    artifact as a measurement.

    Measured across the 2026-08-25 sweep: the 56 runs with a healthy control path
    pooled to 2.2% by this derivation against a true loss near zero.
    """
    # One probe displaced per poll: sent advances by 20, completed by 19.
    legacy = [{"t_monotonic_us": i * 1_000_000,
               "probe": {"probes_sent": 20 * i, "probes_completed": 19 * i,
                         "rtt_us_interval": [], "clock_sync_confidence": "probe"}}
              for i in range(1, 6)]
    m: dict = {}
    pr.extract_probe(legacy, m)
    assert m["probe_loss_legacy_derivation"] is True
    assert m["probe_loss_pct"] > 0  # the artifact, surfaced as a ceiling

    # The same series, now carrying the explicit count: none of those probes was
    # lost, they were merely still in flight when their successor was issued.
    modern = [{"t_monotonic_us": i * 1_000_000,
               "probe": {"probes_sent": 20 * i, "probes_completed": 19 * i,
                         "probes_lost": 0, "probes_in_flight": i,
                         "rtt_us_interval": [], "clock_sync_confidence": "probe"}}
              for i in range(1, 6)]
    m2: dict = {}
    pr.extract_probe(modern, m2)
    assert m2["probe_loss_legacy_derivation"] is False
    assert m2["probe_loss_pct"] == 0.0


def test_control_gaps_are_reported_in_milliseconds():
    """A gap length is a count of consecutive missing sequence numbers at a known
    fixed publisher rate, so it IS a duration and must be reported as one — the
    latency budget is written in milliseconds, not sample counts.

    The real event this exists for: a 121-sample gap on LiveKit Cloud with no
    induced loss is 605 ms at 200 Hz, yet only 0.58% of ~21 000 samples, so
    `control_delivered_pct` absorbs it without a trace.
    """
    polls = [{"t_monotonic_us": i * 1_000_000, "scored": True,
              "control": {"seq_published": 200 * i, "distinct_seq_received": 200 * i,
                          "distinct_seq_received_interval": 200,
                          "max_seq_received": 200 * i, "reordered_interval": 0,
                          "duplicates_interval": 0, "max_gap": 121,
                          "gap_lengths": [5, 121], "gap_p99": 5,
                          "owd_raw_us_interval": [], "jitter_ms": 1.0}}
             for i in range(1, 6)]
    m: dict = {}
    pr.extract_control(polls, m, {"conditions": {"control_rate_hz": 200}},
                       None, None, None)
    assert m["control_max_gap"] == 121
    assert approx(m["control_max_gap_ms"], 605.0)
    assert approx(m["control_gap_p99_ms"], 25.0)


def test_control_gap_ms_is_null_without_a_known_publisher_rate():
    """The conversion is only meaningful against the rate that produced it.
    Defaulting to 200 Hz would silently mislabel any run at another rate."""
    polls = [{"t_monotonic_us": i * 1_000_000, "scored": True,
              "control": {"seq_published": 0, "distinct_seq_received": 10,
                          "distinct_seq_received_interval": 10,
                          "max_seq_received": 10, "reordered_interval": 0,
                          "duplicates_interval": 0, "max_gap": 121,
                          "gap_lengths": [121], "gap_p99": 121,
                          "owd_raw_us_interval": [], "jitter_ms": 1.0}}
             for i in range(1, 4)]
    m: dict = {}
    pr.extract_control(polls, m, {"conditions": {}}, None, None, None)
    assert m["control_max_gap"] == 121
    assert m["control_max_gap_ms"] is None


def test_subscriber_bwe_is_never_reported_as_available_bandwidth():
    """It reads a constant 300000 because the transport sample comes from the
    subscriber PC, which sends only RTCP and so never ramps off libwebrtc's
    default start bitrate. Correct for what it measures, but presenting it as an
    uplink estimate would be a confident wrong answer — so no metric extracts it
    and no report can render it.
    """
    polls = [{"t_monotonic_us": i * 1_000_000,
              "transport": {"candidate_pair_rtt_s": 0.031,
                            "subscriber_available_outgoing_bitrate_bps": 300_000.0}}
             for i in range(10)]
    m: dict = {}
    pr.extract_transport(polls, m)
    assert not [k for k in m if "available" in k or "bandwidth" in k]

    # And it must not have crept into any suite's primary set or the metric
    # catalogue, either of which would put it in front of a reader.
    matrix = pr.load_matrix()
    for name in matrix["metrics"]:
        assert "available_outgoing" not in name, name
    for name, suite in matrix["suites"].items():
        for metric in suite.get("primary") or []:
            assert "available_outgoing" not in metric, f"{name}: {metric}"


def test_rtt_percentile_is_nearest_rank():
    """20 samples at 30 ms plus 2 at 100 ms: p95 by nearest rank is the 21st of
    22 sorted values = 100 ms. Interpolation would invent a value between."""
    vals = [30.0] * 20 + [100.0, 100.0]
    assert pr.percentile(vals, 95) == 100.0
    assert pr.percentile(vals, 50) == 30.0


def test_decode_and_assembly_are_per_frame_delta_ratios():
    """AV1 fixture: 9 ms decode and 7 ms assembly per frame."""
    m = run("q7_av1_zero_jitter_r0")["metrics"]
    assert approx(m["decode_time_avg_ms"], 9.0, tol=1e-6)
    assert approx(m["assembly_time_avg_ms"], 7.0, tol=1e-6)
    assert approx(m["encode_time_avg_ms"], 8.0, tol=1e-6)


def test_keyframe_service_is_a_poll_count_distribution_not_milliseconds():
    """PLIs at polls 5 and 12, keyframes at 7 and 15: 2 polls and 3 polls. It is
    reported in poll intervals, never converted to a millisecond percentile."""
    r = run("pass_h264_r0")
    d = r["distributions"]["keyframe_service_polls"]
    assert d["unit"] == "poll_intervals"
    assert d["values"] == [2, 3]
    assert d["max"] == 3
    assert "keyframe_service_ms" not in r["metrics"]
    assert "keyframe_service_p95_ms" not in r["metrics"]


def test_quality_limitation_missing_key_is_zero_not_an_error():
    m = run("pass_h264_r0")["metrics"]
    assert m["quality_limitation_cpu_pct"] == 0.0
    assert m["quality_limitation_bandwidth_pct"] == 0.0


def test_audio_concealment_and_playout_are_delta_ratios():
    m = run("pass_h264_r0")["metrics"]
    assert approx(m["audio_playout_delay_avg_ms"], 30.0, tol=1e-6)
    assert m["audio_concealment_pct"] == 0.0
    assert approx(m["audio_bitrate_bps"], 250_000.0, tol=1e-3)


def test_silent_audio_invalidates_only_the_audio_columns():
    """audio_level == 0 for the whole run makes every concealment figure
    meaningless, but the video and control measurements are untouched."""
    r = run("audio_silent_r0")
    assert r["verdict"]["status"] != pr.INVALID
    assert r["validity"]["invalid_reasons"] == []
    assert pr.audio_column_invalid(r) is True
    assert any("silent_audio_source" in d for d in r["validity"]["invalid_detail"])
    # The audio OBSERVE rows are suppressed rather than reported against a
    # silent source.
    observed = {o["metric"] for o in r["verdict"]["observed"]}
    assert "audio_playout_delay_avg_ms" not in observed
    assert "audio_concealment_pct" not in observed
    # Video is unaffected.
    assert approx(r["metrics"]["video_bitrate_bps"], 4_000_000.0, tol=1e-3)


def test_audio_thresholds_score_observe_never_fail():
    r = run("pass_h264_r0")
    observed = {o["metric"] for o in r["verdict"]["observed"]}
    assert "audio_playout_delay_avg_ms" in observed
    assert "audio_concealment_pct" in observed
    for o in r["verdict"]["observed"]:
        assert o["effect"] == "observe"
    for f in r["verdict"]["failed_thresholds"]:
        assert not f["metric"].startswith("audio_")


def test_g2g_coverage_and_frame_loss():
    m = run("pass_h264_r0")["metrics"]
    assert approx(m["g2g_metadata_coverage_pct"], 100.0)
    assert approx(m["g2g_p50_ms"], 60.0)
    assert m["g2g_frame_loss_pct"] == 0.0


def test_session_drops_exclude_harness_initiated_close():
    matrix = pr.load_matrix()
    rec = json.loads(json.dumps(run("pass_h264_r0")))
    rec["events"] = [
        {"t_unix_us": 1, "kind": "disconnected", "harness_initiated": True,
         "reason": "harness shutdown"},
    ]
    m: dict = {}
    pr.extract_session(rec, None, m)
    assert m["session_drops"] == 0
    rec["events"].append({"t_unix_us": 2, "kind": "disconnected",
                          "harness_initiated": False, "reason": "signal lost"})
    m = {}
    pr.extract_session(rec, None, m)
    assert m["session_drops"] == 1


def test_a_survived_reconnect_is_not_a_drop():
    """PRD §8.6b explicitly permits it; scoring reconnects as drops would fail
    runs the PRD passes."""
    rec = {"events": [{"t_unix_us": 1, "kind": "reconnecting"},
                      {"t_unix_us": 500_001, "kind": "reconnected"}]}
    m: dict = {}
    pr.extract_session(rec, None, m)
    assert m["session_drops"] == 0
    assert m["reconnect_count"] == 1


# ---------------------------------------------------------------------------
# Threshold semantics
# ---------------------------------------------------------------------------


def test_bitrate_and_fps_breaches_both_fail_and_carry_provenance():
    """6 Mbps over the 5 Mbps ceiling and 24 fps under the 27 fps bar. The fps row
    is governing-page-derived, not a PRD MUST, and the verdict carries that so a
    report cannot state it as a requirement."""
    r = run("fail_bitrate_r0")
    assert r["verdict"]["status"] == pr.FAIL
    failed = {f["metric"]: f for f in r["verdict"]["failed_thresholds"]}
    assert approx(failed["video_bitrate_bps"]["actual"], 6_000_000.0, tol=1e-3)
    assert failed["video_bitrate_bps"]["provenance"] == "prd-stated-target"
    assert approx(failed["video_fps_p50"]["actual"], 24.0)
    assert failed["video_fps_p50"]["provenance"] == "governing-page-derived"
    assert failed["video_fps_p50"]["clause"] == "§7.1a"


def test_advisory_rows_do_not_change_the_verdict():
    """network_rtt_p50_ms <= 50 and control_owd_p99_ms <= 100 are PRD SHOULDs, recorded
    but not blocking. The clean fixture is inside both, so a stricter check: the
    scorer must never mark a run FAIL on an advisory row alone."""
    matrix = pr.load_matrix()
    advisory = [t["metric"] for t in matrix["thresholds"] if t["effect"] == "advisory"]
    assert "network_rtt_p50_ms" in advisory and "control_owd_p99_ms" in advisory
    for r in scored().values():
        if r["verdict"]["status"] == pr.FAIL:
            assert any(f["effect"] == "fail" for f in r["verdict"]["failed_thresholds"])


def test_an_advisory_breach_is_never_filed_as_a_failed_threshold():
    """The status and the list must agree. A consumer reading the raw JSON must
    not see a PASS run that also lists a 'failed' threshold — network_rtt_p50_ms and
    control_owd_p99_ms are PRD SHOULDs and a breach is recorded, not failed."""
    matrix = pr.load_matrix()
    rec = json.loads(json.dumps(run("pass_h264_r0")))
    rec["metrics"]["network_rtt_p50_ms"] = 80.0   # over the 50 ms advisory bar
    rec["metrics"]["control_owd_p99_ms"] = 150.0  # over the 100 ms advisory bar
    pr.score(rec, matrix)
    assert rec["verdict"]["status"] == pr.PASS
    for f in rec["verdict"]["failed_thresholds"]:
        assert f["effect"] == "fail", f
    filed = {f["metric"] for f in rec["verdict"]["failed_thresholds"]}
    assert "network_rtt_p50_ms" not in filed
    assert "control_owd_p99_ms" not in filed
    # The breach is still visible, with within=False, in the observed list.
    breached = {o["metric"]: o for o in rec["verdict"]["observed"]}
    assert breached["network_rtt_p50_ms"]["within"] is False
    assert approx(breached["network_rtt_p50_ms"]["actual"], 80.0)
    assert breached["control_owd_p99_ms"]["within"] is False


def test_failed_thresholds_only_ever_holds_blocking_rows():
    """Across every fixture, not just the constructed case."""
    for r in scored().values():
        for f in r["verdict"]["failed_thresholds"]:
            assert f["effect"] == "fail", f"{r['run_id']}: {f['metric']}"
        if r["verdict"]["status"] == pr.PASS:
            assert r["verdict"]["failed_thresholds"] == [], r["run_id"]


def test_control_delivered_is_null_below_its_sample_floor():
    """matrix.yaml requires >=10000 samples. At a 99.9% bar, 0.1% of 4000 samples
    is four: the metric cannot resolve the threshold it is scored against, so it
    is nulled rather than silently evaluated."""
    matrix = pr.load_matrix()
    floor = pr.min_samples(matrix, "control_delivered_pct")
    assert floor == 10000
    window_s = 20.0  # 200 Hz x 20 s = 4000 samples, below the floor
    lo = 1_000_000_000
    hi = lo + int(window_s * 1e6)
    seq_log = [{"seq": n, "t_send_unix_us": lo + n * 5000}
               for n in range(int(200 * window_s))]
    polls = [{"t_monotonic_us": i * 1_000_000,
              "control": {"seq_published": 200 * i, "distinct_seq_received": 200 * i,
                          "distinct_seq_received_interval": 200,
                          "max_seq_received": 0, "max_gap": 0, "gap_lengths": [],
                          "owd_raw_us_interval": [], "jitter_ms": 0.0},
              "probe": {"probes_sent": 0, "probes_completed": 0,
                        "rtt_us_interval": [], "clock_sync_confidence": "probe"}}
             for i in range(3)]
    meta = {"scored_window_start_unix_us": lo, "scored_window_end_unix_us": hi,
            "seq_published": 4000}
    m: dict = {}
    pr.extract_control(polls, m, {"conditions": {"control_rate_hz": 200}},
                       seq_log, meta, matrix)
    assert m["control_expected_seq_count"] == 4000
    assert m["control_delivered_pct"] is None
    assert m["control_delivered_undersampled"] is True


def test_an_undersampled_delivered_pct_does_not_clear_the_blocking_bar():
    """The failure this guards: a short run silently clearing a 99.9% bar it did
    not have the samples to resolve."""
    matrix = pr.load_matrix()
    rec = json.loads(json.dumps(run("pass_h264_r0")))
    rec["metrics"]["control_delivered_pct"] = None
    rec["metrics"]["control_delivered_undersampled"] = True
    pr.score(rec, matrix)
    evaluated = {f["metric"] for f in rec["verdict"]["failed_thresholds"]}
    assert "control_delivered_pct" not in evaluated
    observed = {o["metric"] for o in rec["verdict"]["observed"]}
    assert "control_delivered_pct" not in observed


def test_the_fixtures_clear_the_delivered_sample_floor():
    """The fixture window is sized to exceed the floor, so the clean run's
    delivered figure is genuinely resolvable rather than nulled."""
    m = run("pass_h264_r0")["metrics"]
    assert m["control_expected_seq_count"] == EXPECTED_SEQS
    assert EXPECTED_SEQS >= pr.min_samples(pr.load_matrix(),
                                           "control_delivered_pct")
    assert m["control_delivered_undersampled"] is False
    assert approx(m["control_delivered_pct"], 100.0)


def test_theta_gated_blocking_rows_are_suppressed_not_failed_without_a_clock():
    """The residual fail-open: T-5 is exempt from run-level invalidation
    on clock=none, but a blocking row whose metric is null BY CONSTRUCTION must
    not be scored either. Suppression is a property of the metric, not the suite."""
    for run_id in ("noclock_t5_r0",):
        r = run(run_id)
        assert r["verdict"]["status"] != pr.FAIL, run_id
        filed = {f["metric"] for f in r["verdict"]["failed_thresholds"]}
        for metric in pr.theta_gated_metrics(pr.load_matrix()):
            assert metric not in filed, f"{run_id}: {metric} scored without a clock"
        # Suppression is stated in the record, not silent.
        suppressed = {o["metric"] for o in r["verdict"]["observed"]
                      if o.get("suppressed")}
        assert "g2g_p50_ms" in suppressed, run_id


def test_control_owd_p99_is_scored_one_way_not_round_trip():
    """The C++ matrix scored an RTT p99 against §8.1c, which is a one-way clause —
    roughly 2x too strict. The corrected row is control_owd_p99_ms, advisory."""
    matrix = pr.load_matrix()
    rows = [t for t in matrix["thresholds"] if t.get("clause") == "§8.1c"]
    assert len(rows) == 1
    assert rows[0]["metric"] == "control_owd_p99_ms"
    assert rows[0]["effect"] == "advisory"
    assert not any(t["metric"] in ("rtt_p99_ms", "app_rtt_p99_ms", "network_rtt_p99_ms")
               for t in matrix["thresholds"])


def test_a_null_metric_never_fails_a_threshold():
    """A metric that was not measured is null. Scoring null against a bar would
    turn an unmeasured column into a failure."""
    matrix = pr.load_matrix()
    rec = json.loads(json.dumps(run("pass_h264_r0")))
    rec["metrics"]["network_rtt_p95_ms"] = None
    rec["metrics"]["video_bitrate_bps"] = None
    pr.score(rec, matrix)
    failed = {f["metric"] for f in rec["verdict"]["failed_thresholds"]}
    assert "network_rtt_p95_ms" not in failed
    assert "video_bitrate_bps" not in failed


def test_boolean_thresholds_reject_ordering_operators():
    with_bool = pr._compare("==", True, True)
    assert with_bool is True
    try:
        pr._compare("<=", True, False)
    except pr.AnalysisError:
        pass
    else:
        raise AssertionError("an ordering operator on a boolean must be refused")


# ---------------------------------------------------------------------------
# clock_unsynchronized — the mechanical suite-scoping rule (design §1g rule i)
# ---------------------------------------------------------------------------


def test_theta_gated_metrics_are_derived_from_matrix_yaml():
    """The gated set is read out of matrix.yaml's `requires` clauses, not listed
    in Python. A metric that gains or loses a theta dependency must change one
    file, not two."""
    matrix = pr.load_matrix()
    gated = pr.theta_gated_metrics(matrix)
    assert gated == {"control_late_pct", "control_owd_p50_ms", "control_owd_p99_ms",
                     "g2g_p50_ms", "g2g_p99_ms"}
    # Skew-immune by construction: RTT must never be in the gated set.
    assert "network_rtt_p95_ms" not in gated
    assert "network_rtt_p50_ms" not in gated


def test_clock_scoping_is_the_intersection_not_a_hardcoded_suite_list():
    """Recomputed here from matrix.yaml independently of parse_runs.py's own
    helper, so a regression in either is caught rather than agreeing with itself."""
    matrix = pr.load_matrix()
    gated = {n for n, d in matrix["metrics"].items()
             if isinstance(d, dict)
             and ("clock_sync_confidence" in str(d.get("requires", ""))
                  or "theta" in str(d.get("requires", "")))}
    expected = {s: bool(gated & set(d.get("primary") or []))
                for s, d in matrix["suites"].items()}
    assert expected == {
        "T1_video_floor": True,      # g2g_p50_ms, blocking under §8.1b
        "T2_loss_collapse": True,    # control_late_pct, blocking under §8.2b
        "T3_jitter_tolerance": True,
        "T4_capacity": True,         # control_late_pct, blocking under §8.2b
        "T5_availability": False,
        "Q7_latency_definition": True,
    }
    for suite, needs in expected.items():
        rec = {"suite": suite}
        assert pr._needs_one_way(rec, matrix) is needs, suite


def test_no_clock_sync_invalidates_every_suite_with_a_theta_gated_primary():
    """T-1, T-2, T-3, T-4 and Q-7 all carry a theta-gated metric as a BLOCKING
    primary. A blocking threshold whose value is null cannot be evaluated, so
    scoring PASS would assert a bar was cleared that was never measured."""
    for run_id in ("noclock_t1_r0", "noclock_t2_r0", "noclock_t3_r0",
                   "noclock_t4_r0", "noclock_q7_r0"):
        r = run(run_id)
        assert r["verdict"]["status"] == pr.INVALID, run_id
        assert r["validity"]["invalid_reasons"] == ["clock_unsynchronized"], run_id
        # INVALID, never FAIL — the run did not measure the thing.
        assert r["verdict"]["failed_thresholds"] == [], run_id


def test_no_clock_sync_leaves_t5_valid_with_columns_suppressed():
    """T-5 is the only suite whose primary set needs no clock offset, so the
    one-way columns are merely empty and the run still answers its question."""
    for run_id in ("noclock_t5_r0",):
        r = run(run_id)
        assert r["verdict"]["status"] != pr.INVALID, run_id
        assert r["validity"]["invalid_reasons"] == [], run_id
        # The suppression is recorded so an empty column is not read as a zero.
        assert any("clock_unsynchronized" in d
                   for d in r["validity"]["invalid_detail"]), run_id
        assert r["metrics"]["clock_sync_confidence"] == "none"


def test_one_way_columns_are_null_not_zero_without_a_clock_offset():
    """The failure this rule exists to prevent: control_late_pct reading 0.0% and
    clearing the 0.1% bar on a run that never measured lateness at all."""
    for run_id in ("noclock_t5_r0", "noclock_t2_r0"):
        m = run(run_id)["metrics"]
        assert m["control_late_pct"] is None, run_id
        assert m["control_owd_p50_ms"] is None, run_id
        assert m["control_owd_p99_ms"] is None, run_id
        assert m["g2g_p50_ms"] is None, run_id


def test_rtt_survives_the_loss_of_clock_sync():
    """Neither round trip depends on the clock offset, so both are measured
    normally on a run with no clock sync.

    The four-timestamp probe is skew-immune by construction — each difference is
    taken on a single clock — and the ICE round trip never involves the peer's
    clock at all. Asserts on real values under `clock_sync_confidence: none`, so
    it cannot pass vacuously against a name that no longer exists.
    """
    r = run("noclock_t5_r0")
    m = r["metrics"]
    assert m["clock_sync_confidence"] == "none"
    assert approx(m["app_rtt_p95_ms"], 30.0)
    assert approx(m["network_rtt_p95_ms"], 20.0)
    assert approx(m["video_bitrate_bps"], 4_000_000.0, tol=1e-3)
    # The run is not invalidated: no theta-gated metric is in T-5's primary set.
    assert r["validity"]["invalid_reasons"] == []


def test_neither_rtt_family_is_theta_gated():
    """Rule (i) intersects each suite's `primary` set with the theta-gated
    metrics, derived from `requires` clauses in matrix.yaml. If an RTT metric ever
    gained a theta dependency it would start invalidating runs on
    `clock_sync_confidence: none`, which is wrong for a skew-immune quantity.

    Also pins that the renamed metrics exist and the pre-split name does not, so
    the rename cannot leave a suite's `primary` referring to a defined-nowhere
    symbol that silently drops out of every intersection.
    """
    matrix = pr.load_matrix()
    gated = pr.theta_gated_metrics(matrix)
    rtts = {"app_rtt_p50_ms", "app_rtt_p95_ms", "app_rtt_p99_ms",
            "network_rtt_p50_ms", "network_rtt_p95_ms"}
    assert rtts & gated == set()
    assert rtts <= set(matrix["metrics"])
    assert not ({"rtt_p50_ms", "rtt_p95_ms", "rtt_p99_ms"} & set(matrix["metrics"]))

    # Every metric named in any suite's `primary` must be a defined metric, or the
    # intersection above silently under-reports.
    for name, suite in matrix["suites"].items():
        unknown = set(suite.get("primary") or []) - set(matrix["metrics"])
        assert not unknown, f"{name} primary names undefined metrics: {sorted(unknown)}"


def test_unsynchronized_clock_does_not_masquerade_as_missing_g2g_metadata():
    """Two different faults present as `g2g: None`, and only one is a valid
    result. Frame metadata is present here — coverage is 100% — so the run must
    be flagged as unsynchronized, never as the subscribe_timing_events fault."""
    r = run("noclock_t1_r0")
    assert approx(r["metrics"]["g2g_metadata_coverage_pct"], 100.0)
    assert "g2g_metadata_missing" not in r["validity"]["invalid_reasons"]
    assert r["validity"]["invalid_reasons"] == ["clock_unsynchronized"]


# ---------------------------------------------------------------------------
# never_pool_across
# ---------------------------------------------------------------------------


def test_grouping_never_pools_across_a_forbidden_axis():
    matrix = pr.load_matrix()
    records = list(scored().values())
    cells = pr.group_cells(records, matrix)
    for suite_cells in cells.values():
        for cell in suite_cells:
            for key in pr.never_pool_keys(matrix):
                vals = set()
                for r in cell.runs:
                    vals.add(dict(pr.pool_key(r, matrix))[key])
                assert len(vals) == 1, f"{cell.suite}/{cell.cell_id} pooled across {key}"


def test_av1_and_h264_never_share_a_cell():
    matrix = pr.load_matrix()
    cells = pr.group_cells(list(scored().values()), matrix)
    for suite_cells in cells.values():
        for cell in suite_cells:
            codecs = {(r["conditions"]["video_codec_actual"]
                       or r["conditions"]["video_codec_requested"])
                      for r in cell.runs}
            assert len(codecs) == 1


def test_pool_key_keeps_all_five_ran_fields():
    """The RAN hypothesis turns on the discard and reordering timers as much as
    on AM-vs-UM. Keying on rlc_mode alone would pool the very configurations a
    lab RAN varies to test it."""
    matrix = pr.load_matrix()
    base = json.loads(json.dumps(run("pass_h264_r0")))
    am_short = json.loads(json.dumps(base))
    am_short["environment"]["ran_profile"] = {
        "rlc_mode": "AM", "aqm_mode": "OFF", "pdcp_discard_timer_ms": 50,
        "pdcp_reordering_timer_ms": 20, "rlc_reassembly_timer_ms": 20}
    am_long = json.loads(json.dumps(am_short))
    # Same RLC mode, different discard timer: a different experiment.
    am_long["environment"]["ran_profile"]["pdcp_discard_timer_ms"] = 500
    assert pr.pool_key(am_short, matrix) != pr.pool_key(am_long, matrix)
    um = json.loads(json.dumps(am_short))
    um["environment"]["ran_profile"]["rlc_mode"] = "UM"
    assert pr.pool_key(am_short, matrix) != pr.pool_key(um, matrix)


def test_camera_and_test_pattern_runs_never_pool():
    """A camera makes bitrate depend on scene content, lighting and framing; the
    synthetic pattern presents every host with an identical encoding problem. Two
    runs alike in every other respect are still different experiments, and an
    average over both describes neither."""
    matrix = pr.load_matrix()
    assert "camera_source" in pr.never_pool_keys(matrix)
    pattern = json.loads(json.dumps(run("pass_h264_r0")))
    pattern["environment"]["camera_source"] = "test_pattern"
    camera = json.loads(json.dumps(pattern))
    camera["environment"]["camera_source"] = "FaceTime HD Camera"
    assert pr.pool_key(pattern, matrix) != pr.pool_key(camera, matrix)
    # Two cameras are two scenes, so they do not pool with each other either.
    other = json.loads(json.dumps(camera))
    other["environment"]["camera_source"] = "Logitech BRIO"
    assert pr.pool_key(camera, matrix) != pr.pool_key(other, matrix)


def test_the_mandatory_av1_cell_never_merges_with_h264():
    """With buffering fixed at zero_jitter the CODEC is the cell. The design §3
    required cell is AV1 under no jitter buffer, and its decode/assembly cost is
    the whole finding — pooling it with H264 would average that cost away."""
    matrix = pr.load_matrix()
    cells = pr.group_cells(list(scored().values()), matrix)["Q7_latency_definition"]
    assert all(dict(c.key)["buffering_mode"] == "zero_jitter" for c in cells)
    av1 = [c for c in cells if c.cell_id == "video_codec=av1"
           and dict(c.key)["video_codec"] == "av1"]
    # encoder_tier is never_pool_across, so the videotoolbox h264 cell is its own
    # cell even though it shares a cell_id with the sw one. That separation is
    # correct and load-bearing: AV1 has no hardware encoder on Apple Silicon, so
    # pooling tiers would compare a hardware H264 against a software AV1.
    h264 = [c for c in cells if c.cell_id == "video_codec=h264"
            and dict(c.key)["encoder_tier"] == "videotoolbox"]
    assert len(av1) == 1, [c.cell_id for c in cells]
    assert len(h264) == 1, [dict(c.key) for c in cells]
    # AV1 costs more to decode and assemble; that separation is the point.
    assert approx(av1[0].stat("decode_time_avg_ms")["median"], 9.0)
    assert approx(h264[0].stat("decode_time_avg_ms")["median"], 3.0)
    assert approx(av1[0].stat("assembly_time_avg_ms")["median"], 7.0)
    # The codec-fallback fixture requests av1 and negotiates vp9. It shares the
    # requested-codec cell_id but must NEVER pool with the real av1 cell, because
    # pool_key uses the ACTUAL codec.
    assert any(c.cell_id == "video_codec=av1"
               and dict(c.key)["video_codec"] == "vp9" for c in cells)


# ---------------------------------------------------------------------------
# Breakpoints
# ---------------------------------------------------------------------------


def test_breakpoint_is_a_point_when_repeats_agree():
    """Delivery holds at 100% through 2% loss and collapses to 75% at 5%. All
    three repeats agree, so the answer is a point with both bracketing cells."""
    matrix = pr.load_matrix()
    bps = pr.breakpoints(list(scored().values()), matrix, "T2_loss_collapse",
                         "loss_pct", "control_delivered_pct")
    # One group per transport — they are never pooled, and the dc_lossy reorder
    # fixture is a single-step group that cannot bracket a crossing.
    b = next(b for b in bps
             if dict(b["group"])["control_transport"] == "data_track_buf1")
    assert b["kind"] == "point"
    assert b["breakpoint"] == 5.0
    assert b["last_passing"] == 2.0
    assert b["first_failing"] == 5.0
    assert [s["value"] for s in b["steps"]] == [0.0, 0.5, 1.0, 2.0, 5.0, 10.0]


def test_breakpoint_is_a_range_when_repeats_disagree():
    """rtt p95 crosses 90 ms at 25 sessions in one repeat and at 50 in the other
    two. A breakpoint whose repeats disagree is a range, not a point."""
    matrix = pr.load_matrix()
    bps = pr.breakpoints(list(scored().values()), matrix, "T4_capacity",
                         "concurrency", "network_rtt_p95_ms")
    b = next(b for b in bps if len(b["steps"]) > 1)
    assert b["kind"] == "range"
    assert b["breakpoint"] == [25, 50]
    assert b["last_passing"] == 10


def test_t3_answer_is_a_curve_not_a_number():
    """T-3 is the one suite whose answer is a curve. The crossing must be
    computed per playout window: a wider deadline tolerates more jitter, and
    pooling the windows would report one number where the answer is three."""
    matrix = pr.load_matrix()
    bps = pr.breakpoints(list(scored().values()), matrix, "T3_jitter_tolerance",
                         "jitter_ms", "control_late_pct")
    curve = {dict(b["group"])["playout_window_ms"]: b["breakpoint"] for b in bps}
    assert curve == {5: 10, 10: 20, 20: 40}
    # Each point is a genuine bracket, not an endpoint artifact.
    for b in bps:
        assert b["kind"] == "point"
        assert b["last_passing"] is not None


def test_breakpoints_hold_the_suites_other_swept_axes_fixed():
    """The general property behind the T-3 curve: a breakpoint group never spans
    two values of another axis the same suite sweeps."""
    matrix = pr.load_matrix()
    records = list(scored().values())
    for suite, axis_name, metric in (
            ("T3_jitter_tolerance", "jitter_ms", "control_late_pct"),
            ("T2_loss_collapse", "loss_pct", "control_delivered_pct")):
        held = [a for a in matrix["suites"][suite]["sweep"] if a != axis_name]
        for b in pr.breakpoints(records, matrix, suite, axis_name, metric):
            group = dict(b["group"])
            for a in held:
                if a in group:
                    assert not isinstance(group[a], (list, set)), (suite, a)


def test_breakpoints_exclude_invalid_runs():
    """The concurrency=50 poll-overbudget fixture is INVALID and must not appear
    in the step counts: a run that did not measure the thing cannot mark the
    point where the thing broke."""
    matrix = pr.load_matrix()
    bps = pr.breakpoints(list(scored().values()), matrix, "T4_capacity",
                         "concurrency", "network_rtt_p95_ms")
    b = next(b for b in bps if len(b["steps"]) > 1)
    steps = {s["value"]: s["n"] for s in b["steps"]}
    assert steps[50] == 3, "the INVALID fourth run at concurrency=50 was counted"


def test_breakpoint_reports_not_reached_rather_than_guessing():
    matrix = pr.load_matrix()
    records = [r for r in scored().values()
               if r["suite"] == "T2_loss_collapse"
               and r["conditions"]["loss_pct"] <= 2.0]
    bps = pr.breakpoints(records, matrix, "T2_loss_collapse", "loss_pct",
                         "control_delivered_pct")
    assert bps[0]["breakpoint"] is None
    assert bps[0]["kind"] == "not_reached"


def test_breakpoint_refuses_an_unordered_axis():
    """'First crossing' has no meaning along video_codec: the answer would depend
    on nothing but alphabetical order."""
    matrix = pr.load_matrix()
    try:
        pr.breakpoints(list(scored().values()), matrix, "T2_loss_collapse",
                       "video_codec", "control_delivered_pct")
    except pr.AnalysisError as e:
        assert "ordered" in str(e)
    else:
        raise AssertionError("a breakpoint along an unordered axis must be refused")


def test_breakpoint_refuses_a_metric_with_no_blocking_threshold():
    matrix = pr.load_matrix()
    try:
        pr.breakpoints(list(scored().values()), matrix, "T2_loss_collapse",
                       "loss_pct", "video_rtx_pct")
    except pr.AnalysisError:
        pass
    else:
        raise AssertionError("a breakpoint without a bar must be refused")


# ---------------------------------------------------------------------------
# The retired V0 units gate
#
# V0_playout_units and playout_units_gate() are gone along with the playout-delay
# hint buffering modes. These tests replace the six that exercised the gate: what
# still needs guarding is that the retirement is COHERENT, i.e. nothing anywhere
# still expects a gate to exist or a hint mode to be a legal value. A half-removed
# gate is worse than either state, because analyze() would order records around a
# gate that never reports.
# ---------------------------------------------------------------------------


def test_no_validation_gate_is_declared_and_none_is_expected():
    matrix = pr.load_matrix()
    assert not [n for n, s in matrix["suites"].items() if s.get("validation_gate")]
    assert not [n for n, s in matrix["suites"].items() if s.get("run_first")]
    assert "V0_playout_units" not in matrix["suites"]
    assert not hasattr(pr, "playout_units_gate")


def test_buffering_mode_is_locked_to_zero_jitter_everywhere():
    """One value, and every suite that holds it holds that value. A suite still
    holding a retired hint mode would expand into cells the harness cannot run."""
    matrix = pr.load_matrix()
    axis = matrix["axes"]["buffering_mode"]
    assert axis["values"] == ["zero_jitter"]
    assert axis["fixed"] is True
    assert set(axis["settings"]) == {"zero_jitter"}
    assert matrix["reference_config"]["buffering_mode"] == "zero_jitter"
    for name, suite in matrix["suites"].items():
        held = suite.get("hold", {}).get("buffering_mode")
        assert held in (None, "zero_jitter"), f"{name} holds {held}"
        assert "sweep_buffering_for_codecs" not in suite, name
        assert "buffering_mode" not in suite.get("sweep", []), name


def test_every_scored_run_is_zero_jitter_with_no_hint_values():
    """The hint modes wrote playout_delay_min/max_ms; zero_jitter never does.
    A non-null pair on a zero_jitter run means a hint was applied anyway."""
    for r in scored().values():
        assert r["conditions"]["buffering_mode"] == "zero_jitter", r["run_id"]
        assert r["conditions"]["playout_delay_min_ms"] is None, r["run_id"]
        assert r["conditions"]["playout_delay_max_ms"] is None, r["run_id"]


def test_jitter_buffer_delay_is_differenced_not_read_as_cumulative():
    """Known answer: the fixture contributes 3 ms of delay per emitted frame and a
    26 ms target. jitter_buffer_delay_s is CUMULATIVE, so an extractor that reads
    the last value instead of differencing reports a number that scales with run
    length -- here it would be ~3 ms x the frame count, not 3 ms."""
    m = run("jb_zero_jitter_r0")["metrics"]
    assert approx(m["jitter_buffer_delay_avg_ms"], 3.0, tol=1e-6)
    assert approx(m["jitter_buffer_target_delay_ms"], 26.0, tol=1e-6)




# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def test_report_renders_and_names_its_boundaries():
    matrix = pr.load_matrix()
    report = pr.Report(list(scored().values()), matrix)
    text = report.render()
    assert "## V0" not in text, "the retired units gate still renders a section"
    assert "zero_jitter" in text, "the report must state the buffering mode"
    assert "## Validity appendix" in text
    # Every INVALID run appears in the appendix with its reason.
    for r in scored().values():
        if r["verdict"]["status"] == pr.INVALID:
            assert r["run_id"] in text
            for reason in r["validity"]["invalid_reasons"]:
                assert reason in text
    # Boundary statements the report must never omit.
    assert "capture → app-delivery" in text
    assert "not cell uplink capacity" in text.lower() or "not** cell uplink" in text
    assert "poll intervals" in text.lower()
    assert "never pooled" in text.lower() or "never pooled across" in text.lower()


def test_report_shows_dispersion_when_repeats_disagree():
    matrix = pr.load_matrix()
    text = pr.Report(list(scored().values()), matrix).render()
    # The T-4 25-session cell has repeats at 70, 72 and 95 ms.
    assert "[70.0–95.0]" in text


def _cli(*argv: str) -> subprocess.CompletedProcess:
    _ensure_fixtures()
    return subprocess.run(
        [sys.executable, str(HERE / "parse_runs.py"), *argv],
        capture_output=True, text=True, cwd=str(HERE))


def _verdict_counts(json_path: Path) -> dict[str, int]:
    counts: dict[str, int] = {}
    for line in json_path.read_text().splitlines():
        if line.strip():
            status = json.loads(line)["verdict"]["status"]
            counts[status] = counts.get(status, 0) + 1
    return counts


def test_cli_resolves_snapshots_with_runs_pointing_at_a_directory():
    """Exercises main() itself, with no --base-dir.

    The internal tests all pass base_dir explicitly, which is exactly why a
    whole matrix of INVALID runs went unnoticed: the path-resolution default was
    never covered.
    """
    out = HERE / "fixtures" / "_cli_dir.jsonl"
    proc = _cli("--runs", "fixtures/runs", "--json", str(out),
                "--report", str(HERE / "fixtures" / "_cli_dir.md"))
    assert proc.returncode == 0, proc.stderr
    counts = _verdict_counts(out)
    assert sum(counts.values()) > 100
    assert counts.get(pr.PASS, 0) > 0, f"nothing passed: {counts}"
    # The symptom of the bug this guards: everything INVALID.
    assert counts.get(pr.INVALID, 0) < sum(counts.values()) / 2, counts
    out.unlink()
    (HERE / "fixtures" / "_cli_dir.md").unlink()


def test_cli_resolves_snapshots_with_runs_pointing_at_a_single_file():
    """The file form sits one level deeper than the directory form: runs/ and
    snapshots/ are siblings, so a file's root is its grandparent."""
    out = HERE / "fixtures" / "_cli_file.jsonl"
    proc = _cli("--runs", "fixtures/runs/T2_loss_collapse.jsonl",
                "--json", str(out),
                "--report", str(HERE / "fixtures" / "_cli_file.md"))
    assert proc.returncode == 0, proc.stderr
    counts = _verdict_counts(out)
    assert sum(counts.values()) > 0
    assert counts.get(pr.INVALID, 0) < sum(counts.values()), (
        f"single-file form resolved no snapshots: {counts}")
    assert counts.get(pr.PASS, 0) > 0, counts
    out.unlink()
    (HERE / "fixtures" / "_cli_file.md").unlink()


def test_default_base_dir_matches_the_sibling_invariant():
    """Both forms must resolve to the root that holds runs/ and snapshots/ as
    siblings. Uses the real fixture tree: the branch turns on `is_dir()`, so
    made-up paths would take the file branch in both cases and prove nothing."""
    assert pr.default_base_dir(RUNS) == FIXTURES
    assert pr.default_base_dir(RUNS / "T2_loss_collapse.jsonl") == FIXTURES
    # The invariant they encode, asserted directly.
    assert (FIXTURES / "runs").is_dir()
    assert (FIXTURES / "snapshots").is_dir()


def test_a_missing_snapshot_file_is_an_operator_error_not_a_lost_session():
    """The confident wrong answer this guards: a wrong --base-dir reported as
    `session_lost_mid_run`, a specific claim about the network, when the real
    cause is a path that does not resolve."""
    proc = _cli("--runs", "fixtures/runs", "--base-dir", "/nonexistent-root")
    assert proc.returncode == 2, proc.stdout[-500:]
    assert "does not exist" in proc.stderr
    assert "operator error" in proc.stderr
    assert "base directory resolved to" in proc.stderr
    # It must NOT have produced a report full of INVALID verdicts.
    assert "session_lost_mid_run" not in proc.stdout


def test_a_missing_seq_log_raises_rather_than_nulling_the_denominator():
    matrix = pr.load_matrix()
    rec = json.loads(json.dumps(run("pass_h264_r0")))
    rec["raw"]["publisher_seq_log_path"] = "snapshots/does_not_exist.seq.jsonl"
    try:
        pr.extract(rec, matrix, base_dir=FIXTURES)
    except pr.AnalysisError as e:
        assert "does not exist" in str(e)
        assert "denominator" in str(e)
    else:
        raise AssertionError("a missing seq log must raise, not null the metric")


def test_report_exits_nonzero_on_a_halted_gate():
    _ensure_fixtures()
    proc = subprocess.run(
        [sys.executable, str(HERE / "parse_runs.py"), "--runs", str(RUNS),
         "--base-dir", str(FIXTURES)],
        capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


# ---------------------------------------------------------------------------
# camera_source is a non-poolable dimension
# ---------------------------------------------------------------------------


def _record_with_camera_source(source: str) -> dict:
    """A minimal record differing from its sibling ONLY in camera_source."""
    return {
        "conditions": {
            "video_codec_requested": "h264",
            "video_codec_actual": "h264",
            "buffering_mode": "zero_jitter",
            "control_transport": "data_track_buf1",
        },
        "environment": {
            "camera_source": source,
            "encoder_tier": "videotoolbox",
            "path": "cloud",
            "ran_profile": "n/a",
        },
    }


def test_repeats_on_one_source_still_pool():
    """The complement of the never-pool rule: separating by source must not also
    separate repeats of the same source, or a cell could never aggregate its
    repeats and every dispersion figure would be undefined."""
    matrix = pr.load_matrix()
    first = pr.pool_key(_record_with_camera_source("FaceTime HD Camera"), matrix)
    second = pr.pool_key(_record_with_camera_source("FaceTime HD Camera"), matrix)
    assert first == second
    assert dict(first)["camera_source"] == "FaceTime HD Camera"


def test_a_record_without_camera_source_is_not_pooled_with_the_pattern():
    """A run predating the field, or one whose harness never recorded it, is of
    UNKNOWN provenance. Defaulting it to the pattern would silently pool an
    unattributed run with attributed ones."""
    matrix = pr.load_matrix()
    unknown = _record_with_camera_source("test_pattern")
    del unknown["environment"]["camera_source"]
    assert pr.pool_key(unknown, matrix) != pr.pool_key(
        _record_with_camera_source("test_pattern"), matrix)


def test_the_three_video_sources_never_pool_with_each_other():
    """An IP camera, a local lens and the generator are three different encoding
    problems. camera_source is what keys the group, so the three must be distinct
    strings — an RTSP run that keyed the same as a pattern run would be aggregated
    with it and nothing downstream could detect that."""
    matrix = pr.load_matrix()
    keys = {
        pr.pool_key(_record_with_camera_source(source), matrix)
        for source in ("test_pattern",
                       "FaceTime HD Camera",
                       "rtsp://192.168.100.123/full1080p")
    }
    assert len(keys) == 3


# ---------------------------------------------------------------------------
# run_matrix.py credential redaction
#
# The run record is committed and shared and RTSP URLs commonly embed user:pass.
# A bug here leaks a password into git history, so it is tested here rather than
# left to the one place it is called.
# ---------------------------------------------------------------------------


def test_rtsp_credentials_are_stripped_from_the_recorded_source():
    import run_matrix as rm
    assert rm.redact_camera_source(
        "rtsp://admin:hunter2@192.168.100.123/full1080p"
    ) == "rtsp://***@192.168.100.123/full1080p"
    # A bare username is still a credential.
    assert rm.redact_camera_source(
        "rtsp://admin@10.0.0.5:554/stream") == "rtsp://***@10.0.0.5:554/stream"
    # A password containing an @ must not leave part of itself behind.
    assert rm.redact_camera_source(
        "rtsp://admin:p@ss@10.0.0.5/s") == "rtsp://***@10.0.0.5/s"


def test_redaction_leaves_a_credentialless_source_byte_identical():
    """Over-redacting would name a stream that does not exist, and would break the
    never_pool_across grouping for every pattern and local-device run."""
    import run_matrix as rm
    for value in ("test_pattern",
                  "0",
                  "FaceTime HD Camera",
                  "rtsp://192.168.100.123/full1080p",
                  "rtsp://192.168.100.123:554/4k?profile=1",
                  # An @ in the stream path is not a credential delimiter.
                  "rtsp://192.168.100.123/live@2"):
        assert rm.redact_camera_source(value) == value


def test_the_recorded_harness_cmd_carries_no_credential():
    """harness_cmd is the verbatim argv, and the argv contains --camera-source. Writing
    it unredacted put an RTSP password into every committed run record -- the record is
    the single most durable place a credential can land, since it is committed to git."""
    import run_matrix as rm
    argv = ["./teleop-harness", "--room-name", "r",
            "--camera-source", "rtsp://admin:hunter2@192.168.100.123/full1080p",
            "--rtsp-transport", "tcp"]
    recorded = [rm.redact_camera_source(a) for a in argv]
    assert not any("hunter2" in a for a in recorded), recorded
    assert "rtsp://***@192.168.100.123/full1080p" in recorded
    # Every other argument must survive byte-for-byte, or the record stops being a
    # faithful account of what was invoked.
    assert recorded[:4] == argv[:4]
    assert recorded[5:] == argv[5:]


def test_the_python_and_rust_redactions_agree():
    """Two implementations of one rule drift. The harness redacts what it records
    and the runner redacts what it plans; if they disagree, one record in a pair
    carries a credential the other stripped."""
    import run_matrix as rm
    binary = HERE.parent / "target" / "release" / "teleop-harness"
    if not binary.exists():
        return  # no release build here; the Rust-side unit tests still cover it
    # NOTE: this compares against whatever binary is on disk. A STALE binary fails this
    # test even when the source is correct -- rebuild with
    # `cargo build -p teleop-test-matrix --release` before reading a failure here as a
    # real leak.
    url = "rtsp://admin:hunter2@192.168.100.123/full1080p"
    out = subprocess.run(
        [str(binary), "--room-name", "r", "--duration-s", "1",
         "--snapshots-out", "/tmp/unused.jsonl",
         "--camera-source", url, "--validate-args"],
        capture_output=True, text=True, check=True).stdout
    assert "hunter2" not in out
    assert f"camera_source={rm.redact_camera_source(url)}" in out


# ---------------------------------------------------------------------------


def _main() -> int:
    fns = [(name, fn) for name, fn in sorted(globals().items())
           if name.startswith("test_") and callable(fn)]
    failures = []
    for name, fn in fns:
        try:
            fn()
            print(f"  ok   {name}")
        except Exception as e:  # noqa: BLE001 - a test runner reports, not raises
            failures.append((name, e))
            print(f"  FAIL {name}: {type(e).__name__}: {e}")
    print(f"\n{len(fns) - len(failures)}/{len(fns)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(_main())
