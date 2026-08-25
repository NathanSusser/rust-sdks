//! Receive-side control accounting.
//!
//! The Rust SDK exposes no per-frame receive statistic for data tracks, and
//! `RtcStats::DataChannel` carries only message and byte counts for the legacy path.
//! Every control metric is therefore derived from the harness's own sequence number and
//! send timestamp in the payload.

use std::collections::HashSet;

use crate::control::payload::ControlSample;

/// Interarrival jitter smoothing factor from RFC 3550: `J += (|D| - J) / 16`.
const JITTER_GAIN: f64 = 1.0 / 16.0;

/// Per-interval counters, reset each time a snapshot is taken.
#[derive(Debug, Default, Clone, Copy)]
struct IntervalCounters {
    distinct_received: u64,
    reordered: u64,
    duplicates: u64,
}

/// Tracks delivery, ordering, jitter and lateness for the control stream.
///
/// Delivery is *not* scored here. The delivered share needs the publisher's sequence
/// range intersected with the scored window as its denominator, and that join happens in
/// the analysis layer against the publisher sequence log. What this tracker owns is what
/// only the receiver can know: which sequence numbers arrived, when, and in what order.
#[derive(Debug)]
pub struct ControlReceiver {
    seen: HashSet<u64>,
    max_seq: u64,
    min_seq: u64,
    any_received: bool,
    last_transit_us: Option<i64>,
    jitter_us: f64,
    late_count: u64,
    late_eligible_count: u64,
    interval: IntervalCounters,
    owd_raw_us_interval: Vec<i64>,
}

impl Default for ControlReceiver {
    fn default() -> Self {
        Self::new()
    }
}

impl ControlReceiver {
    /// Creates an empty tracker.
    pub fn new() -> Self {
        Self {
            seen: HashSet::new(),
            max_seq: 0,
            min_seq: 0,
            any_received: false,
            last_transit_us: None,
            jitter_us: 0.0,
            late_count: 0,
            late_eligible_count: 0,
            interval: IntervalCounters::default(),
            owd_raw_us_interval: Vec::new(),
        }
    }

    /// Records one received sample.
    ///
    /// `t_recv_unix_us` is the receiver's wall clock; the difference against the sample's
    /// send stamp is a raw one-way delay across two unrelated clocks, meaningful only
    /// after the offset correction is applied by the caller.
    pub fn on_sample(&mut self, sample: &ControlSample, t_recv_unix_us: u64) {
        let raw_owd_us = t_recv_unix_us as i64 - sample.t_send_unix_us as i64;
        self.owd_raw_us_interval.push(raw_owd_us);
        self.update_jitter(raw_owd_us);

        if !self.seen.insert(sample.seq) {
            self.interval.duplicates += 1;
            return;
        }
        self.interval.distinct_received += 1;

        if self.any_received && sample.seq < self.max_seq {
            self.interval.reordered += 1;
        }
        if !self.any_received {
            self.min_seq = sample.seq;
            self.max_seq = sample.seq;
        } else {
            self.min_seq = self.min_seq.min(sample.seq);
            self.max_seq = self.max_seq.max(sample.seq);
        }
        self.any_received = true;
    }

    /// Lengths of every run of consecutive missing sequence numbers, in arrival order of
    /// the sequence space.
    ///
    /// Derived from the set of sequence numbers actually seen rather than accumulated as
    /// samples arrive. That distinction is what makes reordering behave correctly: a
    /// sample that arrives late still lands in the set, so the gap it fills simply is not
    /// a gap when the runs are computed. Accumulating gaps on the forward path instead
    /// would record a hole the moment a sequence number was skipped and never retract it,
    /// conflating reorder with loss on exactly the lossy and jittery cells where the two
    /// must be told apart.
    fn gap_runs(&self) -> Vec<u64> {
        if !self.any_received {
            return Vec::new();
        }
        let mut runs = Vec::new();
        let mut run_length = 0u64;
        for seq in self.min_seq..=self.max_seq {
            if self.seen.contains(&seq) {
                if run_length > 0 {
                    runs.push(run_length);
                    run_length = 0;
                }
            } else {
                run_length += 1;
            }
        }
        // A trailing run cannot occur: max_seq is present by construction.
        runs
    }

