//! Four-timestamp probe RTT and clock-offset (theta) estimation.
//!
//! Neither quantity exists in `RtcStats`. `CandidatePair.current_round_trip_time` is
//! STUN-consent RTT and `RemoteInboundRtp.round_trip_time` is RTCP on the video path;
//! neither is the control-path RTT the latency bar is scored against. Both are still
//! recorded elsewhere as corroborators, so an error here is visible rather than silent.

use std::collections::BTreeMap;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};

use parking_lot::Mutex;
use tokio::sync::mpsc;

use crate::clock::RunClock;
use crate::control::payload::ProbeEcho;

/// Default lifetime of an unanswered probe, in microseconds.
///
/// Chosen well above any round trip this harness expects to measure so a slow but genuine
/// echo is never mistaken for loss, and short enough that a probe lost to the network is
/// still retired within the run.
pub const DEFAULT_PROBE_LIFETIME_US: u64 = 2_000_000;

/// Rolling one-way-delay window depth, matching the reference implementation.
const OWD_RING_SIZE: usize = 64;

/// Minimum one-way-delay samples before a theta estimate may be published.
const MIN_SAMPLES_FOR_VALID: usize = 8;

/// Ceiling on probes tracked concurrently.
///
/// Only a real stall can push the in-flight set this deep, since a probe is retired the
/// moment it exceeds its lifetime. The bound exists so a peer that stops echoing entirely
/// cannot grow the map without limit over a long run.
const MAX_OUTSTANDING: usize = 256;

/// How well the two hosts' clocks are related, and therefore whether any one-way figure
/// may be published at all.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ClockSyncConfidence {
    /// No usable estimate. One-way and glass-to-glass columns are suppressed; the run
    /// remains valid for round-trip and video metrics.
    None,
    /// Estimated from the harness's own probe exchanges.
    Probe,
    /// Supplied by an external time source such as PTP. Not produced by this harness.
    External,
}

impl ClockSyncConfidence {
    /// Lowercase name as it appears in the run record.
    pub fn as_str(self) -> &'static str {
        match self {
            Self::None => "none",
            Self::Probe => "probe",
            Self::External => "external",
        }
    }
}

/// Probe RTT accounting and clock-offset estimation over one control transport.
///
/// One-way samples arrive at the control rate (200 Hz) while probes complete at the probe
/// rate, so the two populations span very different intervals. Pairing minima across
/// independent windows would let path-condition drift masquerade as clock offset; this
/// estimator instead pairs the current one-way minimum with the RTT of the *same* probe
/// exchange, so queueing that inflates one term inflates the other and largely cancels.
///
/// Several probes may be in flight at once, and that is what makes a probe interval
/// shorter than the round-trip time usable. Retiring the previous probe whenever a new one
/// is issued would discard every echo that merely arrived after its successor left, so
/// raising the probe rate past `1 / rtt` would drive the completion count *down* while
/// reporting the loss as if the network had caused it. Liveness instead comes from an
/// explicit lifetime: a probe is lost only once it has genuinely aged out.
///
/// The one-way ring is a fixed-size array rather than a `Vec` so that a
/// `Default`-constructed tracker is always usable. A heap-allocated ring would have to be
/// sized in `new`, leaving `Default` with an empty ring that panics on first write.
#[derive(Debug)]
pub struct ProbeTracker {
    rtt_samples_us: Vec<u64>,
    owd_ring: [i64; OWD_RING_SIZE],
    owd_next: usize,
    owd_filled: usize,
    theta_us: Option<i64>,
    probes_sent: u64,
    probes_completed: u64,
    probes_lost: u64,
    next_token: u64,
    /// Probes awaiting an echo, keyed by token, valued by send time. Insertion-ordered so
    /// expiry can stop at the first probe still within its lifetime.
    outstanding: BTreeMap<u64, u64>,
    /// How long a probe may await its echo before it is counted lost.
    lifetime_us: u64,
}

impl Default for ProbeTracker {
    fn default() -> Self {
        Self::new()
    }
}

