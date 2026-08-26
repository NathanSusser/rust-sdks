//! RTSP / IP-camera ingest, as a third opt-in alternative to the synthetic pattern.
//!
//! Same contract as [`crate::camera`]: a realism spot-check for the Tier 2 rig, never a
//! matrix default and never a swept axis, and with **no fallback to synthetic** — a run
//! labelled with a camera source that actually carried the pattern would be pooled with
//! pattern runs and nothing in the record could catch it.
//!
//! Decoding is delegated to an `ffmpeg` subprocess rather than to an in-process RTSP or
//! H.264 crate. Real IP cameras are full of quirks — interleaved TCP framing, missing SPS
//! before the first IDR, vendor timestamps that run backwards — and ffmpeg has absorbed a
//! decade of them. It also keeps a decoder out of the harness's build graph, which matters
//! because every host that runs the matrix has to build this crate.
//!
//! The failure model is the point of most of this module. The first person to run it will
//! be pointing it at a camera on a subnet nobody here can reach, so every way it can fail
//! has to name itself:
//!
//! - a read that produces no bytes for [`RtspFrameSource::open`]'s stall timeout is a
//!   distinct [`RtspError::Stall`], not a generic read error, because ffmpeg holds the
//!   pipe open and simply stops emitting when an RTSP session wedges;
//! - a short read at a frame boundary is [`RtspError::StreamEnded`] and a short read
//!   part-way through a frame is [`RtspError::TruncatedFrame`], and neither is ever
//!   published as a valid frame;
//! - ffmpeg's stderr — the only place an auth failure, an unreachable host or a wrong
//!   stream path is ever explained — is drained on a background thread and replayed into
//!   every error this module produces.

use std::io::{BufRead, BufReader, Read};
use std::process::{Child, ChildStdout, Command, Stdio};
use std::sync::mpsc::{self, Receiver, RecvTimeoutError};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use livekit::webrtc::video_frame::I420Buffer;

/// URL schemes that route `--camera-source` to this module rather than to a local device.
const RTSP_SCHEMES: [&str; 2] = ["rtsp://", "rtsps://"];

/// How many trailing stderr lines from ffmpeg are kept to attach to an error.
///
/// ffmpeg's explanation of an RTSP failure is typically its last handful of lines; the
/// bound stops a camera that logs a warning per frame from growing this without limit.
const STDERR_TAIL_LINES: usize = 20;

/// Default deadline for a single frame read before the stream is declared stalled.
///
/// Long enough to cover a camera reconnecting its RTSP session or emitting a slow keyframe,
/// short enough that a wedged run fails inside a cell rather than hanging the sweep. The
/// value is a measurement parameter and lives in `matrix.yaml` under `meta.parameters`;
/// this constant is the harness-side default for a hand invocation.
pub const DEFAULT_STALL_TIMEOUT_S: u64 = 15;

/// Whether ffmpeg negotiates the RTSP media transport over TCP or UDP.
///
/// TCP is the default deliberately. UDP RTSP degrades by silently dropping media packets
/// on a filtered or congested path, which arrives at the harness as a camera producing
/// corrupt or missing frames — indistinguishable in the record from a genuinely bad camera.
/// TCP turns the same condition into a connection error that names itself.
#[derive(Copy, Clone, Debug, PartialEq, Eq, Default, clap::ValueEnum)]
pub enum RtspTransport {
    /// Interleaved over the RTSP control connection. The default.
    #[default]
    #[value(name = "tcp")]
    Tcp,
    /// Separate UDP media streams.
    #[value(name = "udp")]
    Udp,
}

impl RtspTransport {
    /// The value as it appears on the command line and in the run record.
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Tcp => "tcp",
            Self::Udp => "udp",
        }
    }
}

/// Failure to start or read an RTSP stream.
///
/// Every variant is fatal by design: the caller must not substitute another source. The
/// variants are split finely because they are the only diagnosis available to whoever runs
/// this against real hardware — a stall, a truncated frame and a clean end of stream have
/// entirely different causes and must not collapse into one "read failed".
#[derive(Debug)]
pub enum RtspError {
    /// `ffmpeg` could not be executed at all — typically not installed or not on `PATH`.
    FfmpegUnavailable { program: String, source: String },
    /// The child process started but a pipe could not be taken from it.
    Pipe(String),
    /// `ffmpeg` exited instead of producing frames, e.g. auth failure or unreachable host.
    Exited { status: String, stderr: String },
    /// No bytes arrived within the stall timeout while the process was still running.
    Stall { timeout: Duration, bytes_into_frame: usize, stderr: String },
    /// The pipe closed cleanly on a frame boundary: the stream ended.
    StreamEnded { frames_read: u64, stderr: String },
    /// The pipe closed part-way through a frame, so the last frame is incomplete.
    TruncatedFrame { got: usize, expected: usize, stderr: String },
    /// Reading from the pipe failed.
    Read { source: String, stderr: String },
}

