//! Extraction from `Vec<RtcStats>` into the typed snapshot sections.
//!
//! Every value here is copied through raw or converted only by a unit change that the
//! field name records. No ratios and no rates are computed: those need two polls, and
//! differencing is the analysis layer's job so that a defect in it is fixable without
//! re-running the matrix. The one exception is `packets_lost`, which is differenced here
//! because detecting its negative-delta reorder artifact requires the previous reading.

use std::collections::HashMap;

use livekit::webrtc::stats::{QualityLimitationReason, RtcStats};

use crate::counters::ClampedDelta;
use crate::snapshot::{
    AudioInbound, AudioOutbound, AudioPlayout, DataChannelSample, TransportSample, VideoInbound,
    VideoOutbound,
};

/// Media kind as reported in `RtpStreamStats.kind`.
const KIND_VIDEO: &str = "video";
const KIND_AUDIO: &str = "audio";

/// Maps codec stat ids to their MIME types, so a stream can be joined to its codec.
fn codec_index(stats: &[RtcStats]) -> HashMap<String, String> {
    stats
        .iter()
        .filter_map(|stat| match stat {
            RtcStats::Codec(codec) => Some((codec.rtc.id.clone(), codec.codec.mime_type.clone())),
            _ => None,
        })
        .collect()
}

/// Extracts the video send-side reading.
///
/// When several outbound streams are present — which happens whenever simulcast is on,
/// and can happen transiently otherwise — the active layer is preferred and the first
/// non-empty encoder implementation is the fallback. Picking an arbitrary stream reports
/// the wrong layer's encoder and bitrate.
pub fn extract_video_outbound(stats: &[RtcStats]) -> Option<VideoOutbound> {
    let codecs = codec_index(stats);
    let candidates: Vec<_> = stats
        .iter()
        .filter_map(|stat| match stat {
            RtcStats::OutboundRtp(out) if out.stream.kind == KIND_VIDEO => Some(out),
            _ => None,
        })
        .collect();

    let chosen = candidates
        .iter()
        .find(|out| out.outbound.active)
        .or_else(|| candidates.iter().find(|out| !out.outbound.encoder_implementation.is_empty()))
        .or_else(|| candidates.first())?;

    let durations = &chosen.outbound.quality_limitation_durations;
    // The encoder produced frames but nothing reached the wire. For AV1 this is the
    // malformed-bitstream condition, and it invalidates the run rather than failing it as
    // a zero-bitrate result.
    let malformed_bitstream = chosen.outbound.frames_encoded > 0 && chosen.sent.packets_sent == 0;

    Some(VideoOutbound {
        bytes_sent: chosen.sent.bytes_sent,
        header_bytes_sent: chosen.outbound.header_bytes_sent,
        packets_sent: chosen.sent.packets_sent,
        retransmitted_packets_sent: chosen.outbound.retransmitted_packets_sent,
        frames_encoded: chosen.outbound.frames_encoded,
        key_frames_encoded: chosen.outbound.key_frames_encoded,
        frames_sent: chosen.outbound.frames_sent,
        frames_per_second: chosen.outbound.frames_per_second,
        frame_width: chosen.outbound.frame_width,
        frame_height: chosen.outbound.frame_height,
        total_encode_time_s: chosen.outbound.total_encode_time,
        target_bitrate_bps: chosen.outbound.target_bitrate,
        qp_sum: chosen.outbound.qp_sum,
        nack_count: chosen.outbound.nack_count,
        pli_count: chosen.outbound.pli_count,
        fir_count: chosen.outbound.fir_count,
        quality_limitation_reason: quality_limitation_name(
            chosen.outbound.quality_limitation_reason,
        )
        .to_owned(),
        quality_limitation_cpu_s: durations.get("cpu").copied().unwrap_or(0.0),
        quality_limitation_bandwidth_s: durations.get("bandwidth").copied().unwrap_or(0.0),
        quality_limitation_other_s: durations.get("other").copied().unwrap_or(0.0),
        quality_limitation_none_s: durations.get("none").copied().unwrap_or(0.0),
        quality_limitation_resolution_changes: chosen
            .outbound
            .quality_limitation_resolution_changes,
        encoder_implementation: chosen.outbound.encoder_implementation.clone(),
        power_efficient_encoder: chosen.outbound.power_efficient_encoder,
        malformed_bitstream,
        codec_mime_type: codecs.get(&chosen.stream.codec_id).cloned(),
    })
}

