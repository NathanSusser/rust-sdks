use anyhow::{bail, Result};
use std::{
    fmt,
    fs::File,
    io::{BufWriter, Write},
    path::Path,
};

/// Inclusive frame-ID bounds for per-frame CSV logging.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub(crate) struct FrameLogRange {
    start: Option<u32>,
    end: Option<u32>,
}

impl FrameLogRange {
    /// Validates optional inclusive frame-ID bounds.
    pub(crate) fn new(start: Option<u32>, end: Option<u32>) -> Result<Self> {
        if let (Some(start), Some(end)) = (start, end) {
            if start > end {
                bail!("--log-start-frame-id ({start}) must not exceed --log-end-frame-id ({end})");
            }
        }
        Ok(Self { start, end })
    }

    /// Returns whether a frame ID falls within the configured inclusive bounds.
    pub(crate) fn contains(self, frame_id: u32) -> bool {
        self.start.is_none_or(|start| frame_id >= start)
            && self.end.is_none_or(|end| frame_id <= end)
    }

    /// Returns the frame ID immediately before an explicit start bound, when representable.
    pub(crate) fn previous_to_start(self) -> Option<u32> {
        self.start.and_then(|start| start.checked_sub(1))
    }

    /// Returns whether this frame ID is at or past the configured end bound.
    ///
    /// Deliberately `>=` rather than `==`. This drives publisher shutdown, and
    /// an equality test makes shutdown contingent on one specific frame ID
    /// surviving the whole capture->packetize pipeline. Arm 2b encoded roughly
    /// one frame in twenty-six, so the end frame had about a 4% chance of being
    /// one of the survivors; it was not, and the publisher ran 19 minutes past
    /// its window still publishing into the room. A publisher that outlives its
    /// window silently corrupts the *next* run, which is how a3r1 was lost.
    pub(crate) fn reaches_end(self, frame_id: u32) -> bool {
        self.end.is_some_and(|end| frame_id >= end)
    }
}

/// Creates a buffered CSV file, including missing parent directories, and writes its header.
pub(crate) fn create_csv(path: &Path, header: &str) -> std::io::Result<BufWriter<File>> {
    if let Some(parent) = path.parent().filter(|parent| !parent.as_os_str().is_empty()) {
        std::fs::create_dir_all(parent)?;
    }
    let mut writer = BufWriter::new(File::create(path)?);
    writeln!(writer, "{header}")?;
    writer.flush()?;
    Ok(writer)
}

/// Displays an optional CSV cell without adding quoting or placeholder text.
pub(crate) struct CsvOption<T>(pub(crate) Option<T>);

impl<T: fmt::Display> fmt::Display for CsvOption<T> {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        if let Some(value) = &self.0 {
            value.fmt(formatter)
        } else {
            Ok(())
        }
    }
}

/// Displays a timestamp delta in milliseconds when both endpoints are available and ordered.
pub(crate) struct CsvLatency(Option<u64>);

impl CsvLatency {
    /// Builds a latency cell from optional microsecond timestamps.
    pub(crate) fn between(start_timestamp_us: Option<u64>, end_timestamp_us: Option<u64>) -> Self {
        Self(match (start_timestamp_us, end_timestamp_us) {
            (Some(start), Some(end)) => end.checked_sub(start),
            _ => None,
        })
    }
}

impl fmt::Display for CsvLatency {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        if let Some(latency_us) = self.0 {
            write!(formatter, "{:.3}", latency_us as f64 / 1_000.0)
        } else {
            Ok(())
        }
    }
}

/// Displays an optional floating-point CSV cell with millisecond precision.
pub(crate) struct CsvFloat(pub(crate) Option<f64>);

impl fmt::Display for CsvFloat {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        if let Some(value) = self.0 {
            write!(formatter, "{value:.3}")
        } else {
            Ok(())
        }
    }
}

/// Makes a stats-provided string safe for an unquoted CSV cell.
///
/// Codec and decoder names come from WebRTC, not from this crate, so they are
/// sanitized rather than trusted: a comma or newline would shift every later
/// column in the row.
pub(crate) fn csv_text(value: &str) -> String {
    value
        .chars()
        .map(|c| if c == ',' || c == '\n' || c == '\r' || c == '"' { ';' } else { c })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn frame_log_range_is_inclusive() {
        let range = FrameLogRange::new(Some(10), Some(20)).expect("range should be valid");
        assert!(!range.contains(9));
        assert!(range.contains(10));
        assert!(range.contains(20));
        assert!(!range.contains(21));
        assert_eq!(range.previous_to_start(), Some(9));
        assert!(range.reaches_end(20));
        assert!(!range.reaches_end(19));
    }

    #[test]
    fn reaches_end_fires_on_frames_past_the_bound_not_only_on_the_bound() {
        // The arm-2b hang: at 1.13 fps the encoder dropped frame 3600, so an
        // equality test never fired and the publisher never shut down. Any
        // frame at or past the bound must end the window.
        let range = FrameLogRange::new(Some(0), Some(3600)).expect("range should be valid");
        assert!(range.reaches_end(3600));
        assert!(range.reaches_end(3601), "the end frame is often dropped; the next one must end it");
        assert!(range.reaches_end(9999));
        assert!(!range.reaches_end(3599));
    }

    #[test]
    fn an_open_ended_range_never_reaches_an_end() {
        // No --log-end-frame-id means run until interrupted. `>=` must not turn
        // an absent bound into an immediate shutdown.
        let range = FrameLogRange::new(Some(0), None).expect("range should be valid");
        assert!(!range.reaches_end(0));
        assert!(!range.reaches_end(u32::MAX));
    }

    #[test]
    fn frame_log_range_rejects_reversed_bounds() {
        assert!(FrameLogRange::new(Some(20), Some(10)).is_err());
    }
}