impl ProbeTracker {
    /// Creates an empty tracker using [`DEFAULT_PROBE_LIFETIME_US`].
    pub fn new() -> Self {
        Self::with_lifetime_us(DEFAULT_PROBE_LIFETIME_US)
    }

    /// Creates an empty tracker whose unanswered probes are retired after `lifetime_us`.
    pub fn with_lifetime_us(lifetime_us: u64) -> Self {
        Self {
            rtt_samples_us: Vec::new(),
            owd_ring: [0; OWD_RING_SIZE],
            owd_next: 0,
            owd_filled: 0,
            theta_us: None,
            probes_sent: 0,
            probes_completed: 0,
            probes_lost: 0,
            // Zero is reserved to mean "not a probe" in the control payload.
            next_token: 1,
            outstanding: BTreeMap::new(),
            lifetime_us,
        }
    }

    /// Reserves the next probe token without counting a probe as sent.
    ///
    /// Split from [`ProbeTracker::begin_probe`] so a caller that may fail to dispatch the
    /// probe does not inflate the sent count with one that never left the process.
    pub fn next_token(&mut self) -> u64 {
        let token = self.next_token;
        // Zero is reserved to mean "not a probe" in the control payload.
        self.next_token = self.next_token.wrapping_add(1).max(1);
        token
    }

    /// Records a probe as sent, retiring any probe that has outlived its lifetime.
    ///
    /// Issuing a probe does *not* retire its predecessor. At a probe interval shorter than
    /// the round trip several are legitimately in flight at once, and retiring on issue
    /// would count each of them lost moments before its echo arrived — turning a raised
    /// probe rate into fabricated loss and *fewer* completed samples.
    pub fn begin_probe(&mut self, token: u64, t0_us: u64) {
        self.expire_outstanding(t0_us);
        self.probes_sent += 1;
        self.outstanding.insert(token, t0_us);
        // A peer that has stopped echoing cannot grow the map without bound. Tokens
        // ascend with send time, so the lowest key is always the oldest probe.
        while self.outstanding.len() > MAX_OUTSTANDING {
            let Some(&oldest) = self.outstanding.keys().next() else {
                break;
            };
            self.outstanding.remove(&oldest);
            self.probes_lost += 1;
        }
    }

    /// Retires every outstanding probe older than the configured lifetime.
    ///
    /// Tokens ascend with send time, so iteration stops at the first probe still within
    /// its lifetime rather than scanning the whole map.
    fn expire_outstanding(&mut self, now_us: u64) {
        let expired: Vec<u64> = self
            .outstanding
            .iter()
            .take_while(|(_, &sent_us)| now_us.saturating_sub(sent_us) > self.lifetime_us)
            .map(|(&token, _)| token)
            .collect();
        for token in expired {
            self.outstanding.remove(&token);
            self.probes_lost += 1;
        }
    }

    /// Records a raw one-way delay observation: peer receive time minus sender send time,
    /// uncorrected, on two different clocks. Negative values are expected and meaningful
    /// when the receiver's clock trails the sender's.
    pub fn record_owd(&mut self, raw_owd_us: i64) {
        self.owd_ring[self.owd_next] = raw_owd_us;
        self.owd_next = (self.owd_next + 1) % OWD_RING_SIZE;
        self.owd_filled = (self.owd_filled + 1).min(OWD_RING_SIZE);
    }

    /// Completes a probe exchange and refreshes the theta estimate.
    ///
    /// Returns the measured round-trip time in microseconds, or `None` when the echo does
    /// not match the outstanding probe or the timestamps are inconsistent.
    pub fn complete_probe(&mut self, echo: &ProbeEcho, t3_us: u64) -> Option<u64> {
        if !self.outstanding.contains_key(&echo.token) {
            return None;
        }
        // The round trip is derived before the probe is retired, so an echo carrying
        // inconsistent timestamps leaves it outstanding to age out rather than silently
        // counting as completed.
        let rtt_us = echo.rtt_us(t3_us)?;
        self.outstanding.remove(&echo.token);
        self.probes_completed += 1;
        self.rtt_samples_us.push(rtt_us);

        // Pair the two minima by construction: the one-way minimum as it stands at this
        // instant, against the round trip of this same exchange.
        if self.owd_filled >= MIN_SAMPLES_FOR_VALID {
            let min_owd_us = self.min_owd_us()?;
            self.theta_us = Some(min_owd_us - (rtt_us as i64) / 2);
        }
        Some(rtt_us)
    }

