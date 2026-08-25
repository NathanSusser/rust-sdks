//! The stats sampler: a fixed-cadence loop that owns its own budget accounting.
//!
//! A sampler that silently exceeds its own interval invalidates every rate it reports,
//! because each rate is a delta divided by an interval the sampler assumed it met. The
//! loop therefore measures its own spacing on the monotonic clock and counts every poll
//! that ran over budget. That count is a validity gate, not a failure: a run where the
//! client stalled measured the client, not the network.

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};

use livekit::prelude::*;
use parking_lot::Mutex;

use crate::cli::Args;
use crate::clock::RunClock;
use crate::control::publisher::PublisherCounters;
use crate::keyframe::KeyframeServiceTracker;
use crate::run::SharedState;
use crate::snapshot::{ProbeSample, SamplerHealth, Snapshot};
use crate::writer::JsonLinesWriter;

/// Cadence accounting for one sampler.
#[derive(Debug, Clone)]
pub struct PollBudget {
    nominal: Duration,
    multiplier: f64,
    last_poll_at: Option<Instant>,
    polls_total: u64,
    overbudget_count: u64,
    interval_ms_samples: Vec<f64>,
}

/// What one poll cost, in cadence terms.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct PollTiming {
    /// Configured spacing, in milliseconds.
    pub nominal_interval_ms: f64,
    /// Measured spacing since the previous poll, in milliseconds. Equals the nominal
    /// value for the first poll, which has no predecessor to measure against.
    pub actual_interval_ms: f64,
    /// True when the measured spacing exceeded the configured multiple of nominal.
    pub overbudget: bool,
    /// Overbudget polls so far, including this one.
    pub overbudget_count: u64,
    /// Polls recorded so far, including this one.
    pub polls_total: u64,
}

impl PollBudget {
    /// Creates a budget for the given cadence.
    ///
    /// The multiplier defining "overbudget" comes from `matrix.yaml` and is carried into
    /// the run record, so a change to it is traceable rather than a magic number.
    pub fn new(nominal: Duration, multiplier: f64) -> Self {
        Self {
            nominal,
            multiplier,
            last_poll_at: None,
            polls_total: 0,
            overbudget_count: 0,
            interval_ms_samples: Vec::new(),
        }
    }

    /// Records a poll occurring at `now` and returns its cadence accounting.
    pub fn record(&mut self, now: Instant) -> PollTiming {
        let nominal_ms = self.nominal.as_secs_f64() * 1000.0;
        let actual_ms = match self.last_poll_at {
            Some(previous) => now.duration_since(previous).as_secs_f64() * 1000.0,
            // The first poll has no predecessor. Attributing an interval to it would
            // either invent a stall or hide one.
            None => nominal_ms,
        };
        self.last_poll_at = Some(now);
        self.polls_total += 1;
        self.interval_ms_samples.push(actual_ms);

        let overbudget =
            self.last_poll_had_predecessor() && actual_ms > nominal_ms * self.multiplier;
        if overbudget {
            self.overbudget_count += 1;
        }

        PollTiming {
            nominal_interval_ms: nominal_ms,
            actual_interval_ms: actual_ms,
            overbudget,
            overbudget_count: self.overbudget_count,
            polls_total: self.polls_total,
        }
    }

    /// Whether the poll just recorded had a predecessor to be measured against.
    fn last_poll_had_predecessor(&self) -> bool {
        self.polls_total > 1
    }

    /// Share of polls that ran over budget, as a percentage.
    pub fn overbudget_pct(&self) -> Option<f64> {
        (self.polls_total > 0)
            .then(|| self.overbudget_count as f64 / self.polls_total as f64 * 100.0)
    }

    /// Polls recorded.
    pub fn polls_total(&self) -> u64 {
        self.polls_total
    }

    /// Polls that ran over budget.
    pub fn overbudget_count(&self) -> u64 {
        self.overbudget_count
    }

    /// Every measured interval, in milliseconds.
    pub fn interval_ms_samples(&self) -> &[f64] {
        &self.interval_ms_samples
    }

    /// Configured cadence.
    pub fn nominal(&self) -> Duration {
        self.nominal
    }

