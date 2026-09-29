//! The per-poll snapshot record.
//!
//! One typed struct, serialized as one JSON object per line. Fields are raw or lightly
//! normalized: no thresholds, no derived percentages, no verdict. Differencing and
//! scoring happen in Python so that a threshold change never requires a rebuild, and so
//! that a bug in the differencing is fixable without re-running the matrix.

use serde::Serialize;

use crate::counters::ClampedDelta;

/// A single stats poll, written as one JSON line.
#[derive(Debug, Clone, Serialize)]
pub struct Snapshot {
    /// Sequential poll index within the run, starting at zero.
    pub poll_index: u64,
    /// Wall-clock time of the poll. Comparable across hosts only after clock correction.
    pub t_unix_us: u64,
    /// Monotonic microseconds since run start. The only safe basis for an interval.
    pub t_monotonic_us: u64,
    /// Whether this poll falls inside the post-warmup scored window.
    pub scored: bool,

    /// Sampler self-accounting. A sampler that silently exceeds its own interval
    /// invalidates every rate it reports.
    pub sampler: SamplerHealth,

    /// Video send-side reading, absent until the track is published and encoding.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub video_out: Option<VideoOutbound>,
    /// Video receive-side reading, absent until a remote video track is subscribed.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub video_in: Option<VideoInbound>,
    /// Audio send-side reading.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub audio_out: Option<AudioOutbound>,
    /// Audio receive-side reading.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub audio_in: Option<AudioInbound>,
    /// Playout-side audio reading, independent of the video jitter buffer.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub audio_playout: Option<AudioPlayout>,
    /// Transport and candidate-pair reading.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub transport: Option<TransportSample>,
    /// Legacy data-channel counters. Absent on the data-track path, which surfaces no
    /// stats at all — that is why the control payload carries its own sequence number.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub data_channel: Option<DataChannelSample>,
    /// Application-derived control-path reading for this interval.
    pub control: ControlSample,
    /// Application-derived probe and clock-offset reading.
    pub probe: ProbeSample,
    /// Application-derived glass-to-glass reading, from in-band frame metadata.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub g2g: Option<G2gSample>,
}

/// Sampler cadence accounting for one poll.
#[derive(Debug, Clone, Serialize)]
pub struct SamplerHealth {
    /// Nominal spacing this sampler was configured for.
    pub nominal_interval_ms: f64,
    /// Measured spacing since the previous poll, on the monotonic clock.
    pub actual_interval_ms: f64,
    /// How long the poll's own work took, excluding the wait.
    pub poll_duration_ms: f64,
    /// True when `actual_interval_ms` exceeded the configured multiple of nominal.
    pub overbudget: bool,
    /// Running count of overbudget polls, including this one.
    pub overbudget_count: u64,
    /// Running count of polls attempted.
    pub polls_total: u64,
    /// True when a stats RPC failed during this poll, as opposed to returning no stats.
    /// An empty section is ambiguous on its own — it could mean "not subscribed yet" —
    /// so a persistent failure has to be visible rather than silently thinning the data.
    pub stats_rpc_failed: bool,
    /// Running count of failed stats RPCs across the run.
    pub stats_rpc_failures: u64,
}

/// Raw `OutboundRtp` video fields plus the joined codec identity.
#[derive(Debug, Clone, Serialize)]
pub struct VideoOutbound {
    pub bytes_sent: u64,
    pub header_bytes_sent: u64,
    pub packets_sent: u64,
    pub retransmitted_packets_sent: u64,
    pub frames_encoded: u32,
    pub key_frames_encoded: u32,
    pub frames_sent: u32,
    pub frames_per_second: f64,
    pub frame_width: u32,
    pub frame_height: u32,
    pub total_encode_time_s: f64,
    pub target_bitrate_bps: f64,
    pub qp_sum: u64,
    pub nack_count: u32,
    pub pli_count: u32,
    pub fir_count: u32,
    pub quality_limitation_reason: String,
    pub quality_limitation_cpu_s: f64,
    pub quality_limitation_bandwidth_s: f64,
    pub quality_limitation_other_s: f64,
    pub quality_limitation_none_s: f64,
    pub quality_limitation_resolution_changes: u32,
    /// The encoder libwebrtc actually selected, not the backend requested.
    pub encoder_implementation: String,
    /// libwebrtc's own hardware/software signal, corroborating the encoder tier.
    pub power_efficient_encoder: bool,
    /// True when the encoder produced frames but nothing reached the wire. For AV1 this
    /// is the malformed-bitstream condition, and it is an invalid run rather than a
    /// zero-bitrate failure.
    pub malformed_bitstream: bool,
    /// MIME type from the joined codec stat, e.g. `video/AV1`.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub codec_mime_type: Option<String>,
}

