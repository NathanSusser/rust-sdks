//! Cumulative-counter discipline.
//!
//! Every counter in `RtcStats` is cumulative since subscription start. An interval value
//! requires differencing two consecutive readings, and a ratio requires differencing
//! *both* terms before dividing — dividing two lifetime cumulatives yields a session
//! average that hides exactly the transient a suite is looking for.
//!
//! The harness emits raw readings and lets the analysis layer difference them, but it
//! must difference `packets_lost` itself to detect the reorder artifact below, so the
//! primitives live here and are shared.

/// Result of differencing a signed cumulative counter that is allowed to move backwards.
///
/// `packets_lost` is `i64` and can decrease when a packet arrives late or duplicated: the
/// receiver revises its earlier estimate. The revision is not a gain, so the delta is
/// clamped at zero — but the clamp is a fact about the measurement and travels with the
/// data rather than only reaching a log line.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct ClampedDelta {
    /// The delta after clamping. Never negative.
    pub value: i64,
    /// The raw delta before clamping, present only when it was negative.
    pub clamped_from: Option<i64>,
}

impl ClampedDelta {
    /// Differences two readings of a counter that may legitimately move backwards.
    pub fn between(previous: i64, current: i64) -> Self {
        let raw = current.saturating_sub(previous);
        if raw < 0 {
            return Self { value: 0, clamped_from: Some(raw) };
        }
        Self { value: raw, clamped_from: None }
    }

    /// Whether this delta was revised upward from a negative reading.
    pub fn was_clamped(&self) -> bool {
        self.clamped_from.is_some()
    }
}

/// Differences two readings of a monotonic unsigned counter.
///
/// Returns `None` when the counter moved backwards, which for an unsigned cumulative
/// means the underlying stream was reset (a resubscribe) rather than that the value
/// decreased. A reset interval is not measurable and must not be reported as zero.
pub fn delta_u64(previous: u64, current: u64) -> Option<u64> {
    current.checked_sub(previous)
}

/// Differences two readings of a monotonic `f64` accumulator, such as a `total_*_time`.
///
/// Returns `None` on a backwards step, for the same reason as [`delta_u64`].
pub fn delta_f64(previous: f64, current: f64) -> Option<f64> {
    let delta = current - previous;
    (delta >= 0.0).then_some(delta)
}

/// Divides two interval deltas, guarding the denominator.
///
/// Both terms must already be deltas. Passing lifetime cumulatives here produces a
/// session average, which is the single most likely defect in a stats harness.
pub fn delta_ratio(numerator_delta: f64, denominator_delta: f64) -> Option<f64> {
    (denominator_delta > 0.0).then(|| numerator_delta / denominator_delta)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn positive_delta_passes_through_unclamped() {
        let d = ClampedDelta::between(100, 140);
        assert_eq!(d.value, 40);
        assert!(!d.was_clamped());
        assert_eq!(d.clamped_from, None);
    }

    /// A reorder or duplicate lets the receiver revise `packets_lost` downward. The
    /// interval loss is zero, and the fact that a revision happened is preserved.
    #[test]
    fn negative_delta_is_clamped_and_surfaced() {
        let d = ClampedDelta::between(140, 137);
        assert_eq!(d.value, 0);
        assert!(d.was_clamped());
        assert_eq!(d.clamped_from, Some(-3));
    }

    #[test]
    fn zero_delta_is_not_a_clamp() {
        let d = ClampedDelta::between(50, 50);
        assert_eq!(d.value, 0);
        assert!(!d.was_clamped());
    }

    #[test]
    fn unsigned_counter_reset_is_unmeasurable_not_zero() {
        assert_eq!(delta_u64(10, 25), Some(15));
        assert_eq!(delta_u64(25, 10), None);
    }

    #[test]
    fn float_accumulator_reset_is_unmeasurable() {
        assert_eq!(delta_f64(1.0, 2.5), Some(1.5));
        assert_eq!(delta_f64(2.5, 1.0), None);
        assert_eq!(delta_f64(2.0, 2.0), Some(0.0));
    }

    #[test]
    fn ratio_guards_the_denominator() {
        assert_eq!(delta_ratio(10.0, 4.0), Some(2.5));
        assert_eq!(delta_ratio(10.0, 0.0), None);
        assert_eq!(delta_ratio(10.0, -1.0), None);
    }

    /// The distinction the whole module exists for: a delta ratio over one interval must
    /// reflect that interval, not the session. Lifetime totals of 1000/100000 give 1%,
    /// while the last interval alone was 50/1000 = 5%.
    #[test]
    fn delta_ratio_reveals_a_transient_that_lifetime_ratio_hides() {
        let (lost_prev, recv_prev) = (950.0, 99_000.0);
        let (lost_now, recv_now) = (1000.0, 100_000.0);
        let lifetime = lost_now / recv_now * 100.0;
        let interval =
            delta_ratio(lost_now - lost_prev, recv_now - recv_prev).expect("nonzero") * 100.0;
        assert!(lifetime < 1.1);
        assert!(interval > 4.9);
    }
}
