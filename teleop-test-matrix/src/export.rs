//! Writing the harness's video sources to a file, for offline codec analysis.
//!
//! `webrtc-vmaf` (<https://github.com/livekit/webrtc-vmaf>) answers a question the harness
//! cannot: at a *fixed* bitrate, which codec produces the better picture. It does that
//! offline, on a file. That comparison is only meaningful if the file holds the same
//! content the harness transports — otherwise the VMAF table describes some other video and
//! is silently unrelated to every number in the matrix.
//!
//! So the pattern is exported by driving [`SyntheticFrameSource`] itself rather than by
//! reimplementing it. A second generator would drift from the first the moment either
//! changed, and nothing in either output would show it: both would be plausible moving test
//! patterns.
//!
//! This module is deliberately independent of the LiveKit room: no token, no network, no
//! SFU. Exporting is a local file operation and must stay runnable on a host that cannot
//! reach a LiveKit deployment.

use std::fs::File;
use std::io::{BufWriter, Write};
use std::path::{Path, PathBuf};
use std::time::Duration;

use crate::camera::{CameraFrameSource, CameraSelector};
use crate::rtsp::{RtspFrameSource, RtspOptions, RtspSelector, RtspTransport};
use crate::video::{FrameSource, SyntheticFrameSource};
use crate::y4m::{frame_len, Y4mParams, Y4mWriter};

/// Buffer size for the output file.
///
/// One 1080p frame is ~3 MB, so a small buffer would issue a syscall per plane row. 4 MiB
/// keeps whole frames in one write without meaningfully growing resident memory.
const WRITE_BUFFER_BYTES: usize = 4 * 1024 * 1024;

/// Failure to produce an export.
///
/// Scoped to this operation rather than reusing the run harness's error type: an export
/// cannot fail to join a room, publish a track or lose a session, and offering those
/// variants here would suggest otherwise.
#[derive(Debug)]
pub enum ExportError {
    /// The output file could not be created or written.
    Io { path: PathBuf, source: std::io::Error },
    /// A local capture device could not be opened or read.
    Camera(crate::camera::CameraError),
    /// An RTSP stream could not be opened or read.
    Rtsp(crate::rtsp::RtspError),
    /// The source stopped before the requested number of frames was reached.
    ///
    /// Distinct from a clean success because a short export still produces a playable file:
    /// without this, a camera that dropped out after two seconds would yield a file that
    /// scores a VMAF perfectly happily, against far less content than was asked for.
    Short { got: u64, requested: u64, source: String },
}

impl std::fmt::Display for ExportError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Io { path, source } => write!(f, "cannot write {}: {source}", path.display()),
            Self::Camera(e) => write!(f, "camera export failed: {e}"),
            Self::Rtsp(e) => write!(f, "rtsp export failed: {e}"),
            Self::Short { got, requested, source } => write!(
                f,
                "source stopped after {got} of {requested} frames ({source}); the partial file \
                 is left in place for inspection but must not be used for a VMAF comparison, \
                 which requires every codec to see the same content"
            ),
        }
    }
}

impl std::error::Error for ExportError {}

/// Which source an export should draw frames from.
///
/// Mirrors the harness's [`FrameSource`] selection so the exported file and a live run
/// resolve the same `--camera-source` value to the same pixels.
#[derive(Debug, Clone)]
pub enum ExportSource {
    /// The deterministic moving pattern the matrix publishes.
    Synthetic,
    /// A local capture device.
    Device(CameraSelector),
    /// An IP camera over RTSP.
    Rtsp(RtspSelector, RtspTransport, Duration),
}

/// Everything an export needs, grouped to keep the entry point's signature readable.
#[derive(Debug, Clone)]
pub struct ExportRequest {
    /// Where frames come from.
    pub source: ExportSource,
    /// Requested frame width. Rounded up to even by the source, as in a live run.
    pub width: u32,
    /// Requested frame height.
    pub height: u32,
    /// Frame rate written into the Y4M header and requested of a camera.
    pub fps: u32,
    /// How many frames to write.
    pub frames: u64,
    /// Destination path.
    pub output: PathBuf,
}

/// What an export produced, for the operator and for the wrapper that reads it back.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ExportSummary {
    /// Geometry actually written, after even-rounding and camera negotiation.
    pub width: u32,
    /// Height actually written.
    pub height: u32,
    /// Frame rate declared in the header.
    pub fps: u32,
    /// Frames written.
    pub frames: u64,
    /// Bytes written, header included.
    pub bytes: u64,
    /// The source label, matching the harness's `camera_source` run-record field, so an
    /// export and a run are traceable to the same source.
    pub source_label: String,
}

impl ExportSummary {
    /// Duration of the exported clip in seconds.
    ///
    /// `webrtc-vmaf` seeks to 5 s to capture a preview still and raises if the clip is
    /// shorter, so this is worth reporting rather than leaving to be discovered there.
    pub fn duration_s(&self) -> f64 {
        self.frames as f64 / self.fps.max(1) as f64
    }
}