impl std::fmt::Display for RtspError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::FfmpegUnavailable { program, source } => write!(
                f,
                "cannot execute {program:?}: {source}; an rtsp:// --camera-source requires \
                 ffmpeg on PATH and will not fall back to the synthetic pattern"
            ),
            Self::Pipe(e) => write!(f, "cannot attach to the ffmpeg pipes: {e}"),
            Self::Exited { status, stderr } => write!(
                f,
                "ffmpeg exited before producing a frame ({status}); its output was:\n{stderr}"
            ),
            Self::Stall { timeout, bytes_into_frame, stderr } => write!(
                f,
                "rtsp stream stalled: no data for {timeout:?} ({bytes_into_frame} bytes into \
                 the current frame) while ffmpeg was still running; its last output was:\n{stderr}"
            ),
            Self::StreamEnded { frames_read, stderr } => write!(
                f,
                "rtsp stream ended after {frames_read} frames; ffmpeg's last output was:\n{stderr}"
            ),
            Self::TruncatedFrame { got, expected, stderr } => write!(
                f,
                "rtsp stream ended mid-frame: {got} of {expected} bytes; the partial frame is \
                 discarded rather than published. ffmpeg's last output was:\n{stderr}"
            ),
            Self::Read { source, stderr } => {
                write!(f, "cannot read from ffmpeg: {source}; its last output was:\n{stderr}")
            }
        }
    }
}

impl std::error::Error for RtspError {}

/// How a run was told to reach an IP camera.
///
/// Parsed from a `--camera-source` value beginning with `rtsp://` or `rtsps://`; see
/// [`is_rtsp_url`].
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RtspSelector {
    url: String,
}

impl RtspSelector {
    /// Wraps a `--camera-source` value already known to be an RTSP URL.
    pub fn new(url: &str) -> Self {
        Self { url: url.to_string() }
    }

    /// The URL as given, credentials included. Never put this in the run record.
    pub fn url(&self) -> &str {
        &self.url
    }

    /// The URL with any `user:password@` userinfo replaced, for logs and the run record.
    ///
    /// RTSP URLs routinely embed credentials and the run record is committed and shared, so
    /// the redaction happens here rather than at each use site.
    pub fn redacted_url(&self) -> String {
        redact_url(&self.url)
    }
}

/// Whether a `--camera-source` value names an RTSP stream rather than a local device.
///
/// Case-insensitive on the scheme only: an operator typing `RTSP://` means the same stream,
/// while the rest of the URL is left exactly as given because RTSP paths are case-sensitive
/// on many cameras.
pub fn is_rtsp_url(value: &str) -> bool {
    let lowered = value.to_ascii_lowercase();
    RTSP_SCHEMES.iter().any(|scheme| lowered.starts_with(scheme))
}

/// Replaces the userinfo component of a URL with `***`.
///
/// Operates on the authority section only — the span between `://` and the first following
/// `/`, `?` or `#` — so an `@` appearing later in a stream path is not mistaken for a
/// credential delimiter and does not truncate the recorded URL. A value that is not a URL
/// is returned unchanged, so this is safe to apply to any `--camera-source`.
///
/// ```
/// # use teleop_test_matrix::rtsp::redact_url;
/// assert_eq!(redact_url("rtsp://admin:pw@10.0.0.5/s"), "rtsp://***@10.0.0.5/s");
/// assert_eq!(redact_url("FaceTime HD Camera"), "FaceTime HD Camera");
/// ```
pub fn redact_url(url: &str) -> String {
    let Some(scheme_end) = url.find("://") else {
        return url.to_string();
    };
    let authority_start = scheme_end + 3;
    let authority_end = url[authority_start..]
        .find(['/', '?', '#'])
        .map_or(url.len(), |offset| authority_start + offset);
    let authority = &url[authority_start..authority_end];

    let Some(at) = authority.rfind('@') else {
        return url.to_string();
    };
    format!("{}***@{}{}", &url[..authority_start], &authority[at + 1..], &url[authority_end..])
}

/// The stream a run actually opened, and the geometry it was decoded to.
///
/// Mirrors [`crate::camera::CameraIdentity`] so both camera kinds land in the same run
/// record fields. The recorded URL is redacted; the negotiated geometry is what ffmpeg was
/// told to scale to, which is what the encoder actually sees.
#[derive(Debug, Clone)]
pub struct RtspIdentity {
    /// The `--camera-source` value with credentials stripped.
    pub requested: String,
    /// The stream URL with credentials stripped. Same as `requested` today, kept separate
    /// because `requested` is the CLI contract and this is the resource.
    pub url: String,
    /// RTSP media transport ffmpeg was told to use.
    pub transport: RtspTransport,
    /// Decoded width handed to the encoder.
    pub negotiated_width: u32,
    /// Decoded height handed to the encoder.
    pub negotiated_height: u32,
    /// Output frame rate ffmpeg was told to produce.
    ///
    /// A camera slower than this (the Tier 2 Muscat runs ~10 fps at 1080p) makes ffmpeg
    /// duplicate frames to reach it; a faster one makes it drop them. Either way the record
    /// carries the rate the encoder was fed, not the rate the sensor ran at.
    pub negotiated_fps: u32,
    /// Pixel format on the pipe. Always `yuv420p`: it is what ffmpeg is asked for and what
    /// the frame-size arithmetic below assumes.
    pub negotiated_format: String,
}

