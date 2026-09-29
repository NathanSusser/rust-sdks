#!/usr/bin/env python3
"""Generates the known-answer fixtures parse_runs.py is tested against.

Every fixture is built from round numbers so that the expected metric values can
be computed by hand and asserted exactly, rather than compared against whatever
the parser happens to produce. The expected values are in test_parse_runs.py,
derived independently of this file.

Snapshot shape follows teleop-test-matrix/src/snapshot.rs exactly, including the
terminal `run_metadata` record — its absence is the incomplete-run signal, so one
fixture deliberately omits it.

    python3 fixtures/make_fixtures.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).parent
SNAPSHOTS = HERE / "snapshots"
RUNS = HERE / "runs"

ORIGIN_US = 1_787_611_950_000_000
WARMUP_S = 15
DURATION_S = 120
POLL_MS = 1000.0
# Post-warmup polls per run, at 1 Hz.
#
# The scored window must clear control_delivered_pct's ">=10000 samples"
# precondition (matrix.yaml), which at 200 Hz means at least 50 s. 61 scored
# polls give a 60 s window and 12 000 published seqs — above the floor with
# margin, and still round numbers so every expected value stays hand-computable.
SCORED_POLLS = 61


# ---------------------------------------------------------------------------
# Snapshot construction
# ---------------------------------------------------------------------------


def sampler(i: int, *, overbudget_polls: int = 0, rpc_failures: int = 0,
            interval_ms: float = POLL_MS) -> dict:
    return {
        "nominal_interval_ms": POLL_MS,
        "actual_interval_ms": interval_ms,
        "poll_duration_ms": 4.0,
        "overbudget": i <= overbudget_polls and i > 0,
        "overbudget_count": min(i, overbudget_polls),
        "polls_total": i + 1,
        "stats_rpc_failed": False,
        "stats_rpc_failures": min(i, rpc_failures),
    }


def video_out(i: int, *, bitrate_bps: float, fps: float, w: int, h: int,
              codec_mime: str, encode_ms_per_frame: float = 4.0,
              cpu_s_per_poll: float = 0.0, bw_s_per_poll: float = 0.0,
              packets: bool = True, encoder_impl: str = "libaom",
              power_efficient: bool = False, pli_at: tuple[int, ...] = (),
              kf_at: tuple[int, ...] = ()) -> dict:
    """Cumulative send-side counters at poll `i`.

    bytes_sent grows so that (bytes+header)*8/elapsed equals `bitrate_bps`
    exactly: header bytes are a fixed share so the caller's target is the WIRE
    rate the 5 Mbps ceiling is about.
    """
    total_bytes = int(bitrate_bps / 8 * i)
    header = int(total_bytes * 0.05)
    payload = total_bytes - header
    frames = int(fps * i)
    return {
        "bytes_sent": payload,
        "header_bytes_sent": header,
        "packets_sent": (frames * 10) if packets else 0,
        "retransmitted_packets_sent": 0,
        "frames_encoded": frames,
        "key_frames_encoded": sum(1 for k in kf_at if k <= i),
        "frames_sent": frames,
        "frames_per_second": fps,
        "frame_width": w,
        "frame_height": h,
        "total_encode_time_s": frames * encode_ms_per_frame / 1000.0,
        "target_bitrate_bps": bitrate_bps,
        "qp_sum": frames * 30,
        "nack_count": 0,
        "pli_count": sum(1 for p in pli_at if p <= i),
        "fir_count": 0,
        "quality_limitation_reason": ("cpu" if cpu_s_per_poll > 0
                                      else ("bandwidth" if bw_s_per_poll > 0 else "none")),
        "quality_limitation_cpu_s": cpu_s_per_poll * i,
        "quality_limitation_bandwidth_s": bw_s_per_poll * i,
        "quality_limitation_other_s": 0.0,
        "quality_limitation_none_s": max(0.0, i - (cpu_s_per_poll + bw_s_per_poll) * i),
        "quality_limitation_resolution_changes": 0,
        "encoder_implementation": encoder_impl,
        "power_efficient_encoder": power_efficient,
        "malformed_bitstream": (not packets) and frames > 0,
        "codec_mime_type": codec_mime,
    }


def video_in(i: int, *, fps: float, w: int, h: int, codec_mime: str,
             packets_per_poll: int = 300, lost_per_poll: int = 0,
             jb_ms_per_frame: float = 12.0, decode_ms: float = 3.0,
             assembly_ms: float = 1.0, processing_ms: float = 5.0,
             freezes: int = 0, freeze_ms_each: float = 0.0,
             rtx_per_poll: int = 0, clamp_at: int | None = None,
             frame_intervals_ms: list[float] | None = None,
             target_jb_ms_per_frame: float | None = None) -> dict:
    frames = int(fps * i)
    emitted = frames
    lost_cum = lost_per_poll * i
    d = {
        "bytes_received": frames * 5000,
        "header_bytes_received": frames * 250,
        "packets_received": packets_per_poll * i,
        "packets_lost": lost_cum,
        "packets_lost_delta": lost_per_poll,
        "retransmitted_packets_received": rtx_per_poll * i,
        "frames_decoded": frames,
        "key_frames_decoded": max(1, i // 10),
        "frames_dropped": 0,
        "frames_received": frames,
        "frames_per_second": fps,
        "frame_width": w,
        "frame_height": h,
        "freeze_count": freezes * i,
        "total_freeze_duration_s": freezes * freeze_ms_each * i / 1000.0,
        "pause_count": 0,
        "total_pause_duration_s": 0.0,
        "jitter_s": 0.003,
        "jitter_buffer_delay_s": emitted * jb_ms_per_frame / 1000.0,
        "jitter_buffer_target_delay_s": emitted * (
            target_jb_ms_per_frame if target_jb_ms_per_frame is not None
            else jb_ms_per_frame) / 1000.0,
        "jitter_buffer_minimum_delay_s": emitted * 0.001,
        "jitter_buffer_emitted_count": emitted,
        "total_decode_time_s": frames * decode_ms / 1000.0,
        "total_processing_delay_s": emitted * processing_ms / 1000.0,
        "total_assembly_time_s": frames * assembly_ms / 1000.0,
        "frames_assembled_from_multiple_packets": frames,
        "total_inter_frame_delay_s": i * 1.0,
        "nack_count": 0,
        "pli_count": 0,
        "qp_sum": frames * 30,
        "decoder_implementation": "libdav1d",
        "power_efficient_decoder": False,
        "codec_mime_type": codec_mime,
        "frame_arrival_intervals_ms": frame_intervals_ms or [33.0, 33.0, 34.0],
    }
    if clamp_at is not None and i == clamp_at:
        d["packets_lost_delta"] = 0
        d["packets_lost_clamped_from"] = -3
        d["packets_lost"] = lost_per_poll * i - 3
    return d


def audio_in(i: int, *, level: float = 0.4, conceal_per_poll: int = 0,
             samples_per_poll: int = 48000) -> dict:
    return {
        "bytes_received": i * 6000,
        "packets_received": i * 50,
        "packets_lost": 0,
        "packets_lost_delta": 0,
        "jitter_s": 0.002,
        "jitter_buffer_delay_s": i * samples_per_poll * 0.00002,
        "jitter_buffer_emitted_count": i * samples_per_poll,
        "total_samples_received": i * samples_per_poll,
        "concealed_samples": conceal_per_poll * i,
        "silent_concealed_samples": conceal_per_poll * i // 4,
        "concealment_events": (1 if conceal_per_poll else 0) * i,
        "inserted_samples_for_deceleration": 0,
        "removed_samples_for_acceleration": 0,
        "total_samples_duration_s": i * 1.0,
        "audio_level": level,
    }


def audio_out(i: int, *, bitrate_bps: float = 250_000) -> dict:
    total = int(bitrate_bps / 8 * i)
    return {
        "bytes_sent": int(total * 0.9),
        "header_bytes_sent": total - int(total * 0.9),
        "packets_sent": i * 50,
        "target_bitrate_bps": bitrate_bps,
    }


def audio_playout(i: int, *, playout_ms: float = 30.0,
                  samples_per_poll: int = 48000) -> dict:
    n = i * samples_per_poll
    return {
        "total_playout_delay_s": n * playout_ms / 1000.0,
        "total_samples_count": n,
        "synthesized_samples_duration_s": 0.0,
        "synthesized_samples_events": 0,
        "total_samples_duration_s": i * 1.0,
    }


def transport(i: int, *, rtt_ms: float = 20.0) -> dict:
    return {
        "ice_state": "connected",
        "dtls_state": "connected",
        "selected_candidate_pair_changes": 1,
        "packets_sent": i * 400,
        "packets_received": i * 400,
        "candidate_pair_rtt_s": rtt_ms / 1000.0,
        # Subscriber-side BWE, named for the peer connection it describes. It is
        # NOT the publisher's uplink estimate and no metric extracts it; the field
        # exists only so the fixture matches the harness's snapshot shape.
        "subscriber_available_outgoing_bitrate_bps": 8_000_000.0,
        "rtcp_rtt_s": rtt_ms / 1000.0,
    }


def control(i: int, *, rate_hz: int = 200, delivered_per_poll: int | None = None,
            late_per_poll: int = 0, owd_ms: float = 20.0, gap_p99: int | None = None,
            max_gap: int = 0, jitter_ms: float = 0.5,
            clock_valid: bool = True) -> dict:
    """`clock_valid=False` omits the late counters entirely.

    The harness cannot classify a sample as late without a clock offset, so the
    fields are absent rather than zero — zero would clear the 0.1% bar on a run
    that never measured lateness at all.
    """
    published = rate_hz * i
    per_poll = delivered_per_poll if delivered_per_poll is not None else rate_hz
    received = per_poll * i
    if not clock_valid:
        return {
            "seq_published": published,
            "distinct_seq_received": received,
            "distinct_seq_received_interval": per_poll,
            "max_seq_received": max(published - 1, 0),
            "reordered_interval": 0,
            "duplicates_interval": 0,
            "max_gap": max_gap,
            "gap_lengths": [max_gap] if max_gap else [],
            **({"gap_p99": gap_p99} if gap_p99 is not None else {}),
            "owd_raw_us_interval": [int(owd_ms * 1000)],
            "jitter_ms": jitter_ms,
        }
    return {
        "seq_published": published,
        "distinct_seq_received": received,
        "distinct_seq_received_interval": per_poll,
        "max_seq_received": max(published - 1, 0),
        "reordered_interval": 0,
        "duplicates_interval": 0,
        "max_gap": max_gap,
        "gap_lengths": [max_gap] if max_gap else [],
        **({"gap_p99": gap_p99} if gap_p99 is not None else {}),
        "owd_raw_us_interval": [int(owd_ms * 1000), int(owd_ms * 1000) + 500],
        "jitter_ms": jitter_ms,
        "late_count": late_per_poll * i,
        "late_eligible_count": received,
    }


def probe(i: int, *, rtt_ms: float = 30.0, theta_ms: float | None = 0.0,
          confidence: str = "probe", probes_per_poll: int = 2,
          lost_per_poll: int = 0) -> dict:
    return {
        "probes_sent": probes_per_poll * i,
        "probes_completed": (probes_per_poll - lost_per_poll) * i,
        "rtt_us_interval": [int(rtt_ms * 1000)] * (probes_per_poll - lost_per_poll),
        **({"theta_ms": theta_ms} if theta_ms is not None else {}),
        "clock_sync_confidence": confidence,
    }


def g2g(i: int, *, latency_ms: float, fps: float, without_timestamp_frac: float = 0.0,
        lost_frames: int = 0, clock_valid: bool = True) -> dict:
    """`clock_valid=False` yields metadata coverage but no corrected latencies.

    The frames carry their capture stamps — coverage is fine — but without an
    offset the two clocks are unrelated, so no latency can be derived. This is
    the case the coverage gate exists to distinguish from the ordering fault.
    """
    frames = int(fps * i)
    without = int(frames * without_timestamp_frac)
    samples = ([] if (without_timestamp_frac >= 1.0 or not clock_valid)
               else [int(latency_ms * 1000)] * 3)
    return {
        "latency_us_interval": samples,
        "frames_captured": frames + lost_frames,
        "distinct_frame_ids": frames,
        "frame_id_span": frames + lost_frames,
        "frames_without_timestamp": without,
    }


def run_metadata(*, requested_codec: str, negotiated_codec: str | None,
                 buffering_mode: str, encoder_tier: str = "sw",
                 encoder_impl: str = "libaom", seq_published: int,
                 playout_units_confirmed: bool | None = None,
                 reconnect_count: int = 0, pid: int = 4242,
                 window_start_us: int, window_end_us: int) -> dict:
    d = {
        "record": "run_metadata",
        "scored_window_start_unix_us": window_start_us,
        "scored_window_end_unix_us": window_end_us,
        "run_origin_unix_us": ORIGIN_US,
        "warmup_excluded_s": WARMUP_S,
        "subscriber_process_id": pid,
        "publisher_process_id": pid,
        "harness_version": "0.1.0",
        "requested_codec": requested_codec,
        "seq_published": seq_published,
        "send_failures": 0,
        "reconnect_count": reconnect_count,
        "buffering_mode": buffering_mode,
        "playout_delay_applied": ("not_requested" if buffering_mode == "zero_jitter"
                                  else "room_api"),
    }
    if negotiated_codec is not None:
        d["negotiated_codec"] = negotiated_codec
    if encoder_impl:
        d["encoder_implementation"] = encoder_impl
    if encoder_tier:
        d["encoder_tier"] = encoder_tier
    if playout_units_confirmed is not None:
        d["playout_units_confirmed"] = playout_units_confirmed
    return d


# ---------------------------------------------------------------------------
# Run assembly
# ---------------------------------------------------------------------------


def build_snapshots(polls: int, *, builders: dict, warmup_polls: int = 2) -> list[dict]:
    """Poll 0 is the counter origin. Warmup polls carry `scored: false`.

    Differencing needs a pre-window reading, so the scored window's first delta is
    computed from the last warmup poll — which is exactly what the harness's own
    `scored` flag expresses.
    """
    out = []
    for i in range(polls):
        t_us = ORIGIN_US + int(i * POLL_MS * 1000)
        snap = {
            "poll_index": i,
            "t_unix_us": t_us,
            "t_monotonic_us": int(i * POLL_MS * 1000),
            "scored": i >= warmup_polls,
            "sampler": builders["sampler"](i),
            "control": builders["control"](i),
            "probe": builders["probe"](i),
        }
        for key in ("video_out", "video_in", "audio_out", "audio_in",
                    "audio_playout", "transport", "data_channel", "g2g"):
            fn = builders.get(key)
            if fn is not None:
                v = fn(i)
                if v is not None:
                    snap[key] = v
        out.append(snap)
    return out


def write_snapshots(run_id: str, snaps: list[dict], meta: dict | None) -> None:
    SNAPSHOTS.mkdir(parents=True, exist_ok=True)
    path = SNAPSHOTS / f"{run_id}.jsonl"
    with open(path, "w") as f:
        for s in snaps:
            f.write(json.dumps(s) + "\n")
        if meta is not None:
            f.write(json.dumps(meta) + "\n")


def write_seq_log(run_id: str, *, rate_hz: int, window_start_us: int,
                  window_end_us: int, shortfall_pct: float = 0.0) -> str:
    """The publisher-side seq log — the control_delivered_pct denominator.

    Emitted at exactly `rate_hz` across the window so the expected denominator is
    hand-computable: rate_hz * window_seconds.
    """
    SNAPSHOTS.mkdir(parents=True, exist_ok=True)
    path = SNAPSHOTS / f"{run_id}.seq.jsonl"
    window_s = (window_end_us - window_start_us) / 1e6
    nominal = int(rate_hz * window_s)
    emitted = int(nominal * (1 - shortfall_pct / 100.0))
    step_us = int((window_end_us - window_start_us) / max(emitted, 1))
    with open(path, "w") as f:
        for n in range(emitted):
            t = window_start_us + n * step_us
            f.write(json.dumps({
                "seq": n, "t_send_unix_us": t,
                "t_send_monotonic_us": t - ORIGIN_US, "probe": n % 100 == 0}) + "\n")
    return str(path)


def run_record(*, run_id: str, suite: str, cell_id: str, repeat: int,
               codec: str, buffering: str = "zero_jitter",
               transport_mode: str = "data_track_buf1", profile: str = "tolerable",
               w: int = 1920, h: int = 1080, loss_pct: float = 0.0,
               owd_ms: float = 0.0, jitter_ms: float = 0.0,
               uplink_mbps: float = 10.0, concurrency: int = 1,
               playout_window_ms: float | None = 10.0,
               encoder_tier: str = "sw", path: str = "cloud",
               video_poll_hz: float = 1.0, events: list | None = None,
               shaping_applied: bool = False, seq_log: str | None = None,
               fault: str | None = None,
               g2g_coverage: float | None = None) -> dict:
    return {
        "run_id": run_id,
        "suite": suite,
        "cell_id": cell_id,
        "repeat_index": repeat,
        "started_utc": "2026-08-24T12:00:00+00:00",
        "ended_utc": "2026-08-24T12:02:00+00:00",
        "duration_s": DURATION_S,
        "tier": 0,
        "conditions": {
            "video_codec_requested": codec,
            "video_codec_actual": None,
            "codec_mismatch": False,
            "buffering_mode": buffering,
            # Always null: zero_jitter does not go through the room API.
            "playout_delay_min_ms": None,
            "playout_delay_max_ms": None,
            "audio_buffering": "sdk_default",
            "control_transport": transport_mode,
            "control_buffer_size": 1 if transport_mode == "data_track_buf1" else None,
            "control_max_partial_frames": 1 if transport_mode == "data_track_buf1" else None,
            "control_rate_hz": 200,
            "playout_window_ms": playout_window_ms,
            "video_profile": profile,
            "video_width_requested": w,
            "video_height_requested": h,
            "video_width_actual": None,
            "video_height_actual": None,
            "video_fps_requested": 30,
            "video_max_bitrate_bps": 5_000_000,
            "simulcast": False,
            "dynacast": False,
            "audio_enabled": True,
            "audio_direction": "bidirectional",
            "audio_source": "synthetic_tone",
            "loss_pct": loss_pct,
            "owd_ms": owd_ms,
            "jitter_ms": jitter_ms,
            "uplink_mbps": uplink_mbps,
            "concurrency": concurrency,
            "fault_injection": fault,
            "netem_cmd": None,
            "tbf_cmd": None,
            "tc_qdisc_show": None,
            "shaping_applied": shaping_applied,
        },
        "environment": {
            "path": path,
            "encoder_tier": encoder_tier,
            "encoder_requested": "auto",
            "encoder_implementation": None,
            "decoder_implementation": None,
            "power_efficient_encoder": None,
            "ran_profile": {"rlc_mode": "n/a", "aqm_mode": "n/a",
                            "pdcp_discard_timer_ms": "n/a",
                            "pdcp_reordering_timer_ms": "n/a",
                            "rlc_reassembly_timer_ms": "n/a"},
            "camera_source": "test_pattern",
            "host_id": "fixture-host",
            "host_os": "Darwin 25.5.0",
            "host_arch": "arm64",
            "load_generator_hosts": [],
            "sfu_url": "wss://fixture.livekit.cloud",
            "sfu_version": None,
            "sdk_git_sha": "fixture",
            "build_config": "release",
            "clock_source": "ntp",
        },
        "harness": {
            "video_poll_hz": video_poll_hz,
            "stats_poll_hz": 1,
            "warmup_excluded_s": WARMUP_S,
            "poll_overbudget_multiplier": 1.5,
            "subscriber_process_id": 4242,
            "publisher_process_id": None,
            "scored_window_start_unix_us": None,
            "scored_window_end_unix_us": None,
            "playout_units_confirmed": None,
            "publisher_seq_log": seq_log,
            "harness_version": None,
            "harness_cmd": ["teleop-test-matrix", "--fixture"],
        },
        "metrics": {},
        "distributions": {},
        "events": events or [],
        "raw": {
            "snapshots_jsonl_path": f"snapshots/{run_id}.jsonl",
            "publisher_seq_log_path": f"snapshots/{run_id}.seq.jsonl" if seq_log else None,
        },
        "validity": {
            "valid": True,
            "invalid_reasons": [],
            "invalid_detail": [],
            "clock_sync_confidence": "none",
            "theta_ms": None,
            "g2g_metadata_coverage_pct": g2g_coverage,
            "frames_received": None,
            "frames_with_metadata": None,
            "warmup_excluded_s": WARMUP_S,
            "samples_scored": None,
            "notes": [],
        },
    }


MIME = {"av1": "video/AV1", "h264": "video/H264", "vp9": "video/VP9", "vp8": "video/VP8"}


def make_run(run_id: str, *, record_kwargs: dict, video_out_kwargs: dict | None,
             video_in_kwargs: dict | None, meta_kwargs: dict | None,
             polls: int = SCORED_POLLS + 2, warmup_polls: int = 2,
             control_kwargs: dict | None = None, probe_kwargs: dict | None = None,
             audio_in_kwargs: dict | None = None, g2g_kwargs: dict | None = None,
             sampler_kwargs: dict | None = None,
             transport_kwargs: dict | None = None,
             seq_shortfall_pct: float = 0.0,
             with_seq_log: bool = True) -> dict:
    """Assembles one fixture: snapshots + seq log + run record."""
    codec = record_kwargs["codec"]
    mime = MIME[codec]

    window_start = ORIGIN_US + int(warmup_polls * POLL_MS * 1000)
    window_end = ORIGIN_US + int((polls - 1) * POLL_MS * 1000)

    seq_path = None
    if with_seq_log:
        seq_path = write_seq_log(run_id, rate_hz=200, window_start_us=window_start,
                                 window_end_us=window_end,
                                 shortfall_pct=seq_shortfall_pct)

    builders = {
        "sampler": lambda i: sampler(i, **(sampler_kwargs or {})),
        "control": lambda i: control(i, **(control_kwargs or {})),
        "probe": lambda i: probe(i, **(probe_kwargs or {})),
        "transport": lambda i: transport(i, **(transport_kwargs or {})),
        "audio_out": audio_out,
        "audio_in": lambda i: audio_in(i, **(audio_in_kwargs or {})),
        "audio_playout": audio_playout,
    }
    if video_out_kwargs is not None:
        builders["video_out"] = lambda i: video_out(i, codec_mime=mime,
                                                    **video_out_kwargs)
    if video_in_kwargs is not None:
        builders["video_in"] = lambda i: video_in(i, codec_mime=mime,
                                                  **video_in_kwargs)
    if g2g_kwargs is not None:
        builders["g2g"] = lambda i: g2g(i, **g2g_kwargs)

    snaps = build_snapshots(polls, builders=builders, warmup_polls=warmup_polls)

    meta = None
    if meta_kwargs is not None:
        meta = run_metadata(window_start_us=window_start, window_end_us=window_end,
                            seq_published=200 * (polls - 1 - warmup_polls),
                            **meta_kwargs)
    write_snapshots(run_id, snaps, meta)

    rec = run_record(run_id=run_id, seq_log=seq_path, **record_kwargs)
    return rec


# ---------------------------------------------------------------------------
# The fixtures
# ---------------------------------------------------------------------------


def clean_pass(run_id: str, repeat: int, *, codec: str = "h264",
               bitrate_bps: float = 4_000_000, fps: float = 30.0,
               suite: str = "T1_video_floor", **overrides) -> dict:
    """A healthy run that clears every blocking threshold.

    Round numbers throughout so every expected value is hand-computable:
    4 Mbps wire rate, 30 fps, 200 Hz control fully delivered, 30 ms RTT,
    12 ms jitter buffer, 3 ms decode.
    """
    rk = dict(suite=suite, cell_id=f"video_codec={codec}", repeat=repeat,
              codec=codec)
    rk.update(overrides.pop("record_kwargs", {}))
    return make_run(
        run_id,
        record_kwargs=rk,
        video_out_kwargs=dict(bitrate_bps=bitrate_bps, fps=fps, w=1920, h=1080,
                              encoder_impl="OpenH264", encode_ms_per_frame=4.0,
                              pli_at=(5, 12), kf_at=(7, 15)),
        video_in_kwargs=dict(fps=fps, w=1920, h=1080, jb_ms_per_frame=12.0,
                             decode_ms=3.0, assembly_ms=1.0, processing_ms=5.0),
        control_kwargs=dict(rate_hz=200, delivered_per_poll=200, owd_ms=20.0),
        probe_kwargs=dict(rtt_ms=30.0, theta_ms=0.0, confidence="probe",
                          probes_per_poll=2),
        g2g_kwargs=dict(latency_ms=60.0, fps=fps),
        meta_kwargs=dict(requested_codec=codec, negotiated_codec=codec,
                         buffering_mode="zero_jitter", encoder_tier="sw",
                         encoder_impl="OpenH264"),
        **overrides)


def build_all() -> None:
    RUNS.mkdir(parents=True, exist_ok=True)
    by_suite: dict[str, list[dict]] = {}

    def add(rec: dict) -> None:
        by_suite.setdefault(rec["suite"], []).append(rec)

    # --- 1. Clean PASS, three repeats. -------------------------------------
    for r in range(3):
        add(clean_pass(f"pass_h264_r{r}", r))

    # --- 2. FAIL: bitrate over the 5 Mbps ceiling, fps under 27. -----------
    # 6 Mbps wire rate and 24 fps: two blocking breaches, nothing invalidating.
    for r in range(3):
        add(make_run(
            f"fail_bitrate_r{r}",
            record_kwargs=dict(suite="T1_video_floor",
                               cell_id="video_codec=h264,video_profile=target",
                               repeat=r, codec="h264", profile="target",
                               w=1600, h=1300),
            video_out_kwargs=dict(bitrate_bps=6_000_000, fps=24.0, w=1600, h=1300,
                                  encoder_impl="OpenH264", encode_ms_per_frame=6.0,
                                  bw_s_per_poll=0.4),
            video_in_kwargs=dict(fps=24.0, w=1600, h=1300, jb_ms_per_frame=12.0),
            control_kwargs=dict(rate_hz=200, delivered_per_poll=200, owd_ms=20.0),
            probe_kwargs=dict(rtt_ms=30.0, theta_ms=0.0, confidence="probe"),
            g2g_kwargs=dict(latency_ms=70.0, fps=24.0),
            meta_kwargs=dict(requested_codec="h264", negotiated_codec="h264",
                             buffering_mode="zero_jitter",
                             encoder_impl="OpenH264")))

    # --- 3. INVALID: codec fallback (requested av1, negotiated vp9). -------
    # AV1 has no fallback path in the harness, so this represents the general
    # requested != negotiated condition the schema exists to catch: a cell
    # labelled av1 that measured a different experiment.
    add(make_run(
        "invalid_codec_fallback_r0",
        record_kwargs=dict(suite="Q7_latency_definition", cell_id="video_codec=av1",
                           repeat=0, codec="av1"),
        video_out_kwargs=dict(bitrate_bps=3_000_000, fps=30.0, w=1920, h=1080,
                              encoder_impl="libvpx"),
        video_in_kwargs=dict(fps=30.0, w=1920, h=1080),
        control_kwargs=dict(rate_hz=200, delivered_per_poll=200),
        probe_kwargs=dict(rtt_ms=30.0, theta_ms=0.0, confidence="probe"),
        g2g_kwargs=dict(latency_ms=60.0, fps=30.0),
        meta_kwargs=dict(requested_codec="av1", negotiated_codec="vp9",
                         buffering_mode="zero_jitter",
                         encoder_impl="libvpx")))
    # The mime type must agree with the negotiation, or the fixture would be
    # testing the metadata path alone.
    _rewrite_codec_mime("invalid_codec_fallback_r0", "video/VP9")

    # --- 4. INVALID: malformed AV1 bitstream. -----------------------------
    # frames_encoded > 0 while packets_sent == 0. Presents as zero bitrate, and
    # must never be scored as a zero-bitrate FAIL.
    add(make_run(
        "invalid_malformed_av1_r0",
        record_kwargs=dict(suite="T1_video_floor", cell_id="video_codec=av1",
                           repeat=0, codec="av1"),
        video_out_kwargs=dict(bitrate_bps=0.0, fps=30.0, w=1920, h=1080,
                              packets=False, encoder_impl="libaom"),
        video_in_kwargs=dict(fps=0.0, w=0, h=0),
        control_kwargs=dict(rate_hz=200, delivered_per_poll=200),
        probe_kwargs=dict(rtt_ms=30.0, theta_ms=0.0, confidence="probe"),
        g2g_kwargs=dict(latency_ms=60.0, fps=30.0),
        meta_kwargs=dict(requested_codec="av1", negotiated_codec="av1",
                         buffering_mode="zero_jitter",
                         encoder_impl="libaom")))

    # --- 5. INVALID: CPU-limited software AV1. ----------------------------
    # 0.5 s of cpu limitation per 1 s poll = 50%, well over the 10% gate. The
    # bitrate looks like a pass; a bitrate produced by a starved encoder is not
    # a measurement of the network.
    add(make_run(
        "invalid_cpu_limited_av1_r0",
        record_kwargs=dict(suite="T1_video_floor",
                           cell_id="video_codec=av1,video_profile=tolerable",
                           repeat=0, codec="av1"),
        video_out_kwargs=dict(bitrate_bps=2_500_000, fps=30.0, w=1920, h=1080,
                              cpu_s_per_poll=0.5, encoder_impl="libaom",
                              encode_ms_per_frame=25.0),
        video_in_kwargs=dict(fps=30.0, w=1920, h=1080),
        control_kwargs=dict(rate_hz=200, delivered_per_poll=200),
        probe_kwargs=dict(rtt_ms=30.0, theta_ms=0.0, confidence="probe"),
        g2g_kwargs=dict(latency_ms=80.0, fps=30.0),
        meta_kwargs=dict(requested_codec="av1", negotiated_codec="av1",
                         buffering_mode="zero_jitter",
                         encoder_impl="libaom")))

    # --- 6. INVALID: zero G2G metadata coverage. --------------------------
    # subscribe_timing_events() called after NativeVideoStream was constructed:
    # frames arrive, decode and render normally and every other signal is green,
    # but metadata.user_timestamp is None on every frame.
    add(make_run(
        "invalid_g2g_metadata_r0",
        record_kwargs=dict(suite="Q7_latency_definition", cell_id="video_codec=h264",
                           repeat=0, codec="h264", g2g_coverage=0.0),
        video_out_kwargs=dict(bitrate_bps=4_000_000, fps=30.0, w=1920, h=1080,
                              encoder_impl="OpenH264"),
        video_in_kwargs=dict(fps=30.0, w=1920, h=1080),
        control_kwargs=dict(rate_hz=200, delivered_per_poll=200),
        probe_kwargs=dict(rtt_ms=30.0, theta_ms=0.0, confidence="probe"),
        g2g_kwargs=dict(latency_ms=60.0, fps=30.0, without_timestamp_frac=1.0),
        meta_kwargs=dict(requested_codec="h264", negotiated_codec="h264",
                         buffering_mode="zero_jitter",
                         encoder_impl="OpenH264")))

    # --- 7. INVALID: poll overbudget. -------------------------------------
    # Every scored poll overbudget: the client stalled, so the run measured the
    # client and not the network.
    add(make_run(
        "invalid_poll_overbudget_r0",
        record_kwargs=dict(suite="T4_capacity", cell_id="concurrency=50",
                           repeat=0, codec="h264", concurrency=50),
        video_out_kwargs=dict(bitrate_bps=4_000_000, fps=30.0, w=1920, h=1080,
                              encoder_impl="OpenH264"),
        video_in_kwargs=dict(fps=30.0, w=1920, h=1080),
        control_kwargs=dict(rate_hz=200, delivered_per_poll=200),
        probe_kwargs=dict(rtt_ms=30.0, theta_ms=0.0, confidence="probe"),
        g2g_kwargs=dict(latency_ms=60.0, fps=30.0),
        sampler_kwargs=dict(overbudget_polls=100, interval_ms=2000.0),
        meta_kwargs=dict(requested_codec="h264", negotiated_codec="h264",
                         buffering_mode="zero_jitter",
                         encoder_impl="OpenH264")))

    # --- 8. INVALID: incomplete run (no run_metadata record). -------------
    add(make_run(
        "invalid_incomplete_r0",
        record_kwargs=dict(suite="T5_availability", cell_id="fault_injection=fade_burst",
                           repeat=0, codec="h264", fault="fade_burst"),
        video_out_kwargs=dict(bitrate_bps=4_000_000, fps=30.0, w=1920, h=1080,
                              encoder_impl="OpenH264"),
        video_in_kwargs=dict(fps=30.0, w=1920, h=1080),
        control_kwargs=dict(rate_hz=200, delivered_per_poll=200),
        probe_kwargs=dict(rtt_ms=30.0, theta_ms=0.0, confidence="probe"),
        g2g_kwargs=dict(latency_ms=60.0, fps=30.0),
        meta_kwargs=None, with_seq_log=False))

    # --- 9. Silent audio source: audio columns only, run stays valid. -----
    add(make_run(
        "audio_silent_r0",
        record_kwargs=dict(suite="T4_capacity", cell_id="concurrency=1",
                           repeat=0, codec="h264"),
        video_out_kwargs=dict(bitrate_bps=4_000_000, fps=30.0, w=1920, h=1080,
                              encoder_impl="OpenH264"),
        video_in_kwargs=dict(fps=30.0, w=1920, h=1080),
        control_kwargs=dict(rate_hz=200, delivered_per_poll=200),
        probe_kwargs=dict(rtt_ms=30.0, theta_ms=0.0, confidence="probe"),
        audio_in_kwargs=dict(level=0.0),
        g2g_kwargs=dict(latency_ms=60.0, fps=30.0),
        meta_kwargs=dict(requested_codec="h264", negotiated_codec="h264",
                         buffering_mode="zero_jitter",
                         encoder_impl="OpenH264")))

    # --- 9b. No clock sync: run-level INVALID vs columns-suppressed. -------
    # Same condition, different suites. Whether it invalidates the run depends
    # entirely on whether the suite's own primary metrics need a clock offset —
    # which is computed from matrix.yaml, not from a suite list. T-1 carries
    # g2g_p50_ms and T-2/T-4 carry control_late_pct, all blocking; T-5 and V-0
    # carry no theta-gated primary metric and survive with the columns empty.
    for suite, cell, extra in (
            ("T1_video_floor", "video_codec=h264,video_profile=tolerable", {}),
            ("T2_loss_collapse", "loss_pct=0,control_transport=data_track_buf1",
             dict(shaping_applied=True, video_poll_hz=10.0, path="lan")),
            ("T3_jitter_tolerance", "jitter_ms=0,playout_window_ms=10",
             dict(shaping_applied=True, path="lan")),
            ("T4_capacity", "concurrency=1", {}),
            ("Q7_latency_definition", "video_codec=h264,owd_ms=0", {}),
            ("T5_availability", "fault_injection=baseline_soak",
             dict(fault="baseline_soak"))):
        rid = f"noclock_{suite.split('_')[0].lower()}_r0"
        add(make_run(
            rid,
            record_kwargs=dict(suite=suite, cell_id=cell, repeat=0, codec="h264",
                               **extra),
            video_out_kwargs=dict(bitrate_bps=4_000_000, fps=30.0, w=1920, h=1080,
                                  encoder_impl="OpenH264"),
            video_in_kwargs=dict(fps=30.0, w=1920, h=1080),
            # No theta: the one-way and G2G derivations have no offset to apply.
            control_kwargs=dict(rate_hz=200, delivered_per_poll=200,
                                clock_valid=False),
            probe_kwargs=dict(rtt_ms=30.0, theta_ms=None, confidence="none"),
            g2g_kwargs=dict(latency_ms=60.0, fps=30.0, clock_valid=False),
            meta_kwargs=dict(requested_codec="h264", negotiated_codec="h264",
                             buffering_mode="zero_jitter",
                             encoder_impl="OpenH264")))

    # --- 9c. A reordering path: packets_lost goes BACKWARDS at one poll. ---
    # The cumulative counter is revised downward on a duplicate, so the interval
    # delta is negative. It is clamped at zero for the ratio and surfaced in the
    # validity appendix, because the resulting loss figure is a lower bound.
    add(make_run(
        "reorder_clamp_r0",
        record_kwargs=dict(suite="T2_loss_collapse",
                           cell_id="loss_pct=1,control_transport=dc_lossy",
                           repeat=0, codec="h264", loss_pct=1.0,
                           transport_mode="dc_lossy", shaping_applied=True,
                           video_poll_hz=10.0, path="lan"),
        video_out_kwargs=dict(bitrate_bps=4_000_000, fps=30.0, w=1920, h=1080,
                              encoder_impl="OpenH264"),
        video_in_kwargs=dict(fps=30.0, w=1920, h=1080, lost_per_poll=3,
                             clamp_at=10),
        control_kwargs=dict(rate_hz=200, delivered_per_poll=200),
        probe_kwargs=dict(rtt_ms=30.0, theta_ms=0.0, confidence="probe"),
        g2g_kwargs=dict(latency_ms=60.0, fps=30.0),
        meta_kwargs=dict(requested_codec="h264", negotiated_codec="h264",
                         buffering_mode="zero_jitter",
                         encoder_impl="OpenH264")))

    # --- 10. Jitter-buffer differencing, known answer. --------------------
    # Replaces the retired V0 units-gate fixtures. Those existed to exercise the
    # 400-2000 ms smooth cell against the 10x-separated units prediction; with the
    # hint modes retired there is no such cell. What must NOT be lost is coverage
    # of the Δratio itself: jitter_buffer_delay_avg_ms is a ratio of two CUMULATIVE
    # counters and the extractor must difference both, not read the last value.
    # jb_ms_per_frame is the per-frame contribution, so the correct extracted
    # value is exactly jb_ms_per_frame regardless of run length — a fixture that
    # fails to difference reports a number that grows with duration instead.
    for r in range(3):
        add(make_run(
            f"jb_zero_jitter_r{r}",
            record_kwargs=dict(suite="Q7_latency_definition",
                               cell_id="video_codec=h264,owd_ms=25",
                               repeat=r, codec="h264", owd_ms=25.0),
            video_out_kwargs=dict(bitrate_bps=4_000_000, fps=30.0, w=1920, h=1080,
                                  encoder_impl="OpenH264"),
            video_in_kwargs=dict(fps=30.0, w=1920, h=1080, jb_ms_per_frame=3.0,
                                 target_jb_ms_per_frame=26.0),
            control_kwargs=dict(rate_hz=200, delivered_per_poll=200),
            probe_kwargs=dict(rtt_ms=30.0, theta_ms=0.0, confidence="probe"),
            g2g_kwargs=dict(latency_ms=55.0, fps=30.0),
            meta_kwargs=dict(requested_codec="h264", negotiated_codec="h264",
                             buffering_mode="zero_jitter",
                             encoder_impl="OpenH264")))

    # --- 11. T-2 loss sweep: a breakpoint with a clean crossing. ----------
    # Delivered % holds at 100 through 2% loss and collapses at 5%. Three repeats
    # agree, so the breakpoint is a POINT.
    delivered_by_loss = {0.0: 200, 0.5: 200, 1.0: 200, 2.0: 200, 5.0: 150, 10.0: 60}
    for loss, per_poll in delivered_by_loss.items():
        for r in range(3):
            rid = f"t2_loss{loss}_r{r}".replace(".", "p")
            add(make_run(
                rid,
                record_kwargs=dict(suite="T2_loss_collapse",
                                   cell_id=f"loss_pct={loss},control_transport=data_track_buf1",
                                   repeat=r, codec="h264", loss_pct=loss,
                                   shaping_applied=True, video_poll_hz=10.0,
                                   path="lan"),
                video_out_kwargs=dict(bitrate_bps=4_000_000, fps=30.0, w=1920, h=1080,
                                      encoder_impl="OpenH264",
                                      pli_at=(4, 9), kf_at=(6, 11)),
                video_in_kwargs=dict(fps=30.0, w=1920, h=1080,
                                     lost_per_poll=int(loss * 3),
                                     rtx_per_poll=int(loss)),
                control_kwargs=dict(rate_hz=200, delivered_per_poll=per_poll,
                                    late_per_poll=0 if per_poll == 200 else 2,
                                    gap_p99=1 if per_poll == 200 else 5,
                                    max_gap=1 if per_poll == 200 else 9),
                probe_kwargs=dict(rtt_ms=30.0, theta_ms=0.0, confidence="probe"),
                g2g_kwargs=dict(latency_ms=60.0, fps=30.0),
                meta_kwargs=dict(requested_codec="h264", negotiated_codec="h264",
                                 buffering_mode="zero_jitter",
                                 encoder_impl="OpenH264")))

    # --- 11b. T-3 jitter x playout window: the answer is a CURVE. ----------
    # A wider playout deadline tolerates more jitter, so the crossing step moves
    # along the window axis. late % rises with jitter and falls with the window;
    # the 0.1% bar is crossed at 10 ms of jitter under a 5 ms window but only at
    # 40 ms under a 20 ms window. One breakpoint per window, never one number.
    late_by_cell = {
        (5, 0): 0.0, (5, 2): 0.0, (5, 5): 0.05, (5, 10): 2.0, (5, 20): 6.0, (5, 40): 12.0,
        (10, 0): 0.0, (10, 2): 0.0, (10, 5): 0.0, (10, 10): 0.05, (10, 20): 3.0, (10, 40): 9.0,
        (20, 0): 0.0, (20, 2): 0.0, (20, 5): 0.0, (20, 10): 0.0, (20, 20): 0.05, (20, 40): 4.0,
    }
    for (window, jit), late_pct in late_by_cell.items():
        for r in range(3):
            # late_per_poll is out of 200 received per poll, so the share is
            # late_per_poll / 200 * 100 = late_pct.
            late_per_poll = round(late_pct * 200 / 100)
            add(make_run(
                f"t3_w{window}_j{jit}_r{r}",
                record_kwargs=dict(suite="T3_jitter_tolerance",
                                   cell_id=f"jitter_ms={jit},playout_window_ms={window},"
                                           "control_transport=data_track_buf1",
                                   repeat=r, codec="h264", jitter_ms=jit,
                                   playout_window_ms=window, owd_ms=25.0,
                                   shaping_applied=True, path="lan"),
                video_out_kwargs=dict(bitrate_bps=4_000_000, fps=30.0, w=1920, h=1080,
                                      encoder_impl="OpenH264"),
                video_in_kwargs=dict(fps=30.0, w=1920, h=1080),
                control_kwargs=dict(rate_hz=200, delivered_per_poll=200,
                                    late_per_poll=late_per_poll,
                                    owd_ms=20.0 + jit / 2.0,
                                    jitter_ms=float(jit)),
                probe_kwargs=dict(rtt_ms=50.0, theta_ms=0.0, confidence="probe"),
                g2g_kwargs=dict(latency_ms=60.0, fps=30.0),
                meta_kwargs=dict(requested_codec="h264", negotiated_codec="h264",
                                 buffering_mode="zero_jitter",
                                 encoder_impl="OpenH264")))

    # --- 12. T-4 sweep where the repeats DISAGREE on the crossing step. ---
    # rtt p95 crosses 90 ms at 25 sessions in repeat 0 and at 50 in repeats 1-2,
    # so the breakpoint must be reported as the range 25-50, never as a point.
    rtt_by_cell = {
        (10, 0): 40.0, (10, 1): 40.0, (10, 2): 40.0,
        (25, 0): 95.0, (25, 1): 70.0, (25, 2): 72.0,
        (50, 0): 120.0, (50, 1): 110.0, (50, 2): 115.0,
    }
    for (conc, r), rtt in rtt_by_cell.items():
        add(make_run(
            f"t4_c{conc}_r{r}",
            record_kwargs=dict(suite="T4_capacity", cell_id=f"concurrency={conc}",
                               repeat=r, codec="h264", concurrency=conc,
                               shaping_applied=True, path="lan"),
            video_out_kwargs=dict(bitrate_bps=4_000_000, fps=30.0, w=1920, h=1080,
                                  encoder_impl="OpenH264"),
            video_in_kwargs=dict(fps=30.0, w=1920, h=1080),
            control_kwargs=dict(rate_hz=200, delivered_per_poll=200),
            # The 90 ms bar is scored against the NETWORK round trip, so the
            # crossing has to be driven there. The probe's application loop runs
            # above it, as it does in real data — it contains the same network
            # traversal plus scheduling at both ends.
            transport_kwargs=dict(rtt_ms=rtt),
            probe_kwargs=dict(rtt_ms=rtt * 2.0, theta_ms=0.0, confidence="probe",
                              probes_per_poll=2),
            g2g_kwargs=dict(latency_ms=60.0, fps=30.0),
            meta_kwargs=dict(requested_codec="h264", negotiated_codec="h264",
                             buffering_mode="zero_jitter",
                             encoder_impl="OpenH264")))

    # --- 13. Q-7 per codec. Every cell is zero_jitter now, so the codec IS the
    # cell: the mandatory av1 cell is the AV1 decode/assembly share of G2G under
    # no jitter buffer. AV1 carries a visibly larger decode and assembly cost,
    # which is the whole point of the comparison.
    for codec, g2g_ms, dec_ms, asm_ms, jb_ms in (
            ("h264", 60.0, 3.0, 1.0, 0.0),
            ("av1", 74.0, 9.0, 7.0, 0.0)):
        buffering = "zero_jitter"
        for r in range(3):
            rid = f"q7_{codec}_{buffering}_r{r}"
            add(make_run(
                rid,
                record_kwargs=dict(suite="Q7_latency_definition",
                                   cell_id=f"video_codec={codec}",
                                   repeat=r, codec=codec, buffering=buffering),
                video_out_kwargs=dict(bitrate_bps=3_500_000, fps=30.0, w=1920, h=1080,
                                      encoder_impl="libaom" if codec == "av1"
                                      else "OpenH264",
                                      encode_ms_per_frame=8.0 if codec == "av1" else 4.0),
                video_in_kwargs=dict(fps=30.0, w=1920, h=1080, decode_ms=dec_ms,
                                     assembly_ms=asm_ms,
                                     jb_ms_per_frame=jb_ms,
                                     target_jb_ms_per_frame=jb_ms),
                control_kwargs=dict(rate_hz=200, delivered_per_poll=200, owd_ms=20.0),
                probe_kwargs=dict(rtt_ms=30.0, theta_ms=0.0, confidence="probe"),
                g2g_kwargs=dict(latency_ms=g2g_ms, fps=30.0),
                meta_kwargs=dict(requested_codec=codec, negotiated_codec=codec,
                                 buffering_mode=buffering,
                                 encoder_tier="sw" if codec == "av1" else "videotoolbox",
                                 encoder_impl="libaom" if codec == "av1"
                                 else "OpenH264")))

    for suite, recs in by_suite.items():
        with open(RUNS / f"{suite}.jsonl", "w") as f:
            for rec in recs:
                f.write(json.dumps(rec) + "\n")
        print(f"{suite}.jsonl: {len(recs)} runs")


def _rewrite_codec_mime(run_id: str, mime: str) -> None:
    """Sets the mime type on an already-written snapshot file.

    Used only by the codec-fallback fixture, where the negotiated codec must
    differ from the requested one in the codec stat as well as the metadata.
    """
    path = SNAPSHOTS / f"{run_id}.jsonl"
    lines = []
    for line in path.read_text().splitlines():
        rec = json.loads(line)
        if rec.get("record") != "run_metadata":
            for sec in ("video_out", "video_in"):
                if sec in rec:
                    rec[sec]["codec_mime_type"] = mime
        lines.append(json.dumps(rec))
    path.write_text("\n".join(lines) + "\n")


if __name__ == "__main__":
    build_all()
    print(f"fixtures written under {HERE}", file=sys.stderr)