/// Lowercase name for the quality limitation reason.
fn quality_limitation_name(reason: QualityLimitationReason) -> &'static str {
    match reason {
        QualityLimitationReason::None => "none",
        QualityLimitationReason::Cpu => "cpu",
        QualityLimitationReason::Bandwidth => "bandwidth",
        QualityLimitationReason::Other => "other",
    }
}

/// Extracts the video receive-side reading.
///
/// `previous_packets_lost` is the prior poll's cumulative reading, used only to difference
/// the loss counter here so that a downward revision can be recognized as a reorder
/// artifact rather than silently becoming a negative rate.
pub fn extract_video_inbound(
    stats: &[RtcStats],
    previous_packets_lost: Option<i64>,
) -> Option<VideoInbound> {
    let codecs = codec_index(stats);
    // Mirrors the send-side selection rule: prefer the stream that is actually decoding.
    // With simulcast off there is one inbound video stream, but picking arbitrarily would
    // silently report the wrong layer the moment that changes.
    let candidates: Vec<_> = stats
        .iter()
        .filter_map(|stat| match stat {
            RtcStats::InboundRtp(inb) if inb.stream.kind == KIND_VIDEO => Some(inb),
            _ => None,
        })
        .collect();
    let inbound = candidates
        .iter()
        .find(|inb| inb.inbound.frames_decoded > 0)
        .or_else(|| candidates.iter().find(|inb| inb.received.packets_received > 0))
        .or_else(|| candidates.first())
        .copied()?;

    let loss_delta = previous_packets_lost
        .map(|previous| ClampedDelta::between(previous, inbound.received.packets_lost))
        .unwrap_or_default();

    Some(VideoInbound {
        bytes_received: inbound.inbound.bytes_received,
        header_bytes_received: inbound.inbound.header_bytes_received,
        packets_received: inbound.received.packets_received,
        packets_lost: inbound.received.packets_lost,
        packets_lost_delta: loss_delta.value,
        packets_lost_clamped_from: loss_delta.clamped_from,
        retransmitted_packets_received: inbound.inbound.retransmitted_packets_received,
        frames_decoded: inbound.inbound.frames_decoded,
        key_frames_decoded: inbound.inbound.key_frames_decoded,
        frames_dropped: inbound.inbound.frames_dropped,
        frames_received: inbound.inbound.frames_received,
        frames_per_second: inbound.inbound.frames_per_second,
        frame_width: inbound.inbound.frame_width,
        frame_height: inbound.inbound.frame_height,
        freeze_count: inbound.inbound.freeze_count,
        total_freeze_duration_s: inbound.inbound.total_freeze_duration,
        pause_count: inbound.inbound.pause_count,
        total_pause_duration_s: inbound.inbound.total_pause_duration,
        jitter_s: inbound.received.jitter,
        jitter_buffer_delay_s: inbound.inbound.jitter_buffer_delay,
        jitter_buffer_target_delay_s: inbound.inbound.jitter_buffer_target_delay,
        jitter_buffer_minimum_delay_s: inbound.inbound.jitter_buffer_minimum_delay,
        jitter_buffer_emitted_count: inbound.inbound.jitter_buffer_emitted_count,
        total_decode_time_s: inbound.inbound.total_decode_time,
        total_processing_delay_s: inbound.inbound.total_processing_delay,
        total_assembly_time_s: inbound.inbound.total_assembly_time,
        frames_assembled_from_multiple_packets: inbound
            .inbound
            .frames_assembled_from_multiple_packets,
        total_inter_frame_delay_s: inbound.inbound.total_inter_frame_delay,
        nack_count: inbound.inbound.nack_count,
        pli_count: inbound.inbound.pli_count,
        qp_sum: inbound.inbound.qp_sum,
        decoder_implementation: inbound.inbound.decoder_implementation.clone(),
        power_efficient_decoder: inbound.inbound.power_efficient_decoder,
        codec_mime_type: codecs.get(&inbound.stream.codec_id).cloned(),
        frame_arrival_intervals_ms: Vec::new(),
    })
}