/// Raw `InboundRtp` video fields plus the joined codec identity.
#[derive(Debug, Clone, Serialize)]
pub struct VideoInbound {
    pub bytes_received: u64,
    pub header_bytes_received: u64,
    pub packets_received: u64,
    /// Cumulative and signed; may be revised downward on reorder or duplicate.
    pub packets_lost: i64,
    /// Interval delta of `packets_lost`, clamped at zero.
    pub packets_lost_delta: i64,
    /// Present only when the interval delta was negative before clamping. A reorder
    /// artifact is not a gain, and hiding the clamp in a log line loses the evidence.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub packets_lost_clamped_from: Option<i64>,
    pub retransmitted_packets_received: u64,
    pub frames_decoded: u32,
    pub key_frames_decoded: u32,
    pub frames_dropped: u32,
    pub frames_received: u64,
    pub frames_per_second: f64,
    pub frame_width: u32,
    pub frame_height: u32,
    pub freeze_count: u32,
    pub total_freeze_duration_s: f64,
    pub pause_count: u32,
    pub total_pause_duration_s: f64,
    pub jitter_s: f64,
    pub jitter_buffer_delay_s: f64,
    pub jitter_buffer_target_delay_s: f64,
    pub jitter_buffer_minimum_delay_s: f64,
    pub jitter_buffer_emitted_count: u64,
    pub total_decode_time_s: f64,
    pub total_processing_delay_s: f64,
    pub total_assembly_time_s: f64,
    pub frames_assembled_from_multiple_packets: u64,
    pub total_inter_frame_delay_s: f64,
    pub nack_count: u32,
    pub pli_count: u32,
    pub qp_sum: u64,
    pub decoder_implementation: String,
    pub power_efficient_decoder: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub codec_mime_type: Option<String>,
    /// Wall-clock spacing between decoded frames observed by the harness during this
    /// interval. `total_inter_frame_delay` gives a mean but not a tail, and a freeze is
    /// a tail event.
    pub frame_arrival_intervals_ms: Vec<f64>,
}

/// Raw audio `OutboundRtp` fields.
#[derive(Debug, Clone, Serialize)]
pub struct AudioOutbound {
    pub bytes_sent: u64,
    pub header_bytes_sent: u64,
    pub packets_sent: u64,
    pub target_bitrate_bps: f64,
}

/// Raw audio `InboundRtp` fields, including the NetEQ concealment counters.
#[derive(Debug, Clone, Serialize)]
pub struct AudioInbound {
    pub bytes_received: u64,
    pub packets_received: u64,
    pub packets_lost: i64,
    pub packets_lost_delta: i64,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub packets_lost_clamped_from: Option<i64>,
    pub jitter_s: f64,
    pub jitter_buffer_delay_s: f64,
    pub jitter_buffer_emitted_count: u64,
    pub total_samples_received: u64,
    pub concealed_samples: u64,
    /// Reported alongside `concealed_samples` so that concealed silence is not counted
    /// as damage.
    pub silent_concealed_samples: u64,
    pub concealment_events: u64,
    pub inserted_samples_for_deceleration: u64,
    pub removed_samples_for_acceleration: u64,
    pub total_samples_duration_s: f64,
    /// Harness health: a silent source makes every concealment metric meaningless.
    pub audio_level: f64,
}

/// Raw `MediaPlayout` fields. Audio has its own jitter buffer, unaffected by the video
/// playout-delay mechanisms, so this must be measured rather than inferred.
#[derive(Debug, Clone, Serialize)]
pub struct AudioPlayout {
    pub total_playout_delay_s: f64,
    pub total_samples_count: u64,
    pub synthesized_samples_duration_s: f64,
    pub synthesized_samples_events: u32,
    pub total_samples_duration_s: f64,
}

