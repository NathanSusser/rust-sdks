//! Dual time base.
//!
//! Every snapshot and every stamped packet carries a monotonic reading and a wallclock
//! reading. Only the wallclock is comparable across hosts, and only after clock-offset
//! correction; only the monotonic reading is safe for measuring an interval, because
//! wallclock can step. Recording just one loses information that cannot be recovered
//! after the run.

use std::time::{Instant, SystemTime, UNIX_EPOCH};

/// Wall-clock microseconds since the Unix epoch.
///
/// Saturates at zero for pre-epoch clocks rather than panicking; a machine whose clock is
/// set before 1970 has a problem the harness cannot fix, and the run record will show it
/// through `clock_sync_confidence`.
pub fn unix_micros() -> u64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_micros() as u64).unwrap_or(0)
}

/// Paired monotonic and wallclock origin for a run, used to stamp every emitted record.
#[derive(Debug, Clone)]
pub struct RunClock {
    monotonic_origin: Instant,
    wall_origin_us: u64,
}

impl RunClock {
    /// Captures both time bases at the same moment.
    pub fn start() -> Self {
        Self { monotonic_origin: Instant::now(), wall_origin_us: unix_micros() }
    }

    /// Monotonic microseconds elapsed since [`RunClock::start`].
    pub fn monotonic_us(&self) -> u64 {
        self.monotonic_origin.elapsed().as_micros() as u64
    }

    /// Current wall-clock microseconds since the Unix epoch.
    pub fn wall_us(&self) -> u64 {
        unix_micros()
    }

    /// Wall-clock microseconds captured at [`RunClock::start`].
    pub fn wall_origin_us(&self) -> u64 {
        self.wall_origin_us
    }

    /// The monotonic origin, for callers that need to schedule against it.
    pub fn monotonic_origin(&self) -> Instant {
        self.monotonic_origin
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn monotonic_advances_and_wall_origin_is_fixed() {
        let clock = RunClock::start();
        let origin = clock.wall_origin_us();
        std::thread::sleep(std::time::Duration::from_millis(5));
        assert!(clock.monotonic_us() >= 5_000);
        assert_eq!(clock.wall_origin_us(), origin);
        assert!(clock.wall_us() >= origin);
    }

    #[test]
    fn unix_micros_is_after_2020() {
        // 2020-01-01T00:00:00Z in microseconds; catches an unset or wildly wrong clock.
        assert!(unix_micros() > 1_577_836_800_000_000);
    }
}