    /// Deadline for the poll at `index`, measured from `origin`.
    ///
    /// Scheduling against absolute deadlines rather than sleeping for a fixed interval
    /// keeps the cadence from drifting: without it, the time each poll spends collecting
    /// stats is added to every subsequent interval and the effective rate falls steadily.
    pub fn deadline(&self, origin: Instant, index: u64) -> Instant {
        let offset_ns =
            self.nominal.as_nanos().saturating_mul(index as u128).min(u64::MAX as u128) as u64;
        origin + Duration::from_nanos(offset_ns)
    }
}

/// Percentile of a sample set, by nearest-rank on a sorted copy.
///
/// Uses the standard nearest-rank definition — the smallest value at or below which at
/// least `quantile` of the samples fall — rather than interpolating between neighbours.
/// Interpolation would invent a value that was never measured, which for a latency tail
/// is exactly the wrong thing to report.
///
/// Returns `None` for an empty set rather than a placeholder: a percentile over no data
/// is not zero, and emitting zero would let an unmeasured run look like a fast one.
pub fn percentile(samples: &[f64], quantile: f64) -> Option<f64> {
    if samples.is_empty() {
        return None;
    }
    let mut sorted: Vec<f64> = samples.to_vec();
    sorted.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
    let q = quantile.clamp(0.0, 1.0);
    let rank = (q * sorted.len() as f64).ceil() as usize;
    let index = rank.saturating_sub(1).min(sorted.len() - 1);
    sorted.get(index).copied()
}

/// What a completed sampler run produced.
#[derive(Debug, Clone, Default)]
pub struct SamplerResult {
    pub snapshots_written: u64,
    /// Whether a remote video track was ever subscribed. A run that never saw one
    /// measured nothing on the receive side.
    pub saw_subscription: bool,
    /// Last observed negotiated codec MIME type, for the run-level record.
    pub negotiated_codec_mime: Option<String>,
    /// Last observed encoder implementation string.
    pub encoder_implementation: Option<String>,
    /// libwebrtc's hardware signal for the selected encoder.
    pub power_efficient_encoder: bool,
    /// Polls where the stats RPC failed outright, as opposed to returning no stats yet.
    pub stats_rpc_failures: u64,
}

/// The stats sampler: a fixed-cadence actor owning its own budget and its output handle.
///
/// The video track is polled on its own cadence, which is raised for the loss suite where
/// keyframe recovery timing is a primary metric. Everything else stays on the base
/// cadence. Both are recorded per run so a reader never has to infer the resolution
/// behind a quantized measurement.
pub struct StatsSampler {
    args: Args,
    clock: RunClock,
    shared: Arc<SharedState>,
    counters: Arc<PublisherCounters>,
    writer: Arc<Mutex<JsonLinesWriter>>,
    video_track: LocalVideoTrack,
    audio_track: Option<LocalAudioTrack>,
    subscriber_room: Arc<Room>,
    shutdown: Arc<AtomicBool>,
}

impl StatsSampler {
    /// Creates a sampler for one run.
    #[allow(clippy::too_many_arguments)]
    pub fn new(
        args: Args,
        clock: RunClock,
        shared: Arc<SharedState>,
        counters: Arc<PublisherCounters>,
        writer: Arc<Mutex<JsonLinesWriter>>,
        video_track: LocalVideoTrack,
        audio_track: Option<LocalAudioTrack>,
        subscriber_room: Arc<Room>,
        shutdown: Arc<AtomicBool>,
    ) -> Self {
        Self {
            args,
            clock,
            shared,
            counters,
            writer,
            video_track,
            audio_track,
            subscriber_room,
            shutdown,
        }
    }