/// How many frames a duration at a given rate implies, with a floor of one.
///
/// ```
/// # use teleop_test_matrix::export::frames_for_duration;
/// assert_eq!(frames_for_duration(10.0, 30), 300);
/// // Rounded up: a partial frame at the end is still a frame that has to be written.
/// assert_eq!(frames_for_duration(1.5, 30), 45);
/// assert_eq!(frames_for_duration(0.0, 30), 1);
/// ```
pub fn frames_for_duration(duration_s: f64, fps: u32) -> u64 {
    let frames = (duration_s.max(0.0) * fps.max(1) as f64).ceil() as u64;
    frames.max(1)
}

/// Bytes a Y4M export of this shape will occupy.
///
/// ```
/// # use teleop_test_matrix::export::estimated_bytes;
/// # use teleop_test_matrix::y4m::{frame_len, Y4mParams};
/// // A 10 s 1080p30 export is ~933 MB; worth knowing before generating it.
/// let bytes = estimated_bytes(1920, 1080, 30, 1, 300);
/// let header = Y4mParams { width: 1920, height: 1080, fps_num: 30, fps_den: 1 }.header();
/// assert_eq!(bytes, header.len() as u64 + 300 * frame_len(1920, 1080) as u64);
/// assert_eq!(bytes / 1_000_000, 933);
/// ```
pub fn estimated_bytes(width: u32, height: u32, fps_num: u32, fps_den: u32, frames: u64) -> u64 {
    let params = Y4mParams { width, height, fps_num, fps_den };
    params.header().len() as u64 + frames * frame_len(width, height) as u64
}

/// Opens the requested source and writes `frames` frames of it to `output` as Y4M.
///
/// The source is resolved through the same [`FrameSource`] the capture loop uses, so a
/// `--camera-source` value means here exactly what it means in a run.
pub fn export(request: &ExportRequest) -> Result<ExportSummary, ExportError> {
    let source = open_source(request)?;
    let (width, height) = (source.width(), source.height());
    let source_label = source.source_label();

    let file = File::create(&request.output)
        .map_err(|e| ExportError::Io { path: request.output.clone(), source: e })?;
    let writer = BufWriter::with_capacity(WRITE_BUFFER_BYTES, file);
    let params = Y4mParams { width, height, fps_num: request.fps.max(1), fps_den: 1 };

    let summary = write_frames(source, writer, params, request.frames, source_label)
        .map_err(|e| map_write_error(e, &request.output))?;

    log::info!(
        "exported {} frames of {} at {}x{}@{} ({:.1} s, {:.1} MB) to {}",
        summary.frames,
        summary.source_label,
        summary.width,
        summary.height,
        summary.fps,
        summary.duration_s(),
        summary.bytes as f64 / 1e6,
        request.output.display()
    );
    Ok(summary)
}

/// Resolves an [`ExportSource`] into the harness's own [`FrameSource`].
fn open_source(request: &ExportRequest) -> Result<FrameSource, ExportError> {
    match &request.source {
        ExportSource::Synthetic => {
            Ok(FrameSource::Synthetic(SyntheticFrameSource::new(request.width, request.height)))
        }
        ExportSource::Device(selector) => {
            let camera =
                CameraFrameSource::open(selector, request.width, request.height, request.fps)
                    .map_err(ExportError::Camera)?;
            Ok(FrameSource::Camera(Box::new(camera)))
        }
        ExportSource::Rtsp(selector, transport, stall_timeout) => {
            let options = RtspOptions {
                width: request.width,
                height: request.height,
                fps: request.fps,
                transport: *transport,
                stall_timeout: *stall_timeout,
            };
            let stream = RtspFrameSource::open(selector, &options).map_err(ExportError::Rtsp)?;
            Ok(FrameSource::Rtsp(Box::new(stream)))
        }
    }
}

/// Errors [`write_frames`] can raise before they are given a path.
enum WriteError {
    Io(std::io::Error),
    Short { got: u64, requested: u64, source: String },
}

fn map_write_error(error: WriteError, path: &Path) -> ExportError {
    match error {
        WriteError::Io(source) => ExportError::Io { path: path.to_path_buf(), source },
        WriteError::Short { got, requested, source } => {
            ExportError::Short { got, requested, source }
        }
    }
}