/// Extracts the audio send-side reading.
pub fn extract_audio_outbound(stats: &[RtcStats]) -> Option<AudioOutbound> {
    let outbound = stats.iter().find_map(|stat| match stat {
        RtcStats::OutboundRtp(out) if out.stream.kind == KIND_AUDIO => Some(out),
        _ => None,
    })?;
    Some(AudioOutbound {
        bytes_sent: outbound.sent.bytes_sent,
        header_bytes_sent: outbound.outbound.header_bytes_sent,
        packets_sent: outbound.sent.packets_sent,
        target_bitrate_bps: outbound.outbound.target_bitrate,
    })
}

/// Extracts the audio receive-side reading.
pub fn extract_audio_inbound(
    stats: &[RtcStats],
    previous_packets_lost: Option<i64>,
) -> Option<AudioInbound> {
    let inbound = stats.iter().find_map(|stat| match stat {
        RtcStats::InboundRtp(inb) if inb.stream.kind == KIND_AUDIO => Some(inb),
        _ => None,
    })?;

    let loss_delta = previous_packets_lost
        .map(|previous| ClampedDelta::between(previous, inbound.received.packets_lost))
        .unwrap_or_default();

    Some(AudioInbound {
        bytes_received: inbound.inbound.bytes_received,
        packets_received: inbound.received.packets_received,
        packets_lost: inbound.received.packets_lost,
        packets_lost_delta: loss_delta.value,
        packets_lost_clamped_from: loss_delta.clamped_from,
        jitter_s: inbound.received.jitter,
        jitter_buffer_delay_s: inbound.inbound.jitter_buffer_delay,
        jitter_buffer_emitted_count: inbound.inbound.jitter_buffer_emitted_count,
        total_samples_received: inbound.inbound.total_samples_received,
        concealed_samples: inbound.inbound.concealed_samples,
        silent_concealed_samples: inbound.inbound.silent_concealed_samples,
        concealment_events: inbound.inbound.concealment_events,
        inserted_samples_for_deceleration: inbound.inbound.inserted_samples_for_deceleration,
        removed_samples_for_acceleration: inbound.inbound.removed_samples_for_acceleration,
        total_samples_duration_s: inbound.inbound.total_samples_duration,
        audio_level: inbound.inbound.audio_level,
    })
}

/// Extracts the playout-side audio reading.
pub fn extract_audio_playout(stats: &[RtcStats]) -> Option<AudioPlayout> {
    let playout = stats.iter().find_map(|stat| match stat {
        RtcStats::MediaPlayout(p) => Some(p),
        _ => None,
    })?;
    Some(AudioPlayout {
        total_playout_delay_s: playout.audio_playout.total_playout_delay,
        total_samples_count: playout.audio_playout.total_samples_count,
        synthesized_samples_duration_s: playout.audio_playout.synthesized_samples_duration,
        synthesized_samples_events: playout.audio_playout.synthesized_samples_events,
        total_samples_duration_s: playout.audio_playout.total_samples_duration,
    })
}