    /// Samples until the run duration elapses.
    pub async fn run(self) -> SamplerResult {
        let mut budget =
            PollBudget::new(self.args.stats_interval(), self.args.poll_overbudget_multiplier);
        let mut video_budget =
            PollBudget::new(self.args.video_interval(), self.args.poll_overbudget_multiplier);
        let mut keyframes = KeyframeServiceTracker::new();
        let mut state = PollState::default();

        let origin = self.clock.monotonic_origin();
        let duration = Duration::from_secs(self.args.duration_s);
        let mut index: u64 = 0;
        let mut result = SamplerResult::default();

        while origin.elapsed() < duration && !self.shutdown.load(Ordering::Acquire) {
            let deadline = budget.deadline(origin, index);
            let now = Instant::now();
            if deadline > now {
                tokio::time::sleep(deadline - now).await;
            }

            let poll_started = Instant::now();
            let timing = budget.record(poll_started);

            let snapshot = self
                .collect(
                    &mut state,
                    &mut keyframes,
                    &mut video_budget,
                    &mut result,
                    index,
                    timing,
                    poll_started,
                )
                .await;
            if snapshot.video_in.is_some() {
                result.saw_subscription = true;
            }

            match snapshot.to_jsonl() {
                Ok(line) => match self.writer.lock().write_line(&line) {
                    Ok(()) => result.snapshots_written += 1,
                    Err(e) => log::error!("snapshot write failed: {e}"),
                },
                Err(e) => log::error!("snapshot serialize failed: {e}"),
            }

            index += 1;
        }

        keyframes.finish();
        log::info!(
            "sampler finished: {} polls, {} overbudget ({:.2}%), {} stats-rpc failures, \
             keyframe recoveries {} max {:?}",
            budget.polls_total(),
            budget.overbudget_count(),
            budget.overbudget_pct().unwrap_or(0.0),
            result.stats_rpc_failures,
            keyframes.completed_polls().len(),
            keyframes.max_polls()
        );

        result
    }