/// Bounded ring of ffmpeg's most recent stderr lines.
///
/// Shared with the draining thread. Every error this module produces attaches a snapshot of
/// it, because ffmpeg's stderr is the only explanation of an RTSP failure that exists.
#[derive(Debug, Default)]
struct StderrTail {
    lines: Mutex<std::collections::VecDeque<String>>,
}

impl StderrTail {
    fn push(&self, line: String) {
        let mut lines = self.lines.lock().expect("stderr tail mutex poisoned");
        if lines.len() == STDERR_TAIL_LINES {
            lines.pop_front();
        }
        lines.push_back(line);
    }

    /// The retained lines, newest last, as one block of text.
    fn snapshot(&self) -> String {
        let lines = self.lines.lock().expect("stderr tail mutex poisoned");
        if lines.is_empty() {
            return "(ffmpeg produced no diagnostics)".to_string();
        }
        lines.iter().cloned().collect::<Vec<_>>().join("\n")
    }
}

/// Bytes one I420 frame of the given geometry occupies on the pipe.
///
/// Luma is `w * h`; the two chroma planes are each subsampled by two in both directions, so
/// together they add `w * h / 2`. Frames arrive back to back with no delimiter, so this
/// number is the only frame boundary that exists — getting it wrong shears every frame.
fn i420_frame_len(width: u32, height: u32) -> usize {
    let (w, h) = (width as usize, height as usize);
    w * h + 2 * (w.div_ceil(2) * h.div_ceil(2))
}

/// A running `ffmpeg` decoding an RTSP stream to raw I420 on its stdout.
///
/// Held by the capture loop, which drives it at the run's frame cadence exactly as it drives
/// the synthetic and local-device sources.
pub struct RtspFrameSource {
    child: Child,
    frames: Receiver<Result<Vec<u8>, RtspError>>,
    stderr: Arc<StderrTail>,
    identity: RtspIdentity,
    width: u32,
    height: u32,
    stall_timeout: Duration,
    frames_read: u64,
}

/// Everything [`RtspFrameSource::open`] needs, grouped to keep the signature readable.
#[derive(Debug, Clone)]
pub struct RtspOptions {
    /// Requested output width. Decoded frames are scaled to it.
    pub width: u32,
    /// Requested output height.
    pub height: u32,
    /// Requested output frame rate.
    pub fps: u32,
    /// RTSP media transport.
    pub transport: RtspTransport,
    /// Deadline for a single frame read before the stream counts as stalled.
    pub stall_timeout: Duration,
}

impl RtspFrameSource {
    /// Spawns `ffmpeg` against the stream and starts reading frames.
    ///
    /// Returns as soon as the process starts; a stream that is unreachable or rejects
    /// authentication surfaces on the first [`next_buffer`](Self::next_buffer) as an
    /// [`RtspError::Exited`] carrying ffmpeg's own explanation. Audio is discarded with
    /// `-an`: the harness publishes its own synthetic tone and the camera's AAC track would
    /// only add a stream ffmpeg has to demux.
    pub fn open(selector: &RtspSelector, options: &RtspOptions) -> Result<Self, RtspError> {
        Self::open_input(
            selector,
            options,
            &["-rtsp_transport".to_string(), options.transport.as_str().to_string()],
            selector.url(),
        )
    }

    /// Spawns ffmpeg against an arbitrary input, which [`open`](Self::open) specialises to
    /// an RTSP URL.
    ///
    /// Split out so the subprocess plumbing — the pipe framing, the stall deadline, the
    /// stderr drain and the exit reporting — is exercisable against a locally generated
    /// input. That machinery is the part most likely to be wrong and the part that cannot
    /// otherwise be tested without a reachable IP camera.
    fn open_input(
        selector: &RtspSelector,
        options: &RtspOptions,
        pre_input_args: &[String],
        input: &str,
    ) -> Result<Self, RtspError> {
        let width = round_up_even(options.width);
        let height = round_up_even(options.height);
        let fps = options.fps.max(1);

        let mut command = Command::new("ffmpeg");
        command
            // Without this ffmpeg competes with the harness for the terminal's stdin and
            // can be stopped by the shell when the run is backgrounded.
            .arg("-nostdin")
            .args(["-loglevel", "error"])
            .args(pre_input_args)
            .args(["-i", input])
            .arg("-an")
            .args(["-f", "rawvideo"])
            .args(["-pix_fmt", "yuv420p"])
            .args(["-s", &format!("{width}x{height}")])
            .args(["-r", &fps.to_string()])
            .arg("pipe:1")
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());

        log::info!(
            "starting ffmpeg for rtsp source {} ({width}x{height}@{fps}, {} transport, {}s stall \
             timeout)",
            selector.redacted_url(),
            options.transport.as_str(),
            options.stall_timeout.as_secs()
        );

        let mut child = command.spawn().map_err(|e| RtspError::FfmpegUnavailable {
            program: "ffmpeg".to_string(),
            source: e.to_string(),
        })?;

        let stdout = child
            .stdout
            .take()
            .ok_or_else(|| RtspError::Pipe("ffmpeg stdout was not captured".to_string()))?;
        let stderr = child
            .stderr
            .take()
            .ok_or_else(|| RtspError::Pipe("ffmpeg stderr was not captured".to_string()))?;

        let tail = Arc::new(StderrTail::default());
        spawn_stderr_drain(stderr, Arc::clone(&tail));