/// Extracts the transport reading, joining the selected candidate pair and the RTCP
/// round-trip corroborator.
pub fn extract_transport(stats: &[RtcStats]) -> Option<TransportSample> {
    let transport = stats.iter().find_map(|stat| match stat {
        RtcStats::Transport(t) => Some(t),
        _ => None,
    })?;

    // Prefer the pair the transport names; fall back to the nominated one, which is what
    // libwebrtc reports before the selection is published.
    let selected_id = &transport.transport.selected_candidate_pair_id;
    let pair = stats
        .iter()
        .filter_map(|stat| match stat {
            RtcStats::CandidatePair(p) => Some(p),
            _ => None,
        })
        .find(|p| &p.rtc.id == selected_id)
        .or_else(|| {
            stats.iter().find_map(|stat| match stat {
                RtcStats::CandidatePair(p) if p.candidate_pair.nominated => Some(p),
                _ => None,
            })
        });

    // Filtered to the video stream. This is documented as the video-path RTCP round trip,
    // and with audio enabled — which the reference configuration requires — an unfiltered
    // search can silently return the audio stream's RTT instead.
    let rtcp = stats.iter().find_map(|stat| match stat {
        RtcStats::RemoteInboundRtp(r)
            if r.stream.kind == KIND_VIDEO && r.remote_inbound.round_trip_time_measurements > 0 =>
        {
            Some(&r.remote_inbound)
        }
        _ => None,
    });

    Some(TransportSample {
        ice_state: transport.transport.ice_state.map(|s| format!("{s:?}").to_lowercase()),
        dtls_state: transport.transport.dtls_state.map(|s| format!("{s:?}").to_lowercase()),
        selected_candidate_pair_changes: transport.transport.selected_candidate_pair_changes,
        packets_sent: transport.transport.packets_sent,
        packets_received: transport.transport.packets_received,
        candidate_pair_rtt_s: pair.map(|p| p.candidate_pair.current_round_trip_time),
        subscriber_available_outgoing_bitrate_bps: pair
            .map(|p| p.candidate_pair.available_outgoing_bitrate),
        rtcp_rtt_s: rtcp.map(|r| r.round_trip_time),
        rtcp_rtt_measurements: rtcp.map(|r| r.round_trip_time_measurements),
    })
}