/// Raw `Transport` and selected-`CandidatePair` fields.
#[derive(Debug, Clone, Serialize)]
pub struct TransportSample {
    #[serde(skip_serializing_if = "Option::is_none")]
    pub ice_state: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub dtls_state: Option<String>,
    pub selected_candidate_pair_changes: u32,
    pub packets_sent: u64,
    pub packets_received: u64,
    /// STUN-consent round trip. Corroborates the harness probe; it is not the media-path
    /// round trip and is not scored against the latency bar.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub candidate_pair_rtt_s: Option<f64>,
    /// Send-side bandwidth estimate of the **subscriber's** peer connection.
    ///
    /// NOT the publisher's uplink estimate, and not usable as one. This whole
    /// [`TransportSample`] is read from the subscriber PC, which receives media and sends
    /// only RTCP — measured on Q7 av1 r0, `packets_received` 46 190 against
    /// `packets_sent` 22 743, while the publisher pushed 4.5 MB of video that never
    /// crossed this transport at all.
    ///
    /// With almost nothing to send, that estimator never ramps off libwebrtc's default
    /// start bitrate and reports a constant 300 000 — `kDefaultStartBitrateBps` in
    /// `api/transport/bitrate_settings.h`. Every scored poll of both the av1 and h264 runs
    /// read exactly that, while H264 was simultaneously reporting
    /// `quality_limitation_reason: bandwidth`, so the real constraint was plainly moving
    /// while this number did not.
    ///
    /// The field is correct for what it measures; it was the name that invited it to be
    /// read as an uplink estimate. For publisher-side bandwidth pressure use
    /// `quality_limitation_bandwidth_pct` and `target_bitrate_bps`, which come from
    /// `OutboundRtp` on the publisher.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub subscriber_available_outgoing_bitrate_bps: Option<f64>,
    /// RTCP-derived round trip on the video path, at roughly 1 Hz. Second corroborator.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub rtcp_rtt_s: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub rtcp_rtt_measurements: Option<u64>,
}

/// Legacy SCTP data-channel counters, for the two data-channel control transports only.
#[derive(Debug, Clone, Serialize)]
pub struct DataChannelSample {
    pub label: String,
    pub messages_sent: u32,
    pub messages_received: u32,
    pub bytes_sent: u64,
    pub bytes_received: u64,
}

/// Application-derived control-path reading for this interval.
#[derive(Debug, Clone, Serialize)]
pub struct ControlSample {
    /// Sequence numbers published since run start.
    pub seq_published: u64,
    /// Distinct sequence numbers received since run start.
    pub distinct_seq_received: u64,
    /// Distinct sequence numbers received during this interval alone. The collapse
    /// signature under loss is a rate collapse, which a delivered percentage hides.
    pub distinct_seq_received_interval: u64,
    /// Highest sequence number seen so far.
    pub max_seq_received: u64,
    /// Samples received out of order during this interval.
    pub reordered_interval: u64,
    /// Duplicate sequence numbers seen during this interval.
    pub duplicates_interval: u64,
    /// Largest run of consecutive missing sequence numbers observed so far.
    pub max_gap: u64,
    /// Every gap run length observed so far. Carried in full because a p99 cannot be
    /// reconstructed from a maximum, and the two suites that score this metric need the
    /// typical gap rather than the worst one.
    pub gap_lengths: Vec<u64>,
    /// Nearest-rank p99 of `gap_lengths`, or null when no gap has occurred.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub gap_p99: Option<u64>,
    /// Raw one-way delays observed this interval, uncorrected, in microseconds. Signed
    /// because the two clocks are unrelated until theta is applied.
    pub owd_raw_us_interval: Vec<i64>,
    /// RFC 3550 interarrival jitter over receive spacing. Single-clock, so skew-immune.
    pub jitter_ms: f64,
    /// Samples whose corrected one-way delay exceeded the playout window, cumulative.
    /// Null when no window was configured or the clock offset is not yet valid.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub late_count: Option<u64>,
    /// Samples measured against the playout window, cumulative. The denominator for the
    /// late share.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub late_eligible_count: Option<u64>,
}