        let frame_len = i420_frame_len(width, height);
        let frames = spawn_frame_reader(stdout, frame_len, Arc::clone(&tail));

        let redacted = selector.redacted_url();
        Ok(Self {
            child,
            frames,
            stderr: tail,
            identity: RtspIdentity {
                requested: redacted.clone(),
                url: redacted,
                transport: options.transport,
                negotiated_width: width,
                negotiated_height: height,
                negotiated_fps: fps,
                negotiated_format: "yuv420p".to_string(),
            },
            width,
            height,
            stall_timeout: options.stall_timeout,
            frames_read: 0,
        })
    }

    /// Width of the frames this source produces.
    pub fn width(&self) -> u32 {
        self.width
    }

    /// Height of the frames this source produces.
    pub fn height(&self) -> u32 {
        self.height
    }

    /// The stream and its decoded geometry, for the run record.
    pub fn identity(&self) -> &RtspIdentity {
        &self.identity
    }

    /// Reads one complete frame from the pipe and wraps it as an I420 buffer.
    ///
    /// Bounded by the stall timeout: a wedged RTSP session leaves ffmpeg alive with the pipe
    /// open and no bytes flowing, which without a deadline would block the capture loop for
    /// the rest of the run with nothing in the log to say so.
    ///
    /// The camera's own RTP timestamps are deliberately not used, for the same reason
    /// [`crate::camera::CameraFrameSource::next_buffer`] ignores the device's: the capture
    /// loop stamps every source at the same point in the loop, so all three sources' G2G
    /// figures measure the same interval.
    pub fn next_buffer(&mut self) -> Result<I420Buffer, RtspError> {
        let frame = match self.frames.recv_timeout(self.stall_timeout) {
            Ok(result) => result?,
            Err(RecvTimeoutError::Timeout) => {
                // A process that has already exited is not stalled — it failed, and its
                // exit status plus stderr say why. Reporting that as a stall would send the
                // person debugging this after a network problem that is not there.
                if let Ok(Some(status)) = self.child.try_wait() {
                    return Err(RtspError::Exited {
                        status: status.to_string(),
                        stderr: self.stderr.snapshot(),
                    });
                }
                return Err(RtspError::Stall {
                    timeout: self.stall_timeout,
                    bytes_into_frame: 0,
                    stderr: self.stderr.snapshot(),
                });
            }
            // The reader thread only drops its sender after sending a terminal result, so
            // an empty channel here means the thread died without reporting — treat it as
            // the process having gone, which is the only way that happens.
            Err(RecvTimeoutError::Disconnected) => {
                let status = match self.child.try_wait() {
                    Ok(Some(status)) => status.to_string(),
                    _ => "still running".to_string(),
                };
                return Err(RtspError::Exited { status, stderr: self.stderr.snapshot() });
            }
        };

        self.frames_read += 1;
        Ok(copy_into_i420(&frame, self.width, self.height))
    }
}

impl Drop for RtspFrameSource {
    /// Kills ffmpeg when the run ends.
    ///
    /// Without this the child outlives the harness holding an RTSP session open, and the
    /// next run against the same camera can be refused because the camera's session limit
    /// is already reached — a failure that would look like a broken camera.
    fn drop(&mut self) {
        if let Err(e) = self.child.kill() {
            log::warn!("could not stop ffmpeg: {e}");
        }
        if let Err(e) = self.child.wait() {
            log::warn!("could not reap ffmpeg: {e}");
        }
    }
}

/// Drains ffmpeg's stderr into the shared tail.
///
/// On its own thread because the pipe has a fixed kernel buffer: an undrained stderr blocks
/// ffmpeg's writes and the video stops for a reason that appears nowhere.
fn spawn_stderr_drain(stderr: std::process::ChildStderr, tail: Arc<StderrTail>) {
    std::thread::spawn(move || {
        for line in BufReader::new(stderr).lines() {
            let Ok(line) = line else {
                break;
            };
            log::warn!("ffmpeg: {line}");
            tail.push(line);
        }
    });
}

/// Reads fixed-size frames off ffmpeg's stdout on a background thread.
///
/// Blocking reads happen here rather than on the caller's thread so the caller can bound
/// them with a channel timeout; a blocking read cannot itself be given a deadline
/// portably. The thread sends exactly one terminal error and then stops.
fn spawn_frame_reader(
    stdout: ChildStdout,
    frame_len: usize,
    tail: Arc<StderrTail>,
) -> Receiver<Result<Vec<u8>, RtspError>> {
    // Depth one: the capture loop consumes at the run's frame cadence, and a deeper queue
    // would hand the encoder frames that are already stale by however deep the backlog is.
    let (tx, rx) = mpsc::sync_channel(1);
    std::thread::spawn(move || {
        let mut reader = BufReader::new(stdout);
        let mut frames_read: u64 = 0;
        loop {
            let mut frame = vec![0u8; frame_len];
            let terminal = match read_frame(&mut reader, &mut frame) {
                Ok(FrameRead::Complete) => {
                    frames_read += 1;
                    if tx.send(Ok(frame)).is_err() {
                        return;
                    }
                    continue;
                }
                Ok(FrameRead::EndOfStream) => {
                    RtspError::StreamEnded { frames_read, stderr: tail.snapshot() }
                }
                Ok(FrameRead::Truncated { got }) => {
                    RtspError::TruncatedFrame { got, expected: frame_len, stderr: tail.snapshot() }
                }
                Err(e) => RtspError::Read { source: e.to_string(), stderr: tail.snapshot() },
            };
            let _ = tx.send(Err(terminal));
            return;
        }
    });
    rx
}