/// Extracts the legacy data-channel reading.
///
/// Absent on the data-track path: `RtcStats::DataChannel` covers SCTP channels only and
/// `livekit-datatrack` surfaces no stats at all, which is why the control payload carries
/// its own sequence number.
pub fn extract_data_channel(stats: &[RtcStats]) -> Option<DataChannelSample> {
    let dc = stats.iter().find_map(|stat| match stat {
        RtcStats::DataChannel(d) => Some(d),
        _ => None,
    })?;
    Some(DataChannelSample {
        label: dc.dc.label.clone(),
        messages_sent: dc.dc.messages_sent,
        messages_received: dc.dc.messages_received,
        bytes_sent: dc.dc.bytes_sent,
        bytes_received: dc.dc.bytes_received,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use livekit::webrtc::stats::{CodecStats, InboundRtpStats, OutboundRtpStats};

    fn codec_stat(id: &str, mime_type: &str) -> RtcStats {
        let mut stat = CodecStats::default();
        stat.rtc.id = id.to_owned();
        stat.codec.mime_type = mime_type.to_owned();
        RtcStats::Codec(stat)
    }

    fn video_outbound(active: bool, encoder: &str, codec_id: &str) -> OutboundRtpStats {
        let mut stat = OutboundRtpStats::default();
        stat.stream.kind = KIND_VIDEO.to_owned();
        stat.stream.codec_id = codec_id.to_owned();
        stat.outbound.active = active;
        stat.outbound.encoder_implementation = encoder.to_owned();
        stat.outbound.frames_encoded = 100;
        stat.sent.packets_sent = 500;
        stat.sent.bytes_sent = 600_000;
        stat.outbound.header_bytes_sent = 12_000;
        stat
    }

    /// The negotiated codec must come from the joined codec stat, never from what was
    /// requested: a run that asked for AV1 and got VP9 is a different experiment.
    #[test]
    fn joins_the_negotiated_codec_by_id() {
        let stats = vec![
            codec_stat("codec-1", "video/VP9"),
            RtcStats::OutboundRtp(video_outbound(true, "libvpx", "codec-1")),
        ];
        let out = extract_video_outbound(&stats).expect("outbound present");
        assert_eq!(out.codec_mime_type.as_deref(), Some("video/VP9"));
        assert_eq!(
            crate::encoder::codec_from_mime_type(&out.codec_mime_type.unwrap()).as_deref(),
            Some("vp9")
        );
    }

    /// Bitrate must include header bytes: payload-only understates the wire rate the
    /// uplink ceiling is about.
    #[test]
    fn header_bytes_are_carried_separately_for_the_wire_rate() {
        let stats = vec![RtcStats::OutboundRtp(video_outbound(true, "libvpx", "c"))];
        let out = extract_video_outbound(&stats).expect("outbound");
        assert_eq!(out.bytes_sent, 600_000);
        assert_eq!(out.header_bytes_sent, 12_000);
    }

    /// With several layers present, the active one is the measurement. Reading an
    /// arbitrary layer reports the wrong encoder and the wrong bitrate.
    #[test]
    fn prefers_the_active_outbound_layer() {
        let stats = vec![
            RtcStats::OutboundRtp(video_outbound(false, "inactive-encoder", "c")),
            RtcStats::OutboundRtp(video_outbound(true, "active-encoder", "c")),
        ];
        let out = extract_video_outbound(&stats).expect("outbound");
        assert_eq!(out.encoder_implementation, "active-encoder");
    }

    #[test]
    fn falls_back_to_a_named_encoder_when_no_layer_is_active() {
        let stats = vec![
            RtcStats::OutboundRtp(video_outbound(false, "", "c")),
            RtcStats::OutboundRtp(video_outbound(false, "libaom", "c")),
        ];
        let out = extract_video_outbound(&stats).expect("outbound");
        assert_eq!(out.encoder_implementation, "libaom");
    }

    /// Frames encoded but nothing sent is the malformed-bitstream condition. It must be
    /// a flag the analysis can turn into an invalid verdict, not an apparent zero bitrate.
    #[test]
    fn detects_frames_encoded_with_nothing_sent() {
        let mut stat = video_outbound(true, "libaom", "c");
        stat.sent.packets_sent = 0;
        let stats = vec![RtcStats::OutboundRtp(stat)];
        let out = extract_video_outbound(&stats).expect("outbound");
        assert!(out.malformed_bitstream);
    }

    #[test]
    fn healthy_stream_is_not_malformed() {
        let stats = vec![RtcStats::OutboundRtp(video_outbound(true, "libaom", "c"))];
        assert!(!extract_video_outbound(&stats).expect("outbound").malformed_bitstream);
    }

    /// A missing quality-limitation key means zero seconds spent in that state, not an
    /// error. libwebrtc omits keys it has never entered.
    #[test]
    fn missing_quality_limitation_keys_are_zero_not_an_error() {
        let mut stat = video_outbound(true, "libaom", "c");
        stat.outbound.quality_limitation_durations.insert("cpu".to_owned(), 4.0);
        let stats = vec![RtcStats::OutboundRtp(stat)];
        let out = extract_video_outbound(&stats).expect("outbound");
        assert_eq!(out.quality_limitation_cpu_s, 4.0);
        assert_eq!(out.quality_limitation_bandwidth_s, 0.0);
        assert_eq!(out.quality_limitation_other_s, 0.0);
    }

    fn video_inbound_stat(packets_lost: i64) -> RtcStats {
        let mut stat = InboundRtpStats::default();
        stat.stream.kind = KIND_VIDEO.to_owned();
        stat.received.packets_lost = packets_lost;
        stat.received.packets_received = 10_000;
        stat.inbound.jitter_buffer_delay = 1.25;
        stat.inbound.jitter_buffer_emitted_count = 250;
        RtcStats::InboundRtp(stat)
    }

    #[test]
    fn first_poll_has_no_loss_delta() {
        let stats = vec![video_inbound_stat(12)];
        let inbound = extract_video_inbound(&stats, None).expect("inbound");
        assert_eq!(inbound.packets_lost, 12);
        assert_eq!(inbound.packets_lost_delta, 0);
        assert_eq!(inbound.packets_lost_clamped_from, None);
    }

    #[test]
    fn loss_delta_is_differenced_against_the_previous_poll() {
        let stats = vec![video_inbound_stat(20)];
        let inbound = extract_video_inbound(&stats, Some(12)).expect("inbound");
        assert_eq!(inbound.packets_lost_delta, 8);
        assert_eq!(inbound.packets_lost_clamped_from, None);
    }

    /// A downward revision of `packets_lost` is a reorder artifact, not a gain. The
    /// clamp must reach the record.
    #[test]
    fn downward_loss_revision_is_clamped_and_recorded() {
        let stats = vec![video_inbound_stat(9)];
        let inbound = extract_video_inbound(&stats, Some(12)).expect("inbound");
        assert_eq!(inbound.packets_lost_delta, 0);
        assert_eq!(inbound.packets_lost_clamped_from, Some(-3));
    }

    /// Video and audio share the inbound struct and are distinguished only by `kind`.
    /// Confusing them would report audio concealment as video loss.
    #[test]
    fn audio_and_video_inbound_are_kept_apart() {
        let mut audio = InboundRtpStats::default();
        audio.stream.kind = KIND_AUDIO.to_owned();
        audio.inbound.audio_level = 0.42;
        audio.inbound.concealed_samples = 7;
        let stats = vec![video_inbound_stat(3), RtcStats::InboundRtp(audio)];

        let video = extract_video_inbound(&stats, None).expect("video inbound");
        assert_eq!(video.packets_lost, 3);
        let audio_in = extract_audio_inbound(&stats, None).expect("audio inbound");
        assert_eq!(audio_in.audio_level, 0.42);
        assert_eq!(audio_in.concealed_samples, 7);
    }

    /// The RTCP corroborator is documented as the video-path round trip. With audio
    /// enabled — which the reference configuration requires — an unfiltered search can
    /// return the audio stream's RTT instead and silently corroborate the wrong path.
    #[test]
    fn rtcp_corroborator_ignores_the_audio_stream() {
        let mut audio_remote = livekit::webrtc::stats::RemoteInboundRtpStats::default();
        audio_remote.stream.kind = KIND_AUDIO.to_owned();
        audio_remote.remote_inbound.round_trip_time = 0.999;
        audio_remote.remote_inbound.round_trip_time_measurements = 5;

        let mut video_remote = livekit::webrtc::stats::RemoteInboundRtpStats::default();
        video_remote.stream.kind = KIND_VIDEO.to_owned();
        video_remote.remote_inbound.round_trip_time = 0.025;
        video_remote.remote_inbound.round_trip_time_measurements = 5;

        let mut transport = livekit::webrtc::stats::TransportStats::default();
        transport.rtc.id = "transport-1".to_owned();

        // Audio listed first, so an unfiltered search would take it.
        let stats = vec![
            RtcStats::Transport(transport),
            RtcStats::RemoteInboundRtp(audio_remote),
            RtcStats::RemoteInboundRtp(video_remote),
        ];
        let sample = extract_transport(&stats).expect("transport");
        assert_eq!(sample.rtcp_rtt_s, Some(0.025));
    }

    /// The inbound side must apply the same layer-selection care as the outbound side:
    /// the stream that is actually decoding is the measurement.
    #[test]
    fn prefers_the_decoding_inbound_stream() {
        let mut idle = livekit::webrtc::stats::InboundRtpStats::default();
        idle.stream.kind = KIND_VIDEO.to_owned();
        idle.inbound.decoder_implementation = "idle-layer".to_owned();

        let mut active = livekit::webrtc::stats::InboundRtpStats::default();
        active.stream.kind = KIND_VIDEO.to_owned();
        active.inbound.frames_decoded = 120;
        active.inbound.decoder_implementation = "active-layer".to_owned();

        let stats = vec![RtcStats::InboundRtp(idle), RtcStats::InboundRtp(active)];
        let inbound = extract_video_inbound(&stats, None).expect("inbound");
        assert_eq!(inbound.decoder_implementation, "active-layer");
    }

    #[test]
    fn absent_sections_extract_to_none() {
        let empty: Vec<RtcStats> = Vec::new();
        assert!(extract_video_outbound(&empty).is_none());
        assert!(extract_video_inbound(&empty, None).is_none());
        assert!(extract_audio_inbound(&empty, None).is_none());
        assert!(extract_audio_playout(&empty).is_none());
        assert!(extract_transport(&empty).is_none());
        assert!(extract_data_channel(&empty).is_none());
    }

    #[test]
    fn quality_limitation_names_are_lowercase() {
        assert_eq!(quality_limitation_name(QualityLimitationReason::Cpu), "cpu");
        assert_eq!(quality_limitation_name(QualityLimitationReason::Bandwidth), "bandwidth");
        assert_eq!(quality_limitation_name(QualityLimitationReason::None), "none");
    }
}