    /// Applies the RFC 3550 smoothing to the transit-time difference between consecutive
    /// arrivals. Both terms come from the same pair of clocks, so a constant offset
    /// cancels and the result is skew-immune.
    fn update_jitter(&mut self, transit_us: i64) {
        let Some(previous) = self.last_transit_us else {
            self.last_transit_us = Some(transit_us);
            return;
        };
        let d = (transit_us - previous).abs() as f64;
        self.jitter_us += (d - self.jitter_us) * JITTER_GAIN;
        self.last_transit_us = Some(transit_us);
    }

    /// Scores one sample against the playout deadline.
    ///
    /// A control sample that misses its deadline is loss, not latency. Called only when
    /// the clock offset is valid, so that lateness is never judged on an uncorrected
    /// one-way delay.
    pub fn score_lateness(&mut self, corrected_owd_us: i64, playout_window_us: i64) {
        self.late_eligible_count += 1;
        if corrected_owd_us > playout_window_us {
            self.late_count += 1;
        }
    }

    /// Distinct sequence numbers received so far.
    pub fn distinct_received(&self) -> u64 {
        self.seen.len() as u64
    }

    /// Highest sequence number seen, or zero if nothing has arrived.
    pub fn max_seq(&self) -> u64 {
        self.max_seq
    }

    /// Largest run of consecutive missing sequence numbers observed.
    pub fn max_gap(&self) -> u64 {
        self.gap_runs().into_iter().max().unwrap_or(0)
    }

    /// Every gap run length observed, for the analysis layer to summarize.
    ///
    /// The full distribution travels in the record because a p99 cannot be reconstructed
    /// from a maximum: the maximum is one draw from the tail, and reporting it in place of
    /// a p99 would overstate the gap a watchdog must typically tolerate.
    pub fn gap_lengths(&self) -> Vec<u64> {
        self.gap_runs()
    }

    /// Nearest-rank p99 of the gap run lengths.
    ///
    /// Gap lengths are small integers, so the exact distribution is retained and this is
    /// a true percentile rather than an estimate from a sketch.
    pub fn gap_p99(&self) -> Option<u64> {
        let mut runs = self.gap_runs();
        if runs.is_empty() {
            return None;
        }
        runs.sort_unstable();
        let rank = (0.99 * runs.len() as f64).ceil() as usize;
        let index = rank.saturating_sub(1).min(runs.len() - 1);
        runs.get(index).copied()
    }

    /// Current smoothed interarrival jitter in milliseconds.
    pub fn jitter_ms(&self) -> f64 {
        self.jitter_us / 1000.0
    }

    /// Samples that missed the playout deadline, cumulative.
    pub fn late_count(&self) -> u64 {
        self.late_count
    }

    /// Samples measured against the playout deadline, cumulative.
    pub fn late_eligible_count(&self) -> u64 {
        self.late_eligible_count
    }

    /// Takes this interval's counters and raw one-way observations, resetting them.
    pub fn take_interval(&mut self) -> ControlInterval {
        let counters = std::mem::take(&mut self.interval);
        ControlInterval {
            distinct_received: counters.distinct_received,
            reordered: counters.reordered,
            duplicates: counters.duplicates,
            owd_raw_us: std::mem::take(&mut self.owd_raw_us_interval),
        }
    }
}