/// Outcome of trying to fill one frame's worth of bytes from the pipe.
#[derive(Debug, PartialEq, Eq)]
enum FrameRead {
    /// Exactly `frame_len` bytes were read.
    Complete,
    /// The pipe closed on a frame boundary, with nothing read for this frame.
    EndOfStream,
    /// The pipe closed part-way through a frame. The bytes read are not a usable frame.
    Truncated { got: usize },
}

/// Fills `frame` from `reader`, distinguishing a clean end of stream from a torn frame.
///
/// `read` on a pipe returns whatever is available, so a frame routinely arrives across
/// several reads; only a zero-length return means the writer is gone. Which side of a frame
/// boundary that happens on is the difference between "the camera stopped" and "the harness
/// has half a frame", and the two must not be reported as the same thing.
fn read_frame<R: Read>(reader: &mut R, frame: &mut [u8]) -> std::io::Result<FrameRead> {
    let mut filled = 0;
    while filled < frame.len() {
        match reader.read(&mut frame[filled..]) {
            Ok(0) if filled == 0 => return Ok(FrameRead::EndOfStream),
            Ok(0) => return Ok(FrameRead::Truncated { got: filled }),
            Ok(n) => filled += n,
            Err(e) if e.kind() == std::io::ErrorKind::Interrupted => continue,
            Err(e) => return Err(e),
        }
    }
    Ok(FrameRead::Complete)
}

/// Copies a packed I420 frame off the pipe into a stride-aware [`I420Buffer`].
///
/// ffmpeg writes planes packed at exactly the plane width, while `I420Buffer` allocates its
/// own strides, so the copy is row by row rather than one block move. `frame` is assumed to
/// be [`i420_frame_len`] bytes, which is guaranteed by [`read_frame`] returning
/// [`FrameRead::Complete`].
fn copy_into_i420(frame: &[u8], width: u32, height: u32) -> I420Buffer {
    let (w, h) = (width as usize, height as usize);
    let (cw, ch) = (w.div_ceil(2), h.div_ceil(2));

    let mut buffer = I420Buffer::new(width, height);
    let (stride_y, stride_u, stride_v) = buffer.strides();
    let (data_y, data_u, data_v) = buffer.data_mut();

    let (src_y, rest) = frame.split_at(w * h);
    let (src_u, src_v) = rest.split_at(cw * ch);

    for row in 0..h {
        let dst = row * stride_y as usize;
        data_y[dst..dst + w].copy_from_slice(&src_y[row * w..row * w + w]);
    }
    for row in 0..ch {
        let dst_u = row * stride_u as usize;
        data_u[dst_u..dst_u + cw].copy_from_slice(&src_u[row * cw..row * cw + cw]);
        let dst_v = row * stride_v as usize;
        data_v[dst_v..dst_v + cw].copy_from_slice(&src_v[row * cw..row * cw + cw]);
    }

    buffer
}

/// Rounds a dimension up to the nearest even value, with a floor of two.
///
/// Mirrors the rule in [`crate::video`] and [`crate::camera`] so all three sources produce
/// the same geometry from the same request. Also what makes ffmpeg's `-s` argument valid:
/// yuv420p has no odd-dimension representation and ffmpeg rejects one.
fn round_up_even(value: u32) -> u32 {
    let v = value.max(2);
    if v.is_multiple_of(2) {
        v
    } else {
        v + 1
    }
}

#[cfg(test)]
mod tests {
    use livekit::webrtc::prelude::VideoBuffer;

    use super::*;

    #[test]
    fn rtsp_urls_are_recognised_by_scheme() {
        assert!(is_rtsp_url("rtsp://192.168.100.123/full1080p"));
        assert!(is_rtsp_url("rtsps://cam.local/4k"));
        // A scheme typed in caps is the same stream; the path's case is left alone.
        assert!(is_rtsp_url("RTSP://192.168.100.123/Full1080p"));

        assert!(!is_rtsp_url("test_pattern"));
        assert!(!is_rtsp_url("0"));
        assert!(!is_rtsp_url("FaceTime HD Camera"));
        // A device whose name merely mentions rtsp is not a URL.
        assert!(!is_rtsp_url("my rtsp:// camera"));
        assert!(!is_rtsp_url("http://192.168.100.123/stream"));
    }

    /// RTSP URLs routinely carry `user:pass@`, and the run record is committed and shared.
    #[test]
    fn credentials_are_stripped_from_the_recorded_url() {
        assert_eq!(
            redact_url("rtsp://admin:hunter2@192.168.100.123/full1080p"),
            "rtsp://***@192.168.100.123/full1080p"
        );
        // A username with no password is still a credential.
        assert_eq!(
            redact_url("rtsp://admin@10.0.0.5:554/stream"),
            "rtsp://***@10.0.0.5:554/stream"
        );
        // A password containing an @ must not leave part of itself behind.
        assert_eq!(redact_url("rtsp://admin:p@ss@10.0.0.5/s"), "rtsp://***@10.0.0.5/s");
    }