/// Pulls frames from the source and writes them, stopping at `frames` or at exhaustion.
///
/// Split from [`export`] so the loop is exercisable against any [`Write`] without touching
/// the filesystem, and so the short-source case has a test that does not need a camera to
/// fail halfway.
fn write_frames<W: Write>(
    mut source: FrameSource,
    sink: W,
    params: Y4mParams,
    frames: u64,
    source_label: String,
) -> Result<ExportSummary, WriteError> {
    let mut writer = Y4mWriter::new(sink, params).map_err(WriteError::Io)?;

    for _ in 0..frames {
        // `FrameSource` logs the underlying capture error and yields `None`; the export is
        // reported short rather than being padded with a repeated or blank frame, which
        // would change the encoding problem without changing the frame count.
        let Some(buffer) = source.next_buffer() else {
            let got = writer.frames_written();
            return Err(WriteError::Short {
                got,
                requested: frames,
                source: source.source_label(),
            });
        };
        writer.write_frame(&buffer).map_err(WriteError::Io)?;
    }

    let written = writer.frames_written();
    writer.finish().map_err(WriteError::Io)?;

    Ok(ExportSummary {
        width: params.width,
        height: params.height,
        fps: params.fps_num,
        frames: written,
        bytes: estimated_bytes(
            params.width,
            params.height,
            params.fps_num,
            params.fps_den,
            written,
        ),
        source_label,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_duration_converts_to_a_whole_number_of_frames() {
        assert_eq!(frames_for_duration(10.0, 30), 300);
        assert_eq!(frames_for_duration(10.0, 15), 150);
        // Never zero: an export of no frames is a file ffprobe reports as having no video.
        assert_eq!(frames_for_duration(0.0, 30), 1);
        assert_eq!(frames_for_duration(-5.0, 30), 1);
    }

    #[test]
    fn the_size_estimate_matches_what_is_written() {
        let (w, h, frames) = (32u32, 16u32, 7u64);
        let params = Y4mParams { width: w, height: h, fps_num: 30, fps_den: 1 };
        let summary = write_frames(
            FrameSource::Synthetic(SyntheticFrameSource::new(w, h)),
            Vec::new(),
            params,
            frames,
            "test_pattern".to_string(),
        )
        .unwrap_or_else(|_| panic!("synthetic export must not fail"));

        assert_eq!(summary.frames, frames);
        assert_eq!(summary.bytes, estimated_bytes(w, h, 30, 1, frames));
        assert_eq!((summary.width, summary.height, summary.fps), (w, h, 30));
        assert_eq!(summary.source_label, "test_pattern");
        assert!((summary.duration_s() - 7.0 / 30.0).abs() < 1e-9);
    }

    /// The property the entire cross-codec comparison rests on: every codec must be handed
    /// byte-identical content. If two exports at the same parameters differed, a VMAF gap
    /// between two codecs could be a difference in the source rather than in the encoder.
    #[test]
    fn two_exports_at_the_same_parameters_are_byte_identical() {
        let params = Y4mParams { width: 64, height: 48, fps_num: 30, fps_den: 1 };
        let render = || {
            let mut bytes = Vec::new();
            let mut writer = Y4mWriter::new(&mut bytes, params).expect("header");
            let mut source = SyntheticFrameSource::new(64, 48);
            for _ in 0..12 {
                writer.write_frame(&source.next_buffer()).expect("frame");
            }
            writer.finish().expect("finish");
            bytes
        };
        assert_eq!(render(), render());
    }

    /// And the converse: the export must not be a static image the encoder can collapse,
    /// or every codec scores ~100 and the comparison says nothing.
    #[test]
    fn successive_exported_frames_differ() {
        let params = Y4mParams { width: 64, height: 48, fps_num: 30, fps_den: 1 };
        let mut bytes = Vec::new();
        let mut writer = Y4mWriter::new(&mut bytes, params).expect("header");
        let mut source = SyntheticFrameSource::new(64, 48);
        for _ in 0..2 {
            writer.write_frame(&source.next_buffer()).expect("frame");
        }
        writer.finish().expect("finish");

        let header = params.header().len();
        let stride = frame_len(64, 48);
        assert_ne!(&bytes[header..header + stride], &bytes[header + stride..header + 2 * stride]);
    }

    /// Odd geometry must round the same way it does in a live run, or the exported file is
    /// not the picture the harness would have sent.
    #[test]
    fn export_geometry_rounds_like_the_harness() {
        let summary = write_frames(
            FrameSource::Synthetic(SyntheticFrameSource::new(63, 47)),
            Vec::new(),
            Y4mParams { width: 64, height: 48, fps_num: 30, fps_den: 1 },
            2,
            "test_pattern".to_string(),
        )
        .unwrap_or_else(|_| panic!("export"));
        assert_eq!((summary.width, summary.height), (64, 48));
    }

    /// A partial export must name itself. The file it leaves behind is playable and would
    /// otherwise be compared against a full-length one.
    #[test]
    fn a_short_export_is_an_error_not_a_shorter_file() {
        let err = ExportError::Short {
            got: 42,
            requested: 300,
            source: "rtsp:rtsp://***@10.0.0.5/s".to_string(),
        };
        let message = err.to_string();
        assert!(message.contains("42 of 300"), "{message}");
        assert!(message.contains("must not be used"), "{message}");
    }

    /// An I/O failure has to name the path; an export is typically run with a redirect and
    /// a bare "no space left" says nothing about which of several outputs failed.
    #[test]
    fn an_io_error_names_the_output_path() {
        let err = ExportError::Io {
            path: PathBuf::from("/vmaf/sources/pattern.y4m"),
            source: std::io::Error::other("no space left on device"),
        };
        assert!(err.to_string().contains("/vmaf/sources/pattern.y4m"), "{err}");
    }
}