/// One interval's worth of receive-side observations.
#[derive(Debug, Default, Clone)]
pub struct ControlInterval {
    /// Distinct sequence numbers first seen during this interval. The collapse signature
    /// under loss is a rate collapse, which a delivered percentage alone hides.
    pub distinct_received: u64,
    pub reordered: u64,
    pub duplicates: u64,
    /// Raw, uncorrected one-way delays observed during this interval.
    pub owd_raw_us: Vec<i64>,
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sample(seq: u64, t_send_unix_us: u64) -> ControlSample {
        ControlSample { seq, t_send_unix_us, probe_token: 0, pad: 0 }
    }

    #[test]
    fn counts_distinct_sequences() {
        let mut rx = ControlReceiver::new();
        for seq in 0..5 {
            rx.on_sample(&sample(seq, 1000 + seq * 5), 1015 + seq * 5);
        }
        assert_eq!(rx.distinct_received(), 5);
        assert_eq!(rx.max_seq(), 4);
        assert_eq!(rx.max_gap(), 0);
    }

    /// A duplicate must not inflate the delivered count, and must be visible as a
    /// duplicate rather than silently dropped.
    #[test]
    fn duplicates_do_not_inflate_delivery() {
        let mut rx = ControlReceiver::new();
        rx.on_sample(&sample(1, 1000), 1015);
        rx.on_sample(&sample(1, 1000), 1020);
        assert_eq!(rx.distinct_received(), 1);
        let interval = rx.take_interval();
        assert_eq!(interval.distinct_received, 1);
        assert_eq!(interval.duplicates, 1);
    }

    #[test]
    fn gap_length_is_the_longest_run_of_missing_sequences() {
        let mut rx = ControlReceiver::new();
        rx.on_sample(&sample(0, 1000), 1015);
        rx.on_sample(&sample(1, 1005), 1020);
        // 2..=6 lost: a run of five.
        rx.on_sample(&sample(7, 1035), 1050);
        rx.on_sample(&sample(8, 1040), 1055);
        // 9 lost: a run of one, which must not lower the maximum.
        rx.on_sample(&sample(10, 1050), 1065);
        assert_eq!(rx.max_gap(), 5);
    }

    /// The reorder-versus-loss distinction. A sample that arrives late still fills its
    /// hole, so the gap must be retracted — accumulating gaps on the forward path would
    /// permanently record a loss that never happened, on exactly the lossy and jittery
    /// cells where the two must be told apart.
    #[test]
    fn a_gap_filled_by_a_late_arrival_is_retracted() {
        let mut rx = ControlReceiver::new();
        rx.on_sample(&sample(0, 1000), 1015);
        // 1 and 2 are skipped: at this instant the forward path sees a gap of two.
        rx.on_sample(&sample(3, 1015), 1030);
        assert_eq!(rx.max_gap(), 2);

        // Both arrive late. The gap they filled is no longer a gap.
        rx.on_sample(&sample(1, 1005), 1035);
        rx.on_sample(&sample(2, 1010), 1040);
        assert_eq!(rx.max_gap(), 0);
        assert!(rx.gap_lengths().is_empty());
        assert_eq!(rx.gap_p99(), None);
    }

    #[test]
    fn a_partially_filled_gap_shrinks_to_what_is_still_missing() {
        let mut rx = ControlReceiver::new();
        rx.on_sample(&sample(0, 1000), 1015);
        rx.on_sample(&sample(4, 1020), 1035);
        assert_eq!(rx.max_gap(), 3);
        // 2 arrives late, splitting the run of three into two runs of one.
        rx.on_sample(&sample(2, 1010), 1040);
        assert_eq!(rx.max_gap(), 1);
        assert_eq!(rx.gap_lengths(), vec![1, 1]);
    }