    /// A URL with no userinfo must round-trip untouched, or the record names a stream that
    /// does not exist.
    #[test]
    fn a_url_without_credentials_is_left_alone() {
        let url = "rtsp://192.168.100.123/full1080p";
        assert_eq!(redact_url(url), url);
        assert_eq!(
            redact_url("rtsp://192.168.100.123:554/4k?profile=1"),
            "rtsp://192.168.100.123:554/4k?profile=1"
        );
        // An @ in the path is not a credential delimiter and must not truncate the URL.
        assert_eq!(redact_url("rtsp://192.168.100.123/live@2"), "rtsp://192.168.100.123/live@2");
        assert_eq!(redact_url("not a url"), "not a url");
    }

    #[test]
    fn the_selector_records_a_redacted_url_and_dials_the_full_one() {
        let selector = RtspSelector::new("rtsp://admin:hunter2@192.168.100.123/full1080p");
        assert_eq!(selector.url(), "rtsp://admin:hunter2@192.168.100.123/full1080p");
        assert_eq!(selector.redacted_url(), "rtsp://***@192.168.100.123/full1080p");
    }

    /// Frames arrive back to back with no delimiter, so this arithmetic is the only frame
    /// boundary there is. An error here shears every frame in the run.
    #[test]
    fn frame_length_is_the_i420_plane_sum() {
        assert_eq!(i420_frame_len(1920, 1080), 1920 * 1080 * 3 / 2);
        assert_eq!(i420_frame_len(3840, 2160), 3840 * 2160 * 3 / 2);
        assert_eq!(i420_frame_len(2, 2), 6);
        assert_eq!(i420_frame_len(640, 480), 640 * 480 + 2 * 320 * 240);
    }

    #[test]
    fn rtsp_geometry_is_rounded_to_even_like_every_other_source() {
        assert_eq!(round_up_even(1919), 1920);
        assert_eq!(round_up_even(1080), 1080);
        assert_eq!(round_up_even(0), 2);
    }

    #[test]
    fn a_full_frame_reads_complete() {
        let frame_len = i420_frame_len(4, 4);
        let mut source = std::io::Cursor::new(vec![7u8; frame_len]);
        let mut frame = vec![0u8; frame_len];
        assert_eq!(read_frame(&mut source, &mut frame).expect("read"), FrameRead::Complete);
        assert!(frame.iter().all(|&b| b == 7));
    }

    /// A frame routinely spans several pipe reads; a short read mid-frame is normal and
    /// must not be mistaken for the stream ending.
    #[test]
    fn a_frame_split_across_reads_still_completes() {
        /// Yields at most three bytes per call, like a pipe under load.
        struct Dribble(std::io::Cursor<Vec<u8>>);
        impl Read for Dribble {
            fn read(&mut self, buf: &mut [u8]) -> std::io::Result<usize> {
                let take = buf.len().min(3);
                self.0.read(&mut buf[..take])
            }
        }

        let frame_len = i420_frame_len(6, 6);
        let mut source = Dribble(std::io::Cursor::new((0..frame_len).map(|i| i as u8).collect()));
        let mut frame = vec![0u8; frame_len];
        assert_eq!(read_frame(&mut source, &mut frame).expect("read"), FrameRead::Complete);
        assert_eq!(frame, (0..frame_len).map(|i| i as u8).collect::<Vec<_>>());
    }

    /// The pipe closing on a frame boundary is the stream ending, not a broken frame.
    #[test]
    fn a_closed_pipe_on_a_frame_boundary_is_end_of_stream() {
        let frame_len = i420_frame_len(4, 4);
        let mut source = std::io::Cursor::new(Vec::new());
        let mut frame = vec![0u8; frame_len];
        assert_eq!(read_frame(&mut source, &mut frame).expect("read"), FrameRead::EndOfStream);
    }

    /// A partial frame must never be published: its lower rows are whatever the buffer held,
    /// and the encoder would happily encode the garbage as real content.
    #[test]
    fn a_closed_pipe_mid_frame_is_a_truncated_frame() {
        let frame_len = i420_frame_len(8, 8);
        let mut source = std::io::Cursor::new(vec![1u8; frame_len - 5]);
        let mut frame = vec![0u8; frame_len];
        assert_eq!(
            read_frame(&mut source, &mut frame).expect("read"),
            FrameRead::Truncated { got: frame_len - 5 }
        );
    }

    /// The packed pipe layout and `I420Buffer`'s stride-padded layout are different; a
    /// block copy would diagonally shear the image.
    #[test]
    fn a_packed_frame_copies_into_the_strided_buffer_row_by_row() {
        let (w, h) = (8u32, 8u32);
        let frame: Vec<u8> = (0..i420_frame_len(w, h)).map(|i| i as u8).collect();
        let buffer = copy_into_i420(&frame, w, h);

        let (stride_y, stride_u, _) = buffer.strides();
        let (data_y, data_u, data_v) = buffer.data();
        for row in 0..h as usize {
            let start = row * stride_y as usize;
            let expected = &frame[row * w as usize..row * w as usize + w as usize];
            assert_eq!(&data_y[start..start + w as usize], expected, "luma row {row}");
        }

        let (cw, ch) = (w as usize / 2, h as usize / 2);
        let u_offset = w as usize * h as usize;
        let v_offset = u_offset + cw * ch;
        for row in 0..ch {
            let start = row * stride_u as usize;
            assert_eq!(
                &data_u[start..start + cw],
                &frame[u_offset + row * cw..u_offset + row * cw + cw],
                "u row {row}"
            );
            assert_eq!(
                &data_v[start..start + cw],
                &frame[v_offset + row * cw..v_offset + row * cw + cw],
                "v row {row}"
            );
        }
    }

