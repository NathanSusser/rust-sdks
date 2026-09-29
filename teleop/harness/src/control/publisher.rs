//! The fixed-rate control publisher.
//!
//! Publishes body-state samples at the configured rate for the life of the run, logging
//! every sequence number it emits. That log is the delivered-share denominator: deriving
//! the expected count from *received* sequence numbers is self-referential and biased
//! toward passing, because loss at either edge of the scored window shrinks the observed
//! range by exactly the number of samples lost.

use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use std::time::Duration;

use parking_lot::Mutex;
use tokio::sync::mpsc;

use crate::clock::RunClock;
use crate::control::payload::{ControlSample, PROBE_TOKEN_NONE};
use crate::control::transport::ControlSender;
use crate::snapshot::PublishedSeq;
use crate::writer::JsonLinesWriter;

/// Counters the publisher shares with the sampler.
///
/// Published count is the harness-health denominator: a publisher that never reached its
/// configured rate measured itself, not the network, and the run is invalid rather than
/// failed.
#[derive(Debug, Default)]
pub struct PublisherCounters {
    seq_published: AtomicU64,
    send_failures: AtomicU64,
    probes_issued: AtomicU64,
}

impl PublisherCounters {
    /// Sequence numbers successfully handed to the transport.
    pub fn seq_published(&self) -> u64 {
        self.seq_published.load(Ordering::Relaxed)
    }

    /// Sends the transport rejected, typically a full queue.
    pub fn send_failures(&self) -> u64 {
        self.send_failures.load(Ordering::Relaxed)
    }

    /// Probe requests issued so far.
    pub fn probes_issued(&self) -> u64 {
        self.probes_issued.load(Ordering::Relaxed)
    }
}

/// A request to stamp the next control sample as a probe.
pub type ProbeRequest = u64;

/// Fixed-rate control publisher, owning its schedule and its sequence log.
pub struct ControlPublisher {
    sender: ControlSender,
    clock: RunClock,
    interval: Duration,
    duration: Duration,
    counters: Arc<PublisherCounters>,
    seq_log: Option<Arc<Mutex<JsonLinesWriter>>>,
    probe_rx: mpsc::Receiver<ProbeRequest>,
}

impl ControlPublisher {
    /// Creates a publisher that will emit for `duration` at one sample per `interval`.
    pub fn new(
        sender: ControlSender,
        clock: RunClock,
        interval: Duration,
        duration: Duration,
        counters: Arc<PublisherCounters>,
        seq_log: Option<Arc<Mutex<JsonLinesWriter>>>,
        probe_rx: mpsc::Receiver<ProbeRequest>,
    ) -> Self {
        Self { sender, clock, interval, duration, counters, seq_log, probe_rx }
    }

    /// Runs until the configured duration elapses.
    ///
    /// Scheduling is against absolute deadlines derived from the run origin rather than
    /// by sleeping for a fixed interval, so send latency does not accumulate into a
    /// steadily falling rate. When the loop falls behind by more than one interval it
    /// skips ahead to the next future deadline instead of emitting a burst: a burst would
    /// hide the shortfall that the harness-health metric exists to catch.
    pub async fn run(mut self) {
        let origin = self.clock.monotonic_origin();
        let mut seq: u64 = 0;

        loop {
            let elapsed = origin.elapsed();
            if elapsed >= self.duration {
                break;
            }

            let deadline = origin + slot_offset(self.interval, seq);
            let now = std::time::Instant::now();
            if deadline > now {
                tokio::time::sleep(deadline - now).await;
            } else if now.duration_since(deadline) > self.interval {
                // Behind by more than one slot: resynchronize to wall position rather
                // than emitting a catch-up burst.
                seq = slots_elapsed(self.interval, origin.elapsed());
            }

            let probe_token = self.probe_rx.try_recv().unwrap_or(PROBE_TOKEN_NONE);
            if probe_token != PROBE_TOKEN_NONE {
                self.counters.probes_issued.fetch_add(1, Ordering::Relaxed);
            }

            let t_send_unix_us = self.clock.wall_us();
            let sample = ControlSample { seq, t_send_unix_us, probe_token, pad: 0 };

            match self.sender.send(&sample.encode()).await {
                Ok(()) => {
                    self.counters.seq_published.fetch_add(1, Ordering::Relaxed);
                    self.log_seq(&sample);
                }
                Err(e) => {
                    self.counters.send_failures.fetch_add(1, Ordering::Relaxed);
                    log::debug!("control send failed at seq {seq}: {e}");
                }
            }

            seq = seq.wrapping_add(1);
        }

        log::info!(
            "control publisher finished: {} published, {} send failures, {} probes",
            self.counters.seq_published(),
            self.counters.send_failures(),
            self.counters.probes_issued()
        );
    }