    /// Smallest one-way observation currently in the ring.
    fn min_owd_us(&self) -> Option<i64> {
        self.owd_ring[..self.owd_filled].iter().copied().min()
    }

    /// Current clock-offset estimate in microseconds, if enough samples have accumulated.
    pub fn theta_us(&self) -> Option<i64> {
        self.theta_us
    }

    /// Current clock-offset estimate in milliseconds.
    pub fn theta_ms(&self) -> Option<f64> {
        self.theta_us.map(|us| us as f64 / 1000.0)
    }

    /// Confidence in the current estimate.
    pub fn confidence(&self) -> ClockSyncConfidence {
        match self.theta_us {
            Some(_) => ClockSyncConfidence::Probe,
            None => ClockSyncConfidence::None,
        }
    }

    /// Corrects a raw one-way delay by the current offset estimate.
    ///
    /// Returns `None` while the estimate is invalid, so a caller cannot accidentally
    /// publish an uncorrected one-way figure.
    pub fn correct_owd_us(&self, raw_owd_us: i64) -> Option<i64> {
        self.theta_us.map(|theta| raw_owd_us - theta)
    }

    /// Probes issued so far.
    pub fn probes_sent(&self) -> u64 {
        self.probes_sent
    }

    /// Probes that produced a usable round-trip measurement.
    pub fn probes_completed(&self) -> u64 {
        self.probes_completed
    }

    /// Probes retired unanswered because they outlived the probe lifetime.
    pub fn probes_lost(&self) -> u64 {
        self.probes_lost
    }

    /// Probes issued but not yet answered or aged out.
    pub fn probes_in_flight(&self) -> usize {
        self.outstanding.len()
    }

    /// Share of probes that never completed, as a percentage.
    ///
    /// The numerator is the explicit aged-out count, not `sent - completed`: the latter
    /// also counts every probe still in flight, which has not yet had the chance to be
    /// answered. At a probe interval below the round trip that difference is several
    /// probes at any instant, so the two forms are not interchangeable.
    pub fn loss_pct(&self) -> Option<f64> {
        if self.probes_sent == 0 {
            return None;
        }
        Some(self.probes_lost as f64 / self.probes_sent as f64 * 100.0)
    }

    /// All completed round-trip measurements, in microseconds.
    pub fn rtt_samples_us(&self) -> &[u64] {
        &self.rtt_samples_us
    }
}

/// Owner of the probe tracker a [`ProbeLoop`] drives.
///
/// Exists so the loop can borrow the tracker out of the run's shared state without taking
/// a second `Arc` to it, which would leave two owners of one tracker.
pub trait ProbeHost: Send + Sync + 'static {
    /// The tracker this host owns.
    fn tracker(&self) -> &Mutex<ProbeTracker>;
}

/// Issues probes on a fixed cadence of its own, independent of the stats poll.
///
/// The probe rate was previously the stats poll rate, which capped it at 1 Hz and produced
/// too few samples for the percentiles the latency bar is scored against. Probing is not a
/// sampling concern — it needs its own cadence, and coupling the two also meant a slow
/// stats RPC delayed the next probe and biased the interval it measured.
pub struct ProbeLoop<S> {
    shared: Arc<S>,
    clock: RunClock,
    probe_tx: mpsc::Sender<u64>,
    interval: Duration,
    duration: Duration,
    shutdown: Arc<AtomicBool>,
}