    /// The stall message is the whole reason the variant is distinct: a run debugged on
    /// another machine has nothing but this line to go on.
    #[test]
    fn a_stall_names_itself_and_carries_ffmpeg_output() {
        let err = RtspError::Stall {
            timeout: Duration::from_secs(15),
            bytes_into_frame: 0,
            stderr: "method DESCRIBE failed: 401 Unauthorized".to_string(),
        };
        let message = err.to_string();
        assert!(message.contains("stalled"), "{message}");
        assert!(message.contains("401 Unauthorized"), "{message}");
    }

    /// A missing ffmpeg is the single most likely first failure on a new host, and the
    /// message must rule out the reading that the run continued on the pattern.
    #[test]
    fn a_missing_ffmpeg_rules_out_a_synthetic_fallback() {
        let err = RtspError::FfmpegUnavailable {
            program: "ffmpeg".to_string(),
            source: "No such file or directory (os error 2)".to_string(),
        };
        let message = err.to_string();
        assert!(message.contains("PATH"), "{message}");
        assert!(message.contains("will not fall back"), "{message}");
    }

    /// A truncated frame and an ended stream have different causes and must read
    /// differently, since neither can be reproduced by whoever reads the log.
    #[test]
    fn truncation_and_stream_end_are_distinguishable_in_the_log() {
        let ended = RtspError::StreamEnded { frames_read: 300, stderr: "eof".to_string() };
        assert!(ended.to_string().contains("300 frames"), "{ended}");

        let torn =
            RtspError::TruncatedFrame { got: 100, expected: 3_110_400, stderr: "eof".to_string() };
        let message = torn.to_string();
        assert!(message.contains("mid-frame"), "{message}");
        assert!(message.contains("discarded"), "{message}");
    }

    #[test]
    fn the_stderr_tail_is_bounded_and_keeps_the_newest_lines() {
        let tail = StderrTail::default();
        assert!(tail.snapshot().contains("no diagnostics"));

        for i in 0..STDERR_TAIL_LINES + 5 {
            tail.push(format!("line {i}"));
        }
        let snapshot = tail.snapshot();
        assert!(!snapshot.contains("line 0"), "oldest lines must be dropped");
        assert!(
            snapshot.contains(&format!("line {}", STDERR_TAIL_LINES + 4)),
            "newest line must be kept"
        );
        assert_eq!(snapshot.lines().count(), STDERR_TAIL_LINES);
    }

    /// TCP is the default because UDP RTSP degrades by dropping media silently, which
    /// reads as a broken camera rather than a network problem.
    #[test]
    fn the_default_transport_is_tcp() {
        assert_eq!(RtspTransport::default(), RtspTransport::Tcp);
        assert_eq!(RtspTransport::Tcp.as_str(), "tcp");
        assert_eq!(RtspTransport::Udp.as_str(), "udp");
    }

    // -----------------------------------------------------------------------
    // Subprocess-level tests.
    //
    // These drive the real ffmpeg pipe — the framing, the stall deadline, the stderr
    // drain and the exit reporting — against a locally generated input, since no IP
    // camera is reachable from a build host. Everything below the RTSP demuxer is
    // therefore covered; the RTSP negotiation itself is not, and cannot be here.
    // -----------------------------------------------------------------------

    /// Whether ffmpeg is present, so these tests skip rather than fail on a host without
    /// it. A missing ffmpeg is already reported loudly at run time by
    /// [`RtspError::FfmpegUnavailable`]; failing the build for it would only stop a
    /// contributor who never touches this path.
    fn ffmpeg_present() -> bool {
        Command::new("ffmpeg")
            .arg("-version")
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status()
            .is_ok_and(|s| s.success())
    }

    fn options(width: u32, height: u32, stall_timeout: Duration) -> RtspOptions {
        RtspOptions { width, height, fps: 30, transport: RtspTransport::Tcp, stall_timeout }
    }