    /// Appends one line to the publisher sequence log.
    ///
    /// Only samples that actually reached the transport are logged. Logging an attempted
    /// send would inflate the delivered-share denominator with samples the network never
    /// saw, converting a harness shortfall into apparent network loss.
    fn log_seq(&self, sample: &ControlSample) {
        let Some(log) = self.seq_log.as_ref() else {
            return;
        };
        let record = PublishedSeq {
            seq: sample.seq,
            t_send_unix_us: sample.t_send_unix_us,
            t_send_monotonic_us: self.clock.monotonic_us(),
            probe: sample.is_probe(),
        };
        match record.to_jsonl() {
            Ok(line) => {
                if let Err(e) = log.lock().write_line(&line) {
                    log::warn!("publisher seq log write failed: {e}");
                }
            }
            Err(e) => log::warn!("publisher seq log serialize failed: {e}"),
        }
    }
}

/// Offset of slot `index` from the run origin.
///
/// Computed in nanoseconds rather than by multiplying a `Duration` by a `u32`, which
/// overflows past roughly 720 000 slots — reached in a one-hour run at 200 Hz.
fn slot_offset(interval: Duration, index: u64) -> Duration {
    Duration::from_nanos(
        interval.as_nanos().saturating_mul(index as u128).min(u64::MAX as u128) as u64
    )
}

/// Slot index corresponding to an elapsed time, used to resynchronize after a stall.
fn slots_elapsed(interval: Duration, elapsed: Duration) -> u64 {
    let per_slot = interval.as_nanos().max(1);
    (elapsed.as_nanos() / per_slot) as u64
}

/// Share of the nominally expected samples the publisher failed to emit, as a percentage.
///
/// Measured against wall-clock expectation rather than against received data: this is a
/// statement about the harness, and conflating it with network loss would let a slow
/// client look like a bad network.
pub fn publish_shortfall_pct(seq_published: u64, rate_hz: u32, duration_s: f64) -> Option<f64> {
    let expected = rate_hz as f64 * duration_s;
    if expected <= 0.0 {
        return None;
    }
    let shortfall = 1.0 - (seq_published as f64 / expected);
    Some((shortfall * 100.0).max(0.0))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn shortfall_is_zero_when_the_rate_was_met() {
        assert_eq!(publish_shortfall_pct(24_000, 200, 120.0), Some(0.0));
    }

    /// The gate is 2%: a publisher that emitted 200 Hz x 120 s minus 5% did not measure
    /// the network.
    #[test]
    fn shortfall_reflects_a_slow_publisher() {
        let pct = publish_shortfall_pct(22_800, 200, 120.0).expect("expected > 0");
        assert!((pct - 5.0).abs() < 1e-9);
    }

    /// Overshoot is not negative shortfall. A publisher slightly ahead of schedule is
    /// still meeting its rate, and a negative percentage would be meaningless.
    #[test]
    fn overshoot_clamps_to_zero() {
        assert_eq!(publish_shortfall_pct(24_100, 200, 120.0), Some(0.0));
    }

    #[test]
    fn zero_duration_is_unmeasurable() {
        assert_eq!(publish_shortfall_pct(0, 200, 0.0), None);
        assert_eq!(publish_shortfall_pct(100, 0, 120.0), None);
    }

    /// A one-hour run at 200 Hz reaches 720 000 slots, which overflows a `Duration`
    /// multiplied by a `u32` slot count. The offset must stay exact well past that.
    #[test]
    fn slot_offsets_survive_a_long_run() {
        let interval = Duration::from_millis(5);
        assert_eq!(slot_offset(interval, 0), Duration::ZERO);
        assert_eq!(slot_offset(interval, 200), Duration::from_secs(1));
        assert_eq!(slot_offset(interval, 720_000), Duration::from_secs(3600));
    }

    #[test]
    fn slots_elapsed_is_the_inverse_of_slot_offset() {
        let interval = Duration::from_millis(5);
        for index in [0u64, 1, 199, 200, 720_000] {
            assert_eq!(slots_elapsed(interval, slot_offset(interval, index)), index);
        }
    }

    #[test]
    fn counters_start_at_zero() {
        let counters = PublisherCounters::default();
        assert_eq!(counters.seq_published(), 0);
        assert_eq!(counters.send_failures(), 0);
        assert_eq!(counters.probes_issued(), 0);
    }
}