/// Application-derived probe reading.
#[derive(Debug, Clone, Serialize)]
pub struct ProbeSample {
    pub probes_sent: u64,
    pub probes_completed: u64,
    /// Probes retired unanswered after outliving the probe lifetime, cumulative.
    ///
    /// The explicit count, because `sent - completed` also counts every probe still in
    /// flight. At a probe interval below the round trip that is several probes at any
    /// instant, so the two forms are not interchangeable.
    pub probes_lost: u64,
    /// Probes issued but not yet answered or aged out, at this instant.
    pub probes_in_flight: u64,
    /// Round-trip measurements completed during this interval, in microseconds.
    pub rtt_us_interval: Vec<u64>,
    /// Current clock-offset estimate. Null until enough one-way samples accumulate.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub theta_ms: Option<f64>,
    /// `none`, `probe`, or `external`. Gates whether one-way and glass-to-glass figures
    /// may be published at all.
    pub clock_sync_confidence: String,
}

/// Application-derived glass-to-glass reading from in-band frame metadata.
///
/// This is capture to application delivery, not capture to photons: it excludes display
/// and compositor latency. The pixel measurement is a separate, manual, tier-2 procedure.
#[derive(Debug, Clone, Serialize)]
pub struct G2gSample {
    /// Corrected capture-to-delivery latencies observed this interval, in microseconds.
    /// Present only once the clock offset is valid.
    pub latency_us_interval: Vec<i64>,
    /// Frames the capture loop handed to WebRTC, cumulative. Separates "the harness never
    /// generated the frame" from "the frame was lost in transit".
    pub frames_captured: u64,
    /// Distinct frame ids received since run start.
    pub distinct_frame_ids: u64,
    /// Span between the lowest and highest frame id seen, inclusive. With
    /// `distinct_frame_ids` this separates a latency result from vanished frames.
    pub frame_id_span: u64,
    /// Frames whose metadata carried no timestamp, so no latency could be derived.
    pub frames_without_timestamp: u64,
}

impl Snapshot {
    /// Serializes to a single JSON line with a trailing newline.
    pub fn to_jsonl(&self) -> Result<String, serde_json::Error> {
        let mut line = serde_json::to_string(self)?;
        line.push('\n');
        Ok(line)
    }
}

impl VideoInbound {
    /// Applies a differenced `packets_lost` reading, preserving any clamp.
    pub fn set_packets_lost_delta(&mut self, delta: ClampedDelta) {
        self.packets_lost_delta = delta.value;
        self.packets_lost_clamped_from = delta.clamped_from;
    }
}

impl AudioInbound {
    /// Applies a differenced `packets_lost` reading, preserving any clamp.
    pub fn set_packets_lost_delta(&mut self, delta: ClampedDelta) {
        self.packets_lost_delta = delta.value;
        self.packets_lost_clamped_from = delta.clamped_from;
    }
}

/// The capture device or stream a camera run resolved to, and the format it negotiated.
///
/// Recorded separately from the requested geometry because a source is free to downgrade:
/// a request for 1920x1080 served as 1280x720 is a different encoding problem, and nothing
/// downstream can detect that from the requested values alone.
///
/// One shape serves both a local device and an RTSP stream. They are the same claim about a
/// run — that the pixels came from a lens rather than the generator — and `kind`
/// distinguishes them without giving the analysis layer two record shapes for one
/// `never_pool_across` dimension.
#[derive(Debug, Clone, Serialize)]
pub struct CameraDevice {
    /// Which capture path produced the frames: `local_device` or `rtsp`.
    pub kind: &'static str,
    /// The `--camera-source` value as given on the command line, with any RTSP credentials
    /// stripped — the record is committed and shared, and RTSP URLs commonly embed them.
    pub requested: String,
    /// Human-readable device name, or the redacted stream URL. The portable identifier.
    pub device_name: String,
    /// Device index in enumeration order. Positional, so it names a different camera on a
    /// different host — recorded beside the name rather than instead of it. For RTSP this
    /// is the media transport (`tcp` or `udp`), which plays the same role: it is the
    /// host-local detail that changes what the same URL delivers.
    pub device_index: String,
    /// Backend-supplied extra identification, empty when the backend supplies none. For
    /// RTSP, the redacted stream URL.
    pub device_description: String,
    /// Capture width the source negotiated, which may differ from the request.
    pub negotiated_width: u32,
    /// Capture height the source negotiated.
    pub negotiated_height: u32,
    /// Capture frame rate the source negotiated.
    ///
    /// For RTSP this is the rate the decoder was told to emit, not the rate the sensor ran
    /// at: a camera slower than the request has its frames duplicated to reach it. The
    /// Tier 2 Muscat runs ~10 fps at 1080p against a 30 fps matrix, so the two differing is
    /// expected rather than a fault.
    pub negotiated_fps: u32,
    /// Negotiated pixel format, e.g. `YUYV`, `MJPEG` or `yuv420p`. MJPEG adds a decode step
    /// that YUYV does not, which shows up in the capture-to-publish interval.
    pub negotiated_format: String,
}

