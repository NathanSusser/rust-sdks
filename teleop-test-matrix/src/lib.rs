//! Measurement harness for the LiveKit Rust teleoperation test matrix.
//!
//! One process runs one cell of the matrix: it joins a room as both a publisher and a
//! subscriber, publishes a synthetic video track and a fixed-rate control stream, samples
//! `RtcStats` on a fixed cadence, and appends one JSON snapshot per interval. A real
//! camera — a local capture device, or an IP camera over RTSP — can be substituted for the
//! synthetic pattern with `--camera-source`, as an opt-in realism spot-check rather than a
//! matrix default.
//!
//! The binary emits raw and lightly normalized fields only. There are no thresholds, no
//! derived percentages and no verdict logic here: scoring lives in the Python analysis
//! layer so that changing a threshold never requires a rebuild, and so that a defect in
//! the differencing is fixable without re-running the matrix.

pub mod audio;
pub mod camera;
pub mod cli;
pub mod clock;
pub mod control;
pub mod counters;
pub mod encoder;
pub mod export;
pub mod keyframe;
pub mod probe;
pub mod rtsp;
pub mod run;
pub mod sampler;
pub mod session;
pub mod snapshot;
pub mod stats;
pub mod video;
pub mod writer;
pub mod y4m;