    /// The whole pipe, end to end: ffmpeg's packed output is framed at exactly
    /// `w * h * 3 / 2` and lands as a correctly-sized I420 buffer. If the frame arithmetic
    /// or the row-wise copy were wrong, every frame in a real run would be sheared.
    #[test]
    fn frames_arrive_whole_from_a_real_ffmpeg_pipe() {
        if !ffmpeg_present() {
            eprintln!("skipping: ffmpeg not on PATH");
            return;
        }
        let selector = RtspSelector::new("rtsp://test.invalid/generated");
        let (w, h) = (64u32, 48u32);
        let mut source = RtspFrameSource::open_input(
            &selector,
            &options(w, h, Duration::from_secs(20)),
            &["-f".to_string(), "lavfi".to_string()],
            "testsrc=size=64x48:rate=30:duration=1",
        )
        .expect("ffmpeg must start");

        assert_eq!(source.width(), w);
        assert_eq!(source.height(), h);

        for _ in 0..3 {
            let buffer = source.next_buffer().expect("a frame must arrive whole");
            assert_eq!(buffer.width(), w);
            assert_eq!(buffer.height(), h);
        }

        // testsrc stops after one second, and the pipe then closes on a frame boundary:
        // an ended stream, never a truncated frame.
        let mut ended = false;
        for _ in 0..200 {
            match source.next_buffer() {
                Ok(_) => continue,
                Err(RtspError::StreamEnded { .. }) | Err(RtspError::Exited { .. }) => {
                    ended = true;
                    break;
                }
                Err(e) => panic!("unexpected error at end of stream: {e}"),
            }
        }
        assert!(ended, "the stream must report ending rather than blocking");
    }

    /// The identity is what reaches the run record, and it must never carry the password
    /// that was on the command line.
    #[test]
    fn the_recorded_identity_is_redacted_and_carries_the_negotiated_geometry() {
        if !ffmpeg_present() {
            eprintln!("skipping: ffmpeg not on PATH");
            return;
        }
        let selector = RtspSelector::new("rtsp://admin:hunter2@192.168.100.123/full1080p");
        let source = RtspFrameSource::open_input(
            &selector,
            // Odd dimensions must round up: yuv420p has no odd representation.
            &options(63, 47, Duration::from_secs(20)),
            &["-f".to_string(), "lavfi".to_string()],
            "testsrc=size=64x48:rate=30:duration=1",
        )
        .expect("ffmpeg must start");

        let identity = source.identity();
        assert_eq!(identity.url, "rtsp://***@192.168.100.123/full1080p");
        assert_eq!(identity.requested, "rtsp://***@192.168.100.123/full1080p");
        assert!(!format!("{identity:?}").contains("hunter2"), "credentials must not be recorded");
        assert_eq!((identity.negotiated_width, identity.negotiated_height), (64, 48));
        assert_eq!(identity.negotiated_fps, 30);
        assert_eq!(identity.negotiated_format, "yuv420p");
        assert_eq!(identity.transport, RtspTransport::Tcp);
    }

    /// An unreachable stream must produce a bounded, named failure carrying ffmpeg's own
    /// diagnosis — this is the exact path the first real run against a misconfigured
    /// camera will take, and the person reading it will have nothing else.
    #[test]
    fn an_unopenable_input_reports_ffmpegs_own_diagnosis() {
        if !ffmpeg_present() {
            eprintln!("skipping: ffmpeg not on PATH");
            return;
        }
        let selector = RtspSelector::new("rtsp://test.invalid/nope");
        let mut source = RtspFrameSource::open_input(
            &selector,
            &options(64, 48, Duration::from_secs(20)),
            &[],
            "/nonexistent/path/to/nothing.mp4",
        )
        .expect("ffmpeg itself must start; the input is what fails");

        let err = source.next_buffer().expect_err("an unopenable input must fail");
        // Either shape is correct — ffmpeg exits, and whether the reader thread observes
        // the closed pipe first is a race — but both must name the process and replay its
        // stderr rather than reporting a bare read failure.
        let message = err.to_string();
        assert!(
            matches!(err, RtspError::Exited { .. } | RtspError::StreamEnded { .. }),
            "unexpected variant: {err:?}"
        );
        assert!(
            message.contains("nonexistent") || message.contains("No such file"),
            "ffmpeg's explanation must reach the message: {message}"
        );
    }

    /// The deadline is the whole reason a wedged stream fails instead of hanging. A
    /// too-short timeout against a live process must produce a stall, not a hang.
    #[test]
    fn a_read_slower_than_the_deadline_is_reported_as_a_stall() {
        if !ffmpeg_present() {
            eprintln!("skipping: ffmpeg not on PATH");
            return;
        }
        let selector = RtspSelector::new("rtsp://test.invalid/slow");
        // A 1 fps source read with a 100 ms deadline: ffmpeg is alive and healthy, and
        // simply has not emitted yet — exactly the shape of a wedged RTSP session.
        let slow = RtspOptions { fps: 1, ..options(320, 240, Duration::from_millis(100)) };
        let mut source = RtspFrameSource::open_input(
            &selector,
            &slow,
            &["-f".to_string(), "lavfi".to_string(), "-re".to_string()],
            "testsrc=size=320x240:rate=1:duration=30",
        )
        .expect("ffmpeg must start");

        let started = std::time::Instant::now();
        let mut stalled = false;
        for _ in 0..20 {
            match source.next_buffer() {
                Err(RtspError::Stall { timeout, .. }) => {
                    assert_eq!(timeout, Duration::from_millis(100));
                    stalled = true;
                    break;
                }
                Ok(_) => continue,
                Err(e) => panic!("expected a stall, got: {e}"),
            }
        }
        assert!(stalled, "a read past its deadline must report a stall");
        // The bound must actually bound: a hang here is the failure this exists to prevent.
        assert!(started.elapsed() < Duration::from_secs(10), "the deadline did not bound the read");
    }
}
