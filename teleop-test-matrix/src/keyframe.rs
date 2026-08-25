//! Keyframe service time, measured in poll intervals.
//!
//! The Rust SDK exposes no picture-loss-indication callback, so recovery timing can only
//! be measured by differencing `pli_count` and `key_frames_encoded` between polls. The
//! measurement resolution is therefore exactly the poll period, and the quantity being
//! measured is "how many polls elapsed" — an integer.
//!
//! That is why the result is reported as a distribution of poll intervals plus the
//! observed maximum, never as a millisecond percentile. At 1 Hz every realizable value is
//! a multiple of 1000 ms; calling a p95 over a handful of such values a "p95 in
//! milliseconds" implies a precision the method cannot deliver. Converting to time is a
//! quoting decision for the report, and it is quoted as a bound.

/// Tracks the number of polls between a picture-loss indication and the next keyframe.
#[derive(Debug, Default)]
pub struct KeyframeServiceTracker {
    /// Polls elapsed since the outstanding request, or `None` when none is outstanding.
    polls_since_request: Option<u64>,
    completed: Vec<u64>,
    /// Requests still outstanding when the run ended, which never saw a keyframe.
    unserviced: u64,
}

impl KeyframeServiceTracker {
    /// Creates an empty tracker.
    pub fn new() -> Self {
        Self::default()
    }

    /// Advances one poll, given this poll's deltas.
    ///
    /// A keyframe in the same poll as the request scores zero intervals: the recovery
    /// completed within one poll period, and the resolution cannot say more than that.
    pub fn observe_poll(&mut self, pli_delta: u64, key_frames_delta: u64) {
        if let Some(polls) = self.polls_since_request {
            // One more poll period has elapsed since the request was seen.
            let elapsed = polls + 1;
            if key_frames_delta > 0 {
                self.completed.push(elapsed);
                self.polls_since_request = None;
            } else {
                self.polls_since_request = Some(elapsed);
            }
        }

        if pli_delta > 0 && self.polls_since_request.is_none() {
            if key_frames_delta > 0 {
                // Request and keyframe landed in the same poll: serviced within one period.
                self.completed.push(0);
            } else {
                self.polls_since_request = Some(0);
            }
        }
    }

    /// Closes the tracker, counting any outstanding request as unserviced.
    pub fn finish(&mut self) {
        if self.polls_since_request.take().is_some() {
            self.unserviced += 1;
        }
    }

    /// Every completed service time, in poll intervals.
    pub fn completed_polls(&self) -> &[u64] {
        &self.completed
    }

    /// Largest completed service time, in poll intervals.
    pub fn max_polls(&self) -> Option<u64> {
        self.completed.iter().copied().max()
    }

    /// Requests that never saw a keyframe.
    pub fn unserviced(&self) -> u64 {
        self.unserviced
    }

    /// Converts a poll-interval count to a millisecond upper bound at the given cadence.
    ///
    /// This is a bound, not a measurement: a value of two polls at 10 Hz means recovery
    /// completed within 200 ms, not that it took 200 ms.
    pub fn polls_to_ms_bound(polls: u64, poll_hz: f64) -> Option<f64> {
        (poll_hz > 0.0).then(|| polls as f64 * 1000.0 / poll_hz)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn keyframe_in_the_same_poll_scores_zero_intervals() {
        let mut tracker = KeyframeServiceTracker::new();
        tracker.observe_poll(1, 1);
        assert_eq!(tracker.completed_polls(), &[0]);
        assert_eq!(tracker.max_polls(), Some(0));
    }

    /// Request at poll 0, keyframe at poll 2: two poll periods elapsed between them.
    /// The count is of elapsed intervals, not of polls touched.
    #[test]
    fn counts_polls_until_the_keyframe_arrives() {
        let mut tracker = KeyframeServiceTracker::new();
        tracker.observe_poll(1, 0);
        tracker.observe_poll(0, 0);
        tracker.observe_poll(0, 1);
        assert_eq!(tracker.completed_polls(), &[2]);
    }

    #[test]
    fn tracks_several_recovery_events() {
        let mut tracker = KeyframeServiceTracker::new();
        // Request at poll 0, serviced at poll 1: one interval.
        tracker.observe_poll(1, 0);
        tracker.observe_poll(0, 1);
        // Request at poll 2, serviced at poll 4: two intervals.
        tracker.observe_poll(1, 0);
        tracker.observe_poll(0, 0);
        tracker.observe_poll(0, 1);
        assert_eq!(tracker.completed_polls(), &[1, 2]);
        assert_eq!(tracker.max_polls(), Some(2));
    }

    /// A request left outstanding at end of run is not a fast recovery. Counting it as
    /// completed would bias the distribution toward looking healthy.
    #[test]
    fn outstanding_request_is_unserviced_not_completed() {
        let mut tracker = KeyframeServiceTracker::new();
        tracker.observe_poll(1, 0);
        tracker.observe_poll(0, 0);
        tracker.finish();
        assert!(tracker.completed_polls().is_empty());
        assert_eq!(tracker.unserviced(), 1);
        assert_eq!(tracker.max_polls(), None);
    }

    #[test]
    fn quiet_polls_produce_nothing() {
        let mut tracker = KeyframeServiceTracker::new();
        for _ in 0..10 {
            tracker.observe_poll(0, 0);
        }
        tracker.finish();
        assert!(tracker.completed_polls().is_empty());
        assert_eq!(tracker.unserviced(), 0);
    }

    /// A keyframe with no outstanding request is a normal periodic keyframe, not a
    /// recovery, and must not enter the distribution.
    #[test]
    fn unsolicited_keyframes_are_ignored() {
        let mut tracker = KeyframeServiceTracker::new();
        tracker.observe_poll(0, 1);
        tracker.observe_poll(0, 1);
        assert!(tracker.completed_polls().is_empty());
    }

    /// The conversion is a bound tied to the cadence, which is why the cadence is
    /// recorded per run. The same two polls mean 2000 ms at 1 Hz and 200 ms at 10 Hz.
    #[test]
    fn millisecond_conversion_depends_on_cadence() {
        assert_eq!(KeyframeServiceTracker::polls_to_ms_bound(2, 1.0), Some(2000.0));
        assert_eq!(KeyframeServiceTracker::polls_to_ms_bound(2, 10.0), Some(200.0));
        assert_eq!(KeyframeServiceTracker::polls_to_ms_bound(2, 0.0), None);
    }
}