    /// A p99 cannot be reconstructed from a maximum. With many single-sample gaps and one
    /// long burst, the p99 must reflect the typical gap, not the worst one.
    #[test]
    fn gap_p99_reflects_the_typical_gap_not_the_maximum() {
        let mut rx = ControlReceiver::new();
        let mut seq = 0u64;
        // 200 gaps of length one.
        for _ in 0..200 {
            rx.on_sample(&sample(seq, 1000 + seq * 5), 1015 + seq * 5);
            seq += 2;
        }
        // One burst: skip 50 consecutive sequence numbers, then resume.
        let resume = seq + 49;
        rx.on_sample(&sample(resume, 1000 + resume * 5), 1015 + resume * 5);

        assert_eq!(rx.max_gap(), 50);
        let p99 = rx.gap_p99().expect("gaps observed");
        assert!(p99 < 50, "p99 {p99} must not be the maximum");
        assert_eq!(p99, 1);
    }

    #[test]
    fn no_gaps_means_no_percentile() {
        let mut rx = ControlReceiver::new();
        for seq in 0..10 {
            rx.on_sample(&sample(seq, 1000 + seq * 5), 1015 + seq * 5);
        }
        assert_eq!(rx.gap_p99(), None);
        assert_eq!(rx.max_gap(), 0);
    }

    #[test]
    fn out_of_order_arrival_is_counted_and_not_a_gap() {
        let mut rx = ControlReceiver::new();
        rx.on_sample(&sample(0, 1000), 1015);
        rx.on_sample(&sample(2, 1010), 1025);
        rx.on_sample(&sample(1, 1005), 1030);
        let interval = rx.take_interval();
        assert_eq!(interval.reordered, 1);
        assert_eq!(interval.distinct_received, 3);
        assert_eq!(rx.distinct_received(), 3);
    }

    /// Jitter is computed from transit-time differences on a single clock pair, so a
    /// large constant offset between the hosts must not appear as jitter.
    #[test]
    fn jitter_is_immune_to_a_constant_clock_offset() {
        let mut steady = ControlReceiver::new();
        let mut offset = ControlReceiver::new();
        for i in 0..40u64 {
            let send = 1_000_000 + i * 5_000;
            steady.on_sample(&sample(i, send), send + 15_000);
            offset.on_sample(&sample(i, send), send + 15_000 + 9_000_000);
        }
        assert!(steady.jitter_ms() < 0.001);
        assert!((steady.jitter_ms() - offset.jitter_ms()).abs() < 0.001);
    }

    #[test]
    fn jitter_rises_with_variable_spacing() {
        let mut rx = ControlReceiver::new();
        let spacings = [15_000i64, 25_000, 15_000, 35_000, 15_000, 30_000];
        for (i, extra) in spacings.iter().enumerate() {
            let send = 1_000_000 + i as u64 * 5_000;
            rx.on_sample(&sample(i as u64, send), (send as i64 + extra) as u64);
        }
        assert!(rx.jitter_ms() > 0.5);
    }

    #[test]
    fn lateness_is_scored_against_the_window() {
        let mut rx = ControlReceiver::new();
        let window_us = 20_000;
        for owd in [5_000, 19_999, 20_000, 20_001, 50_000] {
            rx.score_lateness(owd, window_us);
        }
        assert_eq!(rx.late_eligible_count(), 5);
        assert_eq!(rx.late_count(), 2);
    }

    #[test]
    fn interval_counters_reset_on_take() {
        let mut rx = ControlReceiver::new();
        rx.on_sample(&sample(0, 1000), 1015);
        let first = rx.take_interval();
        assert_eq!(first.distinct_received, 1);
        assert_eq!(first.owd_raw_us.len(), 1);
        let second = rx.take_interval();
        assert_eq!(second.distinct_received, 0);
        assert!(second.owd_raw_us.is_empty());
        // Cumulative state survives the reset.
        assert_eq!(rx.distinct_received(), 1);
    }

    #[test]
    fn raw_owd_may_be_negative_when_receiver_clock_trails() {
        let mut rx = ControlReceiver::new();
        rx.on_sample(&sample(0, 5_000_000), 1_000_000);
        let interval = rx.take_interval();
        assert_eq!(interval.owd_raw_us, vec![-4_000_000]);
    }
}