/// `kind` value for a run that opened a local USB or platform capture device.
pub const CAMERA_KIND_LOCAL_DEVICE: &str = "local_device";

/// `kind` value for a run that ingested an IP camera over RTSP.
pub const CAMERA_KIND_RTSP: &str = "rtsp";

impl From<&crate::camera::CameraIdentity> for CameraDevice {
    fn from(identity: &crate::camera::CameraIdentity) -> Self {
        Self {
            kind: CAMERA_KIND_LOCAL_DEVICE,
            requested: identity.requested.clone(),
            device_name: identity.device_name.clone(),
            device_index: identity.device_index.clone(),
            device_description: identity.device_description.clone(),
            negotiated_width: identity.negotiated_width,
            negotiated_height: identity.negotiated_height,
            negotiated_fps: identity.negotiated_fps,
            negotiated_format: identity.negotiated_format.clone(),
        }
    }
}

impl From<&crate::rtsp::RtspIdentity> for CameraDevice {
    fn from(identity: &crate::rtsp::RtspIdentity) -> Self {
        Self {
            kind: CAMERA_KIND_RTSP,
            requested: identity.requested.clone(),
            device_name: identity.url.clone(),
            device_index: identity.transport.as_str().to_string(),
            device_description: identity.url.clone(),
            negotiated_width: identity.negotiated_width,
            negotiated_height: identity.negotiated_height,
            negotiated_fps: identity.negotiated_fps,
            negotiated_format: identity.negotiated_format.clone(),
        }
    }
}

/// Run-level metadata, emitted once as the final line of the snapshot file.
///
/// These are facts only the harness can know. The runner constructs the rest of the run
/// record, but it cannot observe the harness's own clock origin, the process identity the
/// buffering-mode grouping depends on, or which build produced the data — leaving them
/// null makes the scored window unreconstructable and the process grouping unverifiable.
///
/// It is written last so that a run killed mid-flight still yields readable snapshots; the
/// absence of this record is itself the signal that the run did not complete.
#[derive(Debug, Clone, Serialize)]
pub struct RunMetadata {
    /// Marks this line as metadata rather than a snapshot, so a reader can tell them
    /// apart without positional assumptions.
    pub record: &'static str,

    /// Start of the post-warmup scored window, on the wall clock.
    ///
    /// This is the `control_delivered_pct` denominator boundary and the warmup cutoff.
    /// Only the harness knows its own origin; inferring it from snapshot timestamps would
    /// silently shift the window by however long connection setup took.
    pub scored_window_start_unix_us: u64,
    /// End of the scored window, on the wall clock.
    pub scored_window_end_unix_us: u64,
    /// Monotonic origin of the run, for interval arithmetic that wall time cannot express.
    pub run_origin_unix_us: u64,
    /// Warmup seconds excluded from the scored window.
    pub warmup_excluded_s: u64,

    /// Process that ran the subscriber. The zero-playout-delay field trial is
    /// process-global, so a run at one buffering mode sharing a process with another is
    /// not a run at that mode.
    pub subscriber_process_id: u32,
    /// Process that ran the publisher. Equal to the subscriber's by design: one process
    /// serves one buffering mode, and both ends must sit inside it.
    pub publisher_process_id: u32,

    /// Version of the harness that produced this data.
    pub harness_version: &'static str,
    /// Server build the cell ran against, when the server reported one.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub sfu_version: Option<String>,