impl<S: ProbeHost> ProbeLoop<S> {
    /// Creates a probe loop issuing one probe every `interval` for `duration`.
    pub fn new(
        shared: Arc<S>,
        clock: RunClock,
        probe_tx: mpsc::Sender<u64>,
        interval: Duration,
        duration: Duration,
        shutdown: Arc<AtomicBool>,
    ) -> Self {
        Self { shared, clock, probe_tx, interval, duration, shutdown }
    }

    /// Issues probes until the run duration elapses.
    pub async fn run(self) {
        let origin = self.clock.monotonic_origin();
        let mut index: u64 = 0;
        while origin.elapsed() < self.duration && !self.shutdown.load(Ordering::Acquire) {
            // Absolute deadlines rather than a fixed sleep, so the time each issue costs
            // is not added to every subsequent interval and the rate does not drift down.
            let deadline = origin + self.interval.saturating_mul(index as u32);
            let now = Instant::now();
            if deadline > now {
                tokio::time::sleep(deadline - now).await;
            }
            self.issue();
            index += 1;
        }
    }

    /// Issues one probe, counting it as sent only once it is actually queued.
    ///
    /// The token has to reach the control publisher to leave the process at all. Counting
    /// a probe that never got queued would inflate the loss share with probes the network
    /// never saw, turning a full local channel into apparent packet loss.
    fn issue(&self) {
        let token = { self.shared.tracker().lock().next_token() };
        match self.probe_tx.try_send(token) {
            Ok(()) => self.shared.tracker().lock().begin_probe(token, self.clock.wall_us()),
            Err(e) => log::debug!("probe not queued, not counting it as sent: {e}"),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Builds an exchange with a known true RTT and a known peer clock offset.
    fn exchange(
        token: u64,
        t0_us: u64,
        rtt_us: u64,
        peer_offset_us: i64,
        dwell_us: u64,
    ) -> ProbeEcho {
        let one_way = rtt_us / 2;
        let t1 = (t0_us as i64 + one_way as i64 + peer_offset_us) as u64;
        ProbeEcho { token, t0_us, t1_us: t1, t2_us: t1 + dwell_us }
    }

    /// A `Default`-constructed tracker must be immediately usable. The shared run state
    /// derives `Default`, so a tracker that only became valid via `new` panicked on the
    /// first received control sample.
    /// Issues and records a probe in one step, as a caller that always dispatches
    /// successfully would.
    fn issue(tracker: &mut ProbeTracker, t0_us: u64) -> u64 {
        let token = tracker.next_token();
        tracker.begin_probe(token, t0_us);
        token
    }

    #[test]
    fn default_tracker_accepts_samples_without_panicking() {
        let mut tracker = ProbeTracker::default();
        for i in 0..(OWD_RING_SIZE * 2) {
            tracker.record_owd(i as i64);
        }
        assert_eq!(tracker.confidence(), ClockSyncConfidence::None);
        assert_eq!(tracker.probes_sent(), 0);
        // Token zero means "not a probe", so the first issued token must not be zero.
        assert_ne!(tracker.next_token(), 0);
    }

    #[test]
    fn theta_needs_minimum_samples() {
        let mut tracker = ProbeTracker::new();
        for _ in 0..(MIN_SAMPLES_FOR_VALID - 1) {
            tracker.record_owd(20_000);
        }
        let token = issue(&mut tracker, 1_000_000);
        let echo = exchange(token, 1_000_000, 30_000, 0, 1_000);
        tracker.complete_probe(&echo, 1_000_000 + 30_000 + 1_000);
        assert_eq!(tracker.theta_us(), None);
        assert_eq!(tracker.confidence(), ClockSyncConfidence::None);
        assert_eq!(tracker.correct_owd_us(20_000), None);
    }

    /// With a symmetric path, theta must recover the peer's clock offset. Peer clock is
    /// 5 s ahead; true one-way is 15 ms, so raw one-way observations read 5.015 s.
    #[test]
    fn theta_recovers_a_constant_offset() {
        let mut tracker = ProbeTracker::new();
        let peer_offset_us = 5_000_000;
        let true_owd_us = 15_000;
        for _ in 0..16 {
            tracker.record_owd(peer_offset_us + true_owd_us);
        }
        let t0 = 1_000_000;
        let token = issue(&mut tracker, t0);
        let echo = exchange(token, t0, 30_000, peer_offset_us, 1_000);
        let rtt = tracker.complete_probe(&echo, t0 + 30_000 + 1_000).expect("probe completes");
        assert_eq!(rtt, 30_000);
        // theta = min_owd - rtt/2 = (5_000_000 + 15_000) - 15_000 = 5_000_000
        assert_eq!(tracker.theta_us(), Some(peer_offset_us));
        assert_eq!(tracker.confidence(), ClockSyncConfidence::Probe);
        // A raw observation of offset + 15 ms corrects back to the true one-way delay.
        assert_eq!(tracker.correct_owd_us(peer_offset_us + true_owd_us), Some(true_owd_us));
    }

    /// A congested probe inflates both the one-way minimum and its own RTT. Because the
    /// two are paired, the offset estimate must not absorb the congestion.
    #[test]
    fn paired_minima_reject_path_drift() {
        let mut tracker = ProbeTracker::new();
        let peer_offset_us = 1_000_000;
        // Every one-way observation is inflated by 40 ms of queueing on top of a 15 ms path.
        for _ in 0..16 {
            tracker.record_owd(peer_offset_us + 15_000 + 40_000);
        }
        let t0 = 2_000_000;
        let token = issue(&mut tracker, t0);
        // The probe sees the same queueing: round trip is 2 x (15 + 40) ms.
        let echo = exchange(token, t0, 110_000, peer_offset_us + 40_000, 0);
        tracker.complete_probe(&echo, t0 + 110_000).expect("probe completes");
        // theta = (offset + 55_000) - 55_000 = offset. Queueing cancelled.
        assert_eq!(tracker.theta_us(), Some(peer_offset_us));
    }

    #[test]
    fn mismatched_token_is_ignored() {
        let mut tracker = ProbeTracker::new();
        let token = issue(&mut tracker, 100);
        let wrong = ProbeEcho { token: token + 99, t0_us: 100, t1_us: 110, t2_us: 111 };
        assert_eq!(tracker.complete_probe(&wrong, 130), None);
        assert_eq!(tracker.probes_completed(), 0);
    }

    /// A probe is lost only once it has outlived its lifetime. A probe still within it is
    /// not counted lost — it has not yet had the chance to be answered, and
    /// `sent - completed` would wrongly include it.
    #[test]
    fn loss_accounting_counts_aged_out_probes_only() {
        let mut tracker = ProbeTracker::with_lifetime_us(500_000);
        assert_eq!(tracker.loss_pct(), None);
        for i in 0..4 {
            let t0 = 1_000_000 + i * 1_000_000;
            let token = issue(&mut tracker, t0);
            if i % 2 == 0 {
                let echo = exchange(token, t0, 20_000, 0, 0);
                tracker.complete_probe(&echo, t0 + 20_000).expect("completes");
            }
        }
        assert_eq!(tracker.probes_sent(), 4);
        assert_eq!(tracker.probes_completed(), 2);
        // Probe 1 aged out when probe 2 was issued a full second later. Probe 3 is still
        // outstanding at the end and is not lost.
        assert_eq!(tracker.probes_lost(), 1);
        assert_eq!(tracker.probes_in_flight(), 1);
        assert_eq!(tracker.loss_pct(), Some(25.0));
    }

    /// The regression this rewrite exists for. At a probe interval below the round trip
    /// several probes are legitimately in flight, and every one of them must complete.
    /// The previous single-slot tracker retired each probe as its successor was issued, so
    /// raising the probe rate drove the completion count *down* and reported the discarded
    /// probes as network loss.
    #[test]
    fn probes_faster_than_the_round_trip_all_complete() {
        let mut tracker = ProbeTracker::new();
        // 100 ms round trip against a 20 ms probe interval: five in flight at steady state.
        let interval_us = 20_000;
        let rtt_us = 100_000;
        let mut issued = Vec::new();
        for i in 0..25u64 {
            let t0 = 1_000_000 + i * interval_us;
            issued.push((issue(&mut tracker, t0), t0));
            // Echo whichever probe's round trip has now elapsed.
            if let Some(&(token, sent)) = issued.iter().find(|&&(_, s)| s + rtt_us == t0) {
                let echo = exchange(token, sent, rtt_us, 0, 0);
                assert_eq!(
                    tracker.complete_probe(&echo, sent + rtt_us),
                    Some(rtt_us),
                    "probe issued at {sent} must still be outstanding when its echo lands"
                );
            }
        }
        assert_eq!(tracker.probes_sent(), 25);
        assert_eq!(tracker.probes_completed(), 20);
        // Nothing aged out: the lifetime is far above the round trip.
        assert_eq!(tracker.probes_lost(), 0);
        assert_eq!(tracker.probes_in_flight(), 5);
        assert!(tracker.rtt_samples_us().iter().all(|&r| r == rtt_us));
    }

    /// Echoes may arrive out of order under reordering; each must match its own probe
    /// rather than whichever happens to be newest.
    #[test]
    fn out_of_order_echoes_match_their_own_probe() {
        let mut tracker = ProbeTracker::new();
        let first = issue(&mut tracker, 1_000_000);
        let second = issue(&mut tracker, 1_020_000);
        // The second probe's echo lands first.
        let e2 = exchange(second, 1_020_000, 40_000, 0, 0);
        assert_eq!(tracker.complete_probe(&e2, 1_060_000), Some(40_000));
        let e1 = exchange(first, 1_000_000, 90_000, 0, 0);
        assert_eq!(tracker.complete_probe(&e1, 1_090_000), Some(90_000));
        assert_eq!(tracker.probes_completed(), 2);
        assert_eq!(tracker.probes_lost(), 0);
    }

    /// A duplicated echo must not be counted twice, or probe loss reads negative.
    #[test]
    fn a_repeated_echo_completes_once() {
        let mut tracker = ProbeTracker::new();
        let token = issue(&mut tracker, 1_000_000);
        let echo = exchange(token, 1_000_000, 30_000, 0, 0);
        assert_eq!(tracker.complete_probe(&echo, 1_030_000), Some(30_000));
        assert_eq!(tracker.complete_probe(&echo, 1_030_000), None);
        assert_eq!(tracker.probes_completed(), 1);
    }

    /// A peer that stops echoing entirely must not grow the outstanding set without bound.
    #[test]
    fn outstanding_set_is_bounded() {
        // A lifetime longer than the whole test, so only the cap can retire a probe.
        let mut tracker = ProbeTracker::with_lifetime_us(u64::MAX);
        for i in 0..(MAX_OUTSTANDING as u64 + 50) {
            issue(&mut tracker, 1_000_000 + i);
        }
        assert_eq!(tracker.probes_in_flight(), MAX_OUTSTANDING);
        assert_eq!(tracker.probes_lost(), 50);
    }

    /// A token that is reserved but never dispatched must not count as sent, or a full
    /// local channel would read as network loss.
    #[test]
    fn reserving_a_token_does_not_count_a_probe_as_sent() {
        let mut tracker = ProbeTracker::new();
        let first = tracker.next_token();
        let second = tracker.next_token();
        assert_ne!(first, second);
        assert_eq!(tracker.probes_sent(), 0);
        assert_eq!(tracker.loss_pct(), None);

        tracker.begin_probe(second, 1_000);
        assert_eq!(tracker.probes_sent(), 1);
    }

    /// The ring holds a bounded window; older observations must fall out of it.
    #[test]
    fn owd_ring_is_bounded() {
        let mut tracker = ProbeTracker::new();
        tracker.record_owd(1);
        for _ in 0..OWD_RING_SIZE {
            tracker.record_owd(500_000);
        }
        assert_eq!(tracker.min_owd_us(), Some(500_000));
    }
}