    /// Collects one snapshot.
    async fn collect(
        &self,
        state: &mut PollState,
        keyframes: &mut KeyframeServiceTracker,
        video_budget: &mut PollBudget,
        result: &mut SamplerResult,
        index: u64,
        timing: PollTiming,
        poll_started: Instant,
    ) -> Snapshot {
        // An RPC failure and an empty result are different facts. Collapsing both to an
        // empty Vec makes a persistently broken stats channel look exactly like a session
        // that has not produced stats yet, so the poll records which one happened.
        let mut stats_rpc_failed = false;
        let local_stats = match self.video_track.get_stats().await {
            Ok(stats) => stats,
            Err(e) => {
                log::warn!("video track get_stats failed at poll {index}: {e}");
                stats_rpc_failed = true;
                result.stats_rpc_failures += 1;
                Vec::new()
            }
        };
        let video_out = crate::stats::extract_video_outbound(&local_stats);

        if let Some(out) = video_out.as_ref() {
            result.negotiated_codec_mime = out.codec_mime_type.clone();
            if !out.encoder_implementation.is_empty() {
                result.encoder_implementation = Some(out.encoder_implementation.clone());
                result.power_efficient_encoder = out.power_efficient_encoder;
            }
        }

        if let Some(out) = video_out.as_ref() {
            // Keyframe service time is measured by differencing, which is why its
            // resolution equals the poll period and why it is reported in poll intervals.
            let pli_delta = out.pli_count.saturating_sub(state.pli_count) as u64;
            let key_delta = out.key_frames_encoded.saturating_sub(state.key_frames_encoded) as u64;
            keyframes.observe_poll(pli_delta, key_delta);
            state.pli_count = out.pli_count;
            state.key_frames_encoded = out.key_frames_encoded;
            video_budget.record(poll_started);
        }

        let audio_out = match self.audio_track.as_ref() {
            Some(track) => match track.get_stats().await {
                Ok(stats) => crate::stats::extract_audio_outbound(&stats),
                Err(e) => {
                    log::warn!("audio track get_stats failed at poll {index}: {e}");
                    stats_rpc_failed = true;
                    result.stats_rpc_failures += 1;
                    None
                }
            },
            None => None,
        };

        let (remote_stats, remote_rpc_failed) = self.remote_stats().await;
        if remote_rpc_failed {
            stats_rpc_failed = true;
            result.stats_rpc_failures += 1;
        }
        let video_in = crate::stats::extract_video_inbound(&remote_stats, state.video_packets_lost);
        let audio_in = crate::stats::extract_audio_inbound(&remote_stats, state.audio_packets_lost);
        if let Some(v) = video_in.as_ref() {
            state.video_packets_lost = Some(v.packets_lost);
        }
        if let Some(a) = audio_in.as_ref() {
            state.audio_packets_lost = Some(a.packets_lost);
        }

        let mut video_in = video_in;
        let g2g_interval = self.shared.g2g.lock().take_interval();
        if let Some(v) = video_in.as_mut() {
            v.frame_arrival_intervals_ms = g2g_interval.frame_arrival_intervals_ms.clone();
        }

        let control = {
            let mut receiver = self.shared.control.lock();
            let control_interval = receiver.take_interval();
            crate::snapshot::ControlSample {
                seq_published: self.counters.seq_published(),
                distinct_seq_received: receiver.distinct_received(),
                distinct_seq_received_interval: control_interval.distinct_received,
                max_seq_received: receiver.max_seq(),
                reordered_interval: control_interval.reordered,
                duplicates_interval: control_interval.duplicates,
                max_gap: receiver.max_gap(),
                gap_lengths: receiver.gap_lengths(),
                gap_p99: receiver.gap_p99(),
                owd_raw_us_interval: control_interval.owd_raw_us,
                jitter_ms: receiver.jitter_ms(),
                late_count: self.args.playout_window_ms.map(|_| receiver.late_count()),
                late_eligible_count: self
                    .args
                    .playout_window_ms
                    .map(|_| receiver.late_eligible_count()),
            }
        };

        let probe = {
            let tracker = self.shared.probe.lock();
            let completed = tracker.probes_completed();
            let new_rtts = tracker
                .rtt_samples_us()
                .iter()
                .skip(state.rtt_samples_reported)
                .copied()
                .collect::<Vec<_>>();
            state.rtt_samples_reported = tracker.rtt_samples_us().len();
            ProbeSample {
                probes_sent: tracker.probes_sent(),
                probes_completed: completed,
                probes_lost: tracker.probes_lost(),
                probes_in_flight: tracker.probes_in_flight() as u64,
                rtt_us_interval: new_rtts,
                theta_ms: tracker.theta_ms(),
                clock_sync_confidence: tracker.confidence().as_str().to_owned(),
            }
        };

        let g2g = (g2g_interval.distinct_frame_ids > 0 || !g2g_interval.latency_us.is_empty())
            .then(|| crate::snapshot::G2gSample {
                latency_us_interval: g2g_interval.latency_us,
                frames_captured: self.shared.frames_captured.load(Ordering::Relaxed),
                distinct_frame_ids: g2g_interval.distinct_frame_ids,
                frame_id_span: g2g_interval.frame_id_span,
                frames_without_timestamp: g2g_interval.frames_without_timestamp,
            });

        Snapshot {
            poll_index: index,
            t_unix_us: self.clock.wall_us(),
            t_monotonic_us: self.clock.monotonic_us(),
            scored: self.clock.monotonic_origin().elapsed()
                >= Duration::from_secs(self.args.warmup_s),
            sampler: SamplerHealth {
                nominal_interval_ms: timing.nominal_interval_ms,
                actual_interval_ms: timing.actual_interval_ms,
                poll_duration_ms: poll_started.elapsed().as_secs_f64() * 1000.0,
                overbudget: timing.overbudget,
                overbudget_count: timing.overbudget_count,
                polls_total: timing.polls_total,
                stats_rpc_failed,
                stats_rpc_failures: result.stats_rpc_failures,
            },
            video_out,
            video_in,
            audio_out,
            audio_in: audio_in.clone(),
            audio_playout: crate::stats::extract_audio_playout(&remote_stats),
            transport: crate::stats::extract_transport(&remote_stats),
            data_channel: (!self.args.control_transport.is_data_track())
                .then(|| crate::stats::extract_data_channel(&remote_stats))
                .flatten(),
            control,
            probe,
            g2g,
        }
    }