    /// Outcome of the playout-delay units gate, when this cell was the discriminator.
    /// Null when the cell was not run at the mode that can settle the question.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub playout_units_confirmed: Option<bool>,

    /// Codec requested on the command line.
    pub requested_codec: String,
    /// Codec actually negotiated, read back from the codec stat. A mismatch invalidates
    /// the run: it measured a different experiment than the one it was asked to.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub negotiated_codec: Option<String>,
    /// Encoder implementation libwebrtc selected.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub encoder_implementation: Option<String>,
    /// Encoder tier classified from the implementation string.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub encoder_tier: Option<String>,

    /// What produced the pixels: `test_pattern`, or the resolved capture device name.
    ///
    /// The *resolved* source, not the requested one, so a run is self-identifying rather
    /// than merely flagged. A non-poolable dimension: a lens makes bitrate depend on scene
    /// content, lighting and framing, so a camera run and a pattern run are different
    /// experiments and an average over both describes neither.
    pub camera_source: String,
    /// The device a camera run resolved to, absent for the synthetic pattern.
    ///
    /// Carries the negotiated geometry rather than the requested one, because a device
    /// that downgraded a 1080p30 request to 720p15 presented the encoder with a different
    /// problem, and without the negotiated values that run is indistinguishable from one
    /// that got what it asked for.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub camera_device: Option<CameraDevice>,

    /// Control samples the publisher emitted over the whole run.
    pub seq_published: u64,
    /// Sends the transport rejected, which are a harness shortfall rather than loss.
    pub send_failures: u64,
    /// Reconnects survived. A survived reconnect is not a session drop.
    pub reconnect_count: u64,
    /// Buffering mode this process served.
    pub buffering_mode: String,
    /// How the room-level playout hint was applied.
    pub playout_delay_applied: String,
    /// Whether every video subscription got its receive-side packet trailer handler.
    ///
    /// False means frames arrived with no capture timestamp for at least one subscription,
    /// so the glass-to-glass series covers only part of the run. Without this the failure
    /// is indistinguishable from a run that carried no video at all.
    pub g2g_timing_handler_installed: bool,
    /// Remote video subscriptions established over the run.
    ///
    /// Greater than one means the session re-subscribed mid-run, which is what a full
    /// reconnect does.
    pub video_subscription_count: u64,
}

impl RunMetadata {
    /// Serializes to a single JSON line with a trailing newline.
    pub fn to_jsonl(&self) -> Result<String, serde_json::Error> {
        let mut line = serde_json::to_string(self)?;
        line.push('\n');
        Ok(line)
    }
}

/// One line of the publisher sequence log.
///
/// This is the `control_delivered_pct` denominator. Deriving the expected count from
/// received sequence numbers is self-referential and biased toward passing: if the last
/// samples of the scored window are lost, the observed maximum drops by exactly the
/// number lost and the loss becomes invisible. Logging what the publisher emitted, with
/// its send time, keeps the denominator fixed while loss shows up in the numerator.
#[derive(Debug, Clone, Serialize)]
pub struct PublishedSeq {
    pub seq: u64,
    pub t_send_unix_us: u64,
    pub t_send_monotonic_us: u64,
    /// True when this sample carried a probe token.
    pub probe: bool,
}