    /// Collects receive-side stats from every subscribed remote track.
    ///
    /// Both media kinds and the transport are read from one merged set so that the
    /// extractors can join a stream to its codec and to the selected candidate pair,
    /// which live in different stat entries.
    /// Returns the merged stats and whether any track's stats RPC failed outright.
    async fn remote_stats(&self) -> (Vec<livekit::webrtc::stats::RtcStats>, bool) {
        let mut merged = Vec::new();
        let mut rpc_failed = false;
        for (_, participant) in self.subscriber_room.remote_participants() {
            for (_, publication) in participant.track_publications() {
                let Some(track) = publication.track() else {
                    continue;
                };
                match track.get_stats().await {
                    Ok(stats) => merged.extend(stats),
                    Err(e) => {
                        log::warn!("remote track get_stats failed: {e}");
                        rpc_failed = true;
                    }
                }
            }
        }
        (merged, rpc_failed)
    }
}

/// Cumulative readings carried between polls, so that a counter can be differenced.
#[derive(Debug, Default)]
struct PollState {
    pli_count: u32,
    key_frames_encoded: u32,
    video_packets_lost: Option<i64>,
    audio_packets_lost: Option<i64>,
    rtt_samples_reported: usize,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn first_poll_is_never_overbudget() {
        let mut budget = PollBudget::new(Duration::from_secs(1), 1.5);
        let timing = budget.record(Instant::now());
        assert!(!timing.overbudget);
        assert_eq!(timing.actual_interval_ms, 1000.0);
        assert_eq!(timing.polls_total, 1);
    }

    #[test]
    fn on_cadence_polls_are_within_budget() {
        let mut budget = PollBudget::new(Duration::from_millis(100), 1.5);
        let origin = Instant::now();
        budget.record(origin);
        let timing = budget.record(origin + Duration::from_millis(102));
        assert!(!timing.overbudget);
        assert_eq!(budget.overbudget_count(), 0);
    }

    /// The gate: 1.5 x 100 ms is 150 ms, so 151 ms is over and 149 ms is not.
    #[test]
    fn overbudget_boundary_follows_the_multiplier() {
        let mut budget = PollBudget::new(Duration::from_millis(100), 1.5);
        let origin = Instant::now();
        budget.record(origin);
        assert!(!budget.record(origin + Duration::from_millis(149)).overbudget);
        let late = budget.record(origin + Duration::from_millis(149 + 151));
        assert!(late.overbudget);
        assert_eq!(late.overbudget_count, 1);
    }

    #[test]
    fn overbudget_share_is_reported() {
        let mut budget = PollBudget::new(Duration::from_millis(100), 1.5);
        let mut at = Instant::now();
        budget.record(at);
        for step in [100u64, 100, 400, 100] {
            at += Duration::from_millis(step);
            budget.record(at);
        }
        assert_eq!(budget.polls_total(), 5);
        assert_eq!(budget.overbudget_count(), 1);
        assert_eq!(budget.overbudget_pct(), Some(20.0));
    }

    #[test]
    fn empty_budget_has_no_share() {
        let budget = PollBudget::new(Duration::from_secs(1), 1.5);
        assert_eq!(budget.overbudget_pct(), None);
    }

    /// Deadlines must be exact multiples of the cadence from the origin, or the sampler
    /// drifts by its own work time on every poll.
    #[test]
    fn deadlines_do_not_drift() {
        let budget = PollBudget::new(Duration::from_millis(100), 1.5);
        let origin = Instant::now();
        assert_eq!(budget.deadline(origin, 0), origin);
        assert_eq!(budget.deadline(origin, 10), origin + Duration::from_secs(1));
        assert_eq!(budget.deadline(origin, 36_000), origin + Duration::from_secs(3600));
    }

    #[test]
    fn percentile_picks_by_rank() {
        let samples: Vec<f64> = (1..=100).map(|v| v as f64).collect();
        assert_eq!(percentile(&samples, 0.5), Some(50.0));
        assert_eq!(percentile(&samples, 0.99), Some(99.0));
        assert_eq!(percentile(&samples, 0.0), Some(1.0));
        assert_eq!(percentile(&samples, 1.0), Some(100.0));
    }

    /// A percentile over no samples must be absent, not zero. Zero is a measurement.
    #[test]
    fn percentile_of_nothing_is_absent() {
        assert_eq!(percentile(&[], 0.5), None);
    }

    #[test]
    fn percentile_handles_unsorted_input() {
        assert_eq!(percentile(&[9.0, 1.0, 5.0], 0.5), Some(5.0));
    }
}