impl PublishedSeq {
    /// Serializes to a single JSON line with a trailing newline.
    pub fn to_jsonl(&self) -> Result<String, serde_json::Error> {
        let mut line = serde_json::to_string(self)?;
        line.push('\n');
        Ok(line)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn minimal_snapshot() -> Snapshot {
        Snapshot {
            poll_index: 3,
            t_unix_us: 1_787_611_950_000_000,
            t_monotonic_us: 3_000_000,
            scored: true,
            sampler: SamplerHealth {
                nominal_interval_ms: 1000.0,
                actual_interval_ms: 1002.5,
                poll_duration_ms: 4.1,
                overbudget: false,
                overbudget_count: 0,
                polls_total: 4,
                stats_rpc_failed: false,
                stats_rpc_failures: 0,
            },
            video_out: None,
            video_in: None,
            audio_out: None,
            audio_in: None,
            audio_playout: None,
            transport: None,
            data_channel: None,
            control: ControlSample {
                seq_published: 600,
                distinct_seq_received: 598,
                distinct_seq_received_interval: 199,
                max_seq_received: 599,
                reordered_interval: 0,
                duplicates_interval: 0,
                max_gap: 2,
                gap_lengths: vec![1, 2],
                gap_p99: Some(2),
                owd_raw_us_interval: vec![15_000, 16_000],
                jitter_ms: 0.4,
                late_count: Some(1),
                late_eligible_count: Some(598),
            },
            probe: ProbeSample {
                probes_sent: 3,
                probes_completed: 3,
                probes_lost: 0,
                probes_in_flight: 0,
                rtt_us_interval: vec![28_000],
                theta_ms: Some(0.25),
                clock_sync_confidence: "probe".to_owned(),
            },
            g2g: None,
        }
    }

    #[test]
    fn snapshot_is_one_line_of_json() {
        let line = minimal_snapshot().to_jsonl().expect("serialize");
        assert_eq!(line.matches('\n').count(), 1);
        assert!(line.ends_with('\n'));
        let parsed: serde_json::Value =
            serde_json::from_str(line.trim_end()).expect("valid json object");
        assert_eq!(parsed["poll_index"], 3);
        assert_eq!(parsed["control"]["distinct_seq_received"], 598);
        assert_eq!(parsed["probe"]["clock_sync_confidence"], "probe");
    }

    /// Absent sections must be omitted rather than emitted as null, so a reader can tell
    /// "not subscribed" from "measured zero".
    #[test]
    fn absent_sections_are_omitted() {
        let line = minimal_snapshot().to_jsonl().expect("serialize");
        let parsed: serde_json::Value = serde_json::from_str(line.trim_end()).expect("json");
        let obj = parsed.as_object().expect("object");
        assert!(!obj.contains_key("video_in"));
        assert!(!obj.contains_key("audio_playout"));
        assert!(obj.contains_key("control"));
    }

    /// The clamp must reach the record, not just a log line.
    #[test]
    fn clamped_loss_delta_is_serialized() {
        let mut inbound = VideoInbound {
            bytes_received: 0,
            header_bytes_received: 0,
            packets_received: 100,
            packets_lost: 7,
            packets_lost_delta: 0,
            packets_lost_clamped_from: None,
            retransmitted_packets_received: 0,
            frames_decoded: 0,
            key_frames_decoded: 0,
            frames_dropped: 0,
            frames_received: 0,
            frames_per_second: 0.0,
            frame_width: 0,
            frame_height: 0,
            freeze_count: 0,
            total_freeze_duration_s: 0.0,
            pause_count: 0,
            total_pause_duration_s: 0.0,
            jitter_s: 0.0,
            jitter_buffer_delay_s: 0.0,
            jitter_buffer_target_delay_s: 0.0,
            jitter_buffer_minimum_delay_s: 0.0,
            jitter_buffer_emitted_count: 0,
            total_decode_time_s: 0.0,
            total_processing_delay_s: 0.0,
            total_assembly_time_s: 0.0,
            frames_assembled_from_multiple_packets: 0,
            total_inter_frame_delay_s: 0.0,
            nack_count: 0,
            pli_count: 0,
            qp_sum: 0,
            decoder_implementation: String::new(),
            power_efficient_decoder: false,
            codec_mime_type: None,
            frame_arrival_intervals_ms: Vec::new(),
        };
        inbound.set_packets_lost_delta(ClampedDelta::between(10, 7));
        let json = serde_json::to_value(&inbound).expect("serialize");
        assert_eq!(json["packets_lost_delta"], 0);
        assert_eq!(json["packets_lost_clamped_from"], -3);
    }

    fn metadata(warmup_s: u64, duration_s: u64, origin_us: u64) -> RunMetadata {
        RunMetadata {
            record: "run_metadata",
            scored_window_start_unix_us: origin_us + warmup_s * 1_000_000,
            scored_window_end_unix_us: origin_us + duration_s * 1_000_000,
            run_origin_unix_us: origin_us,
            warmup_excluded_s: warmup_s,
            subscriber_process_id: 4242,
            publisher_process_id: 4242,
            harness_version: "0.1.0",
            sfu_version: None,
            playout_units_confirmed: None,
            requested_codec: "av1".to_owned(),
            negotiated_codec: Some("av1".to_owned()),
            encoder_implementation: Some("libaom".to_owned()),
            encoder_tier: Some("sw".to_owned()),
            camera_source: crate::cli::TEST_PATTERN_SOURCE.to_owned(),
            camera_device: None,
            seq_published: 24_000,
            send_failures: 0,
            reconnect_count: 0,
            buffering_mode: "zero_jitter".to_owned(),
            playout_delay_applied: "not_requested".to_owned(),
            g2g_timing_handler_installed: true,
            video_subscription_count: 1,
        }
    }

    /// The scored window is the delivered-share denominator boundary. It must exclude
    /// exactly the warmup and be anchored to the harness's own origin — inferring it from
    /// snapshot timestamps would shift it by however long connection setup took.
    #[test]
    fn scored_window_excludes_warmup_and_anchors_to_run_origin() {
        let origin = 1_787_611_950_000_000;
        let m = metadata(15, 120, origin);
        assert_eq!(m.scored_window_start_unix_us, origin + 15_000_000);
        assert_eq!(m.scored_window_end_unix_us, origin + 120_000_000);
        assert_eq!(
            m.scored_window_end_unix_us - m.scored_window_start_unix_us,
            105_000_000,
            "scored window must be duration minus warmup"
        );
    }

    /// A run whose packet trailer handler never installed produces frames with no capture
    /// timestamp. That is indistinguishable from a run with no video once the snapshots
    /// are all that survives, so it has to be stated in the record rather than inferred.
    #[test]
    fn timing_handler_state_and_subscription_count_are_recorded() {
        let mut m = metadata(15, 120, 1_787_611_950_000_000);
        let json = serde_json::to_value(&m).expect("serialize");
        assert_eq!(json["g2g_timing_handler_installed"], true);
        assert_eq!(json["video_subscription_count"], 1);

        // What a run that re-subscribed after a full reconnect and lost the race looks like.
        m.g2g_timing_handler_installed = false;
        m.video_subscription_count = 2;
        let json = serde_json::to_value(&m).expect("serialize");
        assert_eq!(json["g2g_timing_handler_installed"], false);
        assert_eq!(json["video_subscription_count"], 2);
    }

    /// Both ends share one process by construction: the zero-playout-delay field trial is
    /// process-global, so the mode label is only meaningful if they do.
    #[test]
    fn both_process_ids_are_recorded_and_equal() {
        let m = metadata(15, 120, 1_787_611_950_000_000);
        assert_eq!(m.subscriber_process_id, m.publisher_process_id);
        let json = serde_json::to_value(&m).expect("serialize");
        assert_eq!(json["subscriber_process_id"], 4242);
        assert_eq!(json["publisher_process_id"], 4242);
    }

    /// The record must be self-identifying so a reader can tell it from a snapshot
    /// without relying on its position in the file.
    #[test]
    fn run_metadata_is_tagged_and_carries_the_negotiated_codec() {
        let m = metadata(15, 120, 1_787_611_950_000_000);
        let line = m.to_jsonl().expect("serialize");
        let json: serde_json::Value = serde_json::from_str(line.trim_end()).expect("json");
        assert_eq!(json["record"], "run_metadata");
        assert_eq!(json["requested_codec"], "av1");
        assert_eq!(json["negotiated_codec"], "av1");
        assert_eq!(json["encoder_tier"], "sw");
        assert_eq!(json["harness_version"], "0.1.0");
        // Unknown values are omitted rather than emitted as a guess.
        assert!(!json.as_object().expect("object").contains_key("sfu_version"));
    }

    #[test]
    fn published_seq_is_one_line() {
        let line = PublishedSeq {
            seq: 42,
            t_send_unix_us: 1_787_611_950_000_000,
            t_send_monotonic_us: 210_000,
            probe: true,
        }
        .to_jsonl()
        .expect("serialize");
        let parsed: serde_json::Value = serde_json::from_str(line.trim_end()).expect("json");
        assert_eq!(parsed["seq"], 42);
        assert_eq!(parsed["probe"], true);
        assert!(line.ends_with('\n'));
    }
}
