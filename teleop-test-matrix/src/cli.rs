//! Command-line contract shared with `run_matrix.py`.
//!
//! Every flag emitted by `run_matrix.py --dry-run` is accepted here. Adding a flag on
//! one side without the other breaks the matrix silently, so the two must move together.

use clap::{Parser, ValueEnum};
use livekit::options::{VideoCodec, VideoEncoderBackend};

use crate::camera::CameraSelector;
use crate::rtsp::{RtspSelector, RtspTransport};

/// The `--camera-source` value selecting the deterministic generated pattern.
///
/// Matches `run_matrix.py`'s default for the same flag and the `environment.camera_source`
/// value already written into every existing run record, so a record produced before the
/// camera path existed reads identically to one produced after it.
pub const TEST_PATTERN_SOURCE: &str = "test_pattern";

/// What a `--camera-source` value resolved to.
///
/// One flag carries all three sources rather than three competing flags, so that
/// `run_matrix.py` keeps emitting exactly one and `environment.camera_source` keeps naming
/// exactly what ran. The variant is decided by the value's shape: a URL scheme routes to
/// RTSP, anything else to a local device.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum VideoSourceSelector {
    /// A local capture device, by enumeration index or name.
    Device(CameraSelector),
    /// An IP camera reached over RTSP.
    Rtsp(RtspSelector),
}

impl VideoSourceSelector {
    /// Resolves a `--camera-source` value, or `None` for the synthetic pattern.
    ///
    /// The pattern comparison is case-insensitive so `Test_Pattern` cannot accidentally be
    /// taken for a device name and open a camera on a run that meant to use the pattern.
    /// An `rtsp://` or `rtsps://` value routes to [`crate::rtsp`]; everything else is a
    /// local device.
    ///
    /// Associated with the type rather than left inside [`Args`] because `export-source`
    /// resolves the same flag: two copies of this rule would eventually disagree about
    /// which source a value names, and an export that came from a different source than
    /// the run it is compared against is exactly the defect the exporter exists to avoid.
    ///
    /// ```
    /// # use teleop_test_matrix::cli::VideoSourceSelector;
    /// assert!(VideoSourceSelector::resolve("test_pattern").is_none());
    /// assert!(matches!(
    ///     VideoSourceSelector::resolve("rtsp://10.0.0.5/s"),
    ///     Some(VideoSourceSelector::Rtsp(_))
    /// ));
    /// ```
    pub fn resolve(value: &str) -> Option<Self> {
        let value = value.trim();
        if value.eq_ignore_ascii_case(TEST_PATTERN_SOURCE) {
            return None;
        }
        if crate::rtsp::is_rtsp_url(value) {
            return Some(Self::Rtsp(RtspSelector::new(value)));
        }
        Some(Self::Device(CameraSelector::parse(value)))
    }
}

/// Video codec requested at publish time.
///
/// H.265 is deliberately absent: it is the one codec with an automatic publish-time
/// fallback to H.264, which would turn an H.265 cell into an H.264 cell without the run
/// record showing it.
#[derive(Copy, Clone, Debug, PartialEq, Eq, ValueEnum)]
pub enum Codec {
    H264,
    Vp8,
    Vp9,
    Av1,
}

impl Codec {
    /// Lowercase name as it appears in `matrix.yaml` and the run record.
    pub fn as_str(self) -> &'static str {
        match self {
            Self::H264 => "h264",
            Self::Vp8 => "vp8",
            Self::Vp9 => "vp9",
            Self::Av1 => "av1",
        }
    }
}

impl From<Codec> for VideoCodec {
    fn from(codec: Codec) -> Self {
        match codec {
            Codec::H264 => VideoCodec::H264,
            Codec::Vp8 => VideoCodec::VP8,
            Codec::Vp9 => VideoCodec::VP9,
            Codec::Av1 => VideoCodec::AV1,
        }
    }
}

/// Encoder backend request. The matrix always passes `auto`; the others exist so a
/// forced-backend run can be done by hand without patching the harness.
#[derive(Copy, Clone, Debug, PartialEq, Eq, ValueEnum)]
pub enum Encoder {
    Auto,
    Software,
    Hardware,
    Nvenc,
    Vaapi,
    #[value(name = "videotoolbox")]
    VideoToolbox,
}

impl Encoder {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Auto => "auto",
            Self::Software => "software",
            Self::Hardware => "hardware",
            Self::Nvenc => "nvenc",
            Self::Vaapi => "vaapi",
            Self::VideoToolbox => "videotoolbox",
        }
    }
}

impl From<Encoder> for VideoEncoderBackend {
    fn from(encoder: Encoder) -> Self {
        match encoder {
            Encoder::Auto => VideoEncoderBackend::Auto,
            Encoder::Software => VideoEncoderBackend::Software,
            Encoder::Hardware => VideoEncoderBackend::Hardware,
            Encoder::Nvenc => VideoEncoderBackend::Nvenc,
            Encoder::Vaapi => VideoEncoderBackend::Vaapi,
            Encoder::VideoToolbox => VideoEncoderBackend::VideoToolbox,
        }
    }
}

/// How the subscriber's jitter buffer is configured for this run.
///
/// This is a per-process axis: [`BufferingMode::ZeroJitter`] is applied through a
/// process-global field trial that cannot be undone, so one process serves exactly one
/// mode. `run_matrix.py` batches runs accordingly.
///
/// Value names are snake_case to match the axis values `matrix.yaml` defines and
/// `run_matrix.py` emits; clap's kebab-case default would reject every real invocation.
#[derive(Copy, Clone, Debug, PartialEq, Eq, ValueEnum)]
pub enum BufferingMode {
    /// No configuration; SDK default jitter buffer.
    ///
    /// NOT IN THE MATRIX: `matrix.yaml` locks `buffering_mode` to `zero_jitter`. This is
    /// the flagless default so that a manual invocation gets the untouched SDK behaviour
    /// rather than silently applying the irreversible zero-playout-delay field trial.
    #[value(name = "default")]
    Default,
    /// Subscriber-side forced 0/0 via `enable_zero_playout_delay`.
    #[value(name = "zero_jitter")]
    ZeroJitter,
}

impl BufferingMode {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Default => "default",
            Self::ZeroJitter => "zero_jitter",
        }
    }

    /// Whether this mode requires the process-global zero-playout-delay field trial.
    pub fn needs_zero_playout_delay(self) -> bool {
        matches!(self, Self::ZeroJitter)
    }
}

/// Transport carrying the 200 Hz control stream.
#[derive(Copy, Clone, Debug, PartialEq, Eq, ValueEnum)]
pub enum ControlTransport {
    /// Data track with an explicit one-frame receive buffer (the SDK default is 16).
    #[value(name = "data_track_buf1")]
    DataTrackBuf1,
    /// Legacy reliable data channel; reproduces the head-of-line-blocking finding.
    #[value(name = "dc_reliable")]
    DcReliable,
    /// Legacy lossy data channel.
    #[value(name = "dc_lossy")]
    DcLossy,
}

impl ControlTransport {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::DataTrackBuf1 => "data_track_buf1",
            Self::DcReliable => "dc_reliable",
            Self::DcLossy => "dc_lossy",
        }
    }

    /// Whether this transport uses the `livekit-datatrack` path rather than a data channel.
    pub fn is_data_track(self) -> bool {
        matches!(self, Self::DataTrackBuf1)
    }
}

/// Audio source used when `--audio` is set.
#[derive(Copy, Clone, Debug, PartialEq, Eq, ValueEnum)]
pub enum AudioSourceKind {
    /// Deterministic generated tone. A microphone would make `audio_level`
    /// environment-dependent and the runs non-reproducible.
    #[value(name = "synthetic_tone")]
    SyntheticTone,
    /// Digital silence; exercises the silent-source validity gate.
    #[value(name = "silence")]
    Silence,
}

impl AudioSourceKind {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::SyntheticTone => "synthetic_tone",
            Self::Silence => "silence",
        }
    }
}

/// Fault class injected during a T-5 availability run. Recorded only; the harness does
/// not inject faults itself — the runner applies them out of band.
#[derive(Copy, Clone, Debug, PartialEq, Eq, ValueEnum)]
pub enum Fault {
    #[value(name = "baseline_soak")]
    BaselineSoak,
    #[value(name = "brief_blackout")]
    BriefBlackout,
    #[value(name = "fade_burst")]
    FadeBurst,
    #[value(name = "handover_sim")]
    HandoverSim,
    #[value(name = "ice_restart")]
    IceRestart,
    #[value(name = "signal_drop")]
    SignalDrop,
}

impl Fault {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::BaselineSoak => "baseline_soak",
            Self::BriefBlackout => "brief_blackout",
            Self::FadeBurst => "fade_burst",
            Self::HandoverSim => "handover_sim",
            Self::IceRestart => "ice_restart",
            Self::SignalDrop => "signal_drop",
        }
    }
}

/// One measurement run of the teleoperation test matrix.
#[derive(Parser, Debug, Clone)]
#[command(name = "teleop-harness", about = "LiveKit Rust teleoperation measurement harness")]
pub struct Args {
    /// LiveKit server URL. Falls back to `LIVEKIT_URL`.
    #[arg(long)]
    pub url: Option<String>,

    /// API key. Falls back to `LIVEKIT_API_KEY`.
    #[arg(long)]
    pub api_key: Option<String>,

    /// API secret. Falls back to `LIVEKIT_API_SECRET`.
    #[arg(long)]
    pub api_secret: Option<String>,

    /// Room to join. Created if it does not exist.
    #[arg(long)]
    pub room_name: String,

    /// Total session length including warmup.
    #[arg(long = "duration-s")]
    pub duration_s: u64,

    /// Leading seconds excluded from scoring. Recorded, not enforced here.
    #[arg(long = "warmup-s", default_value_t = 15)]
    pub warmup_s: u64,

    #[arg(long, value_enum, default_value_t = Codec::H264)]
    pub codec: Codec,

    #[arg(long, value_enum, default_value_t = Encoder::Auto)]
    pub encoder: Encoder,

    #[arg(long, default_value_t = 1920)]
    pub width: u32,

    #[arg(long, default_value_t = 1080)]
    pub height: u32,

    #[arg(long, default_value_t = 30)]
    pub fps: u32,

    /// Video source: `test_pattern` (the matrix default), a local capture device given as
    /// an enumeration index or a substring of its name, or an `rtsp://` / `rtsps://` URL
    /// for an IP camera.
    ///
    /// Every camera is an opt-in realism spot-check and never a matrix default or a swept
    /// axis: a lens makes bitrate depend on scene content, lighting and framing, which
    /// breaks the cross-host comparability every cell rests on. A camera that cannot be
    /// opened fails the run rather than falling back — a run labelled `camera` that
    /// actually ran the pattern would be pooled with pattern runs and could not be
    /// detected afterwards.
    #[arg(long = "camera-source", default_value = TEST_PATTERN_SOURCE)]
    pub camera_source: String,

    /// RTSP media transport, used only when `--camera-source` is an RTSP URL.
    ///
    /// TCP by default: UDP RTSP degrades by silently dropping media on a filtered or
    /// congested path, which reaches the record as a camera producing missing frames rather
    /// than as the network problem it is.
    #[arg(long = "rtsp-transport", value_enum, default_value_t = RtspTransport::Tcp)]
    pub rtsp_transport: RtspTransport,

    /// Seconds a single RTSP frame read may take before the stream counts as stalled.
    ///
    /// A wedged RTSP session leaves ffmpeg alive with its pipe open and no bytes flowing,
    /// which is indistinguishable from a slow stream without a deadline. Sourced from
    /// `matrix.yaml` `meta.parameters.rtsp_stall_timeout_s`.
    #[arg(long = "rtsp-stall-timeout-s", default_value_t = crate::rtsp::DEFAULT_STALL_TIMEOUT_S)]
    pub rtsp_stall_timeout_s: u64,

    /// Encoder bitrate ceiling in bps.
    #[arg(long = "max-bitrate", default_value_t = 5_000_000)]
    pub max_bitrate: u64,

    /// Carry the capture wall-clock timestamp in-band as frame metadata (the G2G send stamp).
    #[arg(long = "attach-timestamp", default_value_t = false)]
    pub attach_timestamp: bool,

    /// Carry a monotonic frame id in-band, for G2G frame-loss accounting.
    #[arg(long = "attach-frame-id", default_value_t = false)]
    pub attach_frame_id: bool,

    #[arg(long = "buffering-mode", value_enum, default_value_t = BufferingMode::Default)]
    pub buffering_mode: BufferingMode,

    #[arg(long = "control-transport", value_enum, default_value_t = ControlTransport::DataTrackBuf1)]
    pub control_transport: ControlTransport,

    #[arg(long = "control-rate-hz", default_value_t = 200)]
    pub control_rate_hz: u32,

    /// Receive buffer depth in frames for the data-track control path.
    #[arg(long = "control-buffer-size", default_value_t = 1)]
    pub control_buffer_size: usize,

    /// Playout deadline a control sample must meet to count as on time.
    #[arg(long = "playout-window-ms")]
    pub playout_window_ms: Option<u64>,

    /// `RtcStats` poll cadence for everything other than the video track.
    #[arg(long = "stats-poll-hz", default_value_t = 1.0)]
    pub stats_poll_hz: f64,

    /// Video track poll cadence. Raised to 10 Hz for T-2, where keyframe recovery timing
    /// is a primary metric and 1 s resolution would be meaningless.
    #[arg(long = "video-poll-hz", default_value_t = 1.0)]
    pub video_poll_hz: f64,

    /// A poll is overbudget when its actual interval exceeds this multiple of nominal.
    #[arg(long = "poll-overbudget-multiplier", default_value_t = 1.5)]
    pub poll_overbudget_multiplier: f64,

    /// Four-timestamp probe rate, decoupled from the stats poll cadence.
    ///
    /// Probes rode the stats poll at 1 Hz until a Tier 0 sweep produced only 63 usable
    /// samples across a 105 s scored window — too few for the percentiles the latency bar
    /// is scored against. Several probes may be in flight at once, so this may exceed
    /// `1 / rtt` without probes being counted lost.
    #[arg(long = "probe-rate-hz", default_value_t = 20.0)]
    pub probe_rate_hz: f64,

    /// How long an unanswered probe may await its echo before it counts as lost.
    #[arg(long = "probe-lifetime-ms", default_value_t = 2000)]
    pub probe_lifetime_ms: u64,

    /// Number of concurrent sessions this process represents. Recorded only.
    #[arg(long, default_value_t = 1)]
    pub concurrency: u32,

    /// Fault class for a T-5 run. Recorded only; injection is the runner's job.
    #[arg(long, value_enum)]
    pub fault: Option<Fault>,

    /// Append-only JSON-lines destination, one object per stats poll.
    #[arg(long = "snapshots-out")]
    pub snapshots_out: std::path::PathBuf,

    /// Destination prefix for per-frame pipeline-stage CSVs, in the `local_video` format.
    ///
    /// Writes `<prefix>.pub.csv` and `<prefix>.sub.csv`, which
    /// `examples/local_video/scripts/generate_frame_report.py` renders to a PDF. These sit
    /// alongside the JSON-lines snapshots rather than replacing them: the snapshots carry
    /// the scored metrics and the validity gates, and the CSVs carry the per-frame stage
    /// decomposition that answers where a given frame's latency went.
    ///
    /// Requires `--attach-timestamp`, since every row is keyed by the in-band capture
    /// stamp. Off unless asked for: at 30 fps a run writes a row per frame per side, which
    /// is a different order of output than one snapshot per second.
    #[arg(long = "frame-csv-out", requires = "attach_timestamp")]
    pub frame_csv_out: Option<std::path::PathBuf>,

    /// Append-only JSON-lines log of every control sequence number published. This is
    /// the `control_delivered_pct` denominator; without it the metric biases toward
    /// passing at the window edges.
    #[arg(long = "publisher-seq-log")]
    pub publisher_seq_log: Option<std::path::PathBuf>,

    /// Publish an audio track alongside video.
    #[arg(long, default_value_t = false)]
    pub audio: bool,

    #[arg(long = "audio-source", value_enum, default_value_t = AudioSourceKind::SyntheticTone)]
    pub audio_source: AudioSourceKind,

    #[arg(long = "audio-bitrate", default_value_t = 250_000)]
    pub audio_bitrate: u64,

    /// Parse and validate the arguments, then exit without touching the network.
    ///
    /// Lets the runner check every invocation it plans to emit against the binary that
    /// will receive them, so a CLI contract drift is caught before a long sweep starts
    /// rather than after the first cell fails.
    #[arg(long = "validate-args", default_value_t = false)]
    pub validate_args: bool,
}

impl Args {
    /// Nominal stats poll interval.
    pub fn stats_interval(&self) -> std::time::Duration {
        hz_to_interval(self.stats_poll_hz)
    }

    /// Nominal video poll interval.
    pub fn video_interval(&self) -> std::time::Duration {
        hz_to_interval(self.video_poll_hz)
    }

    /// Nominal spacing between control samples.
    pub fn control_interval(&self) -> std::time::Duration {
        hz_to_interval(self.control_rate_hz as f64)
    }

    /// Nominal spacing between probes.
    ///
    /// Clamped to at least one control interval: a probe rides the next control sample
    /// rather than travelling on its own path, so a rate above the control rate cannot
    /// produce more probes and would only queue tokens.
    pub fn probe_interval(&self) -> std::time::Duration {
        hz_to_interval(self.probe_rate_hz).max(self.control_interval())
    }

    /// How long an unanswered probe may await its echo, in microseconds.
    pub fn probe_lifetime_us(&self) -> u64 {
        self.probe_lifetime_ms.saturating_mul(1_000)
    }

    /// The video source this run was asked for, or `None` for the synthetic pattern.
    ///
    /// The pattern comparison is case-insensitive so `Test_Pattern` cannot accidentally be
    /// taken for a device name and open a camera on a run that meant to use the pattern.
    /// An `rtsp://` or `rtsps://` value routes to [`crate::rtsp`]; everything else is a
    /// local device.
    pub fn video_source_selector(&self) -> Option<VideoSourceSelector> {
        VideoSourceSelector::resolve(&self.camera_source)
    }

    /// The `--camera-source` value with any RTSP credentials stripped.
    ///
    /// Everything that logs or records the source goes through this: an RTSP URL commonly
    /// embeds `user:pass@`, and both the run record and the runner's log are shared. A
    /// non-URL value is returned unchanged.
    pub fn redacted_camera_source(&self) -> String {
        crate::rtsp::redact_url(&self.camera_source)
    }

    /// How long a single RTSP frame read may take before the stream counts as stalled.
    pub fn rtsp_stall_timeout(&self) -> std::time::Duration {
        std::time::Duration::from_secs(self.rtsp_stall_timeout_s.max(1))
    }
}

/// Converts a rate in hertz to a period, clamping non-positive rates to one second so a
/// misconfigured cadence degrades to a slow loop rather than a busy one.
fn hz_to_interval(hz: f64) -> std::time::Duration {
    if hz <= 0.0 {
        return std::time::Duration::from_secs(1);
    }
    std::time::Duration::from_secs_f64(1.0 / hz)
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The exact invocation `run_matrix.py --dry-run` emits for a Q-7 AV1 cell. If this
    /// stops parsing, the matrix stops running.
    #[test]
    fn parses_matrix_invocation() {
        let argv = [
            "teleop-harness",
            "--url",
            "ws://127.0.0.1:7880",
            "--room-name",
            "teleop-q7_latency_definition-0",
            "--duration-s",
            "120",
            "--codec",
            "av1",
            "--encoder",
            "auto",
            "--width",
            "1920",
            "--height",
            "1080",
            "--fps",
            "30",
            "--max-bitrate",
            "5000000",
            "--attach-timestamp",
            "--attach-frame-id",
            "--buffering-mode",
            "zero_jitter",
            "--control-transport",
            "data_track_buf1",
            "--control-rate-hz",
            "200",
            "--control-buffer-size",
            "1",
            "--stats-poll-hz",
            "1",
            "--video-poll-hz",
            "1",
            "--warmup-s",
            "15",
            "--poll-overbudget-multiplier",
            "1.5",
            "--concurrency",
            "1",
            "--snapshots-out",
            "/tmp/run.jsonl",
            "--publisher-seq-log",
            "/tmp/run.seq.jsonl",
            "--audio",
            "--audio-source",
            "synthetic_tone",
            "--audio-bitrate",
            "250000",
            "--camera-source",
            "test_pattern",
        ];
        let args = Args::try_parse_from(argv).expect("matrix invocation must parse");
        assert_eq!(args.codec, Codec::Av1);
        // The matrix always runs the pattern. A camera would make bitrate depend on
        // scene content and break comparability across hosts.
        assert_eq!(args.video_source_selector(), None);
        assert_eq!(args.buffering_mode, BufferingMode::ZeroJitter);
        assert_eq!(args.control_transport, ControlTransport::DataTrackBuf1);
        assert!(args.attach_timestamp && args.attach_frame_id && args.audio);
        assert_eq!(args.control_buffer_size, 1);
    }

    /// T-2 raises the video poll cadence and adds a jitter playout window; T-5 adds a
    /// fault class and a long duration. Both shapes must parse.
    #[test]
    fn parses_t2_and_t5_shapes() {
        let t2 = Args::try_parse_from([
            "teleop-harness",
            "--room-name",
            "r",
            "--duration-s",
            "120",
            "--video-poll-hz",
            "10",
            "--playout-window-ms",
            "20",
            "--control-transport",
            "dc_reliable",
            "--snapshots-out",
            "/tmp/a.jsonl",
        ])
        .expect("T-2 shape must parse");
        assert_eq!(t2.video_poll_hz, 10.0);
        assert_eq!(t2.playout_window_ms, Some(20));
        assert_eq!(t2.control_transport, ControlTransport::DcReliable);

        let t5 = Args::try_parse_from([
            "teleop-harness",
            "--room-name",
            "r",
            "--duration-s",
            "3600",
            "--fault",
            "handover_sim",
            "--buffering-mode",
            "zero_jitter",
            "--concurrency",
            "70",
            "--snapshots-out",
            "/tmp/b.jsonl",
        ])
        .expect("T-5 shape must parse");
        assert_eq!(t5.fault, Some(Fault::HandoverSim));
        assert!(t5.buffering_mode.needs_zero_playout_delay());
        assert_eq!(t5.concurrency, 70);
    }

    /// Every axis value the matrix emits is snake_case, and every value the harness
    /// records must be the same string. Clap defaults to kebab-case, so a variant added
    /// without an explicit value name silently rejects the invocations `run_matrix.py`
    /// generates — this asserts the parse name and the recorded name agree for all of them.
    #[test]
    fn value_names_round_trip_through_the_parser() {
        fn assert_round_trip<T>(values: &[T], name_of: fn(T) -> &'static str)
        where
            T: Copy + PartialEq + std::fmt::Debug + clap::ValueEnum + Send + Sync + 'static,
        {
            for &value in values {
                let name = name_of(value);
                let parsed = T::from_str(name, false)
                    .unwrap_or_else(|_| panic!("{name} must parse as a value of this axis"));
                assert_eq!(parsed, value, "{name} parsed to the wrong variant");
            }
        }

        assert_round_trip(&[Codec::H264, Codec::Vp8, Codec::Vp9, Codec::Av1], Codec::as_str);
        assert_round_trip(
            &[BufferingMode::Default, BufferingMode::ZeroJitter],
            BufferingMode::as_str,
        );
        assert_round_trip(
            &[
                ControlTransport::DataTrackBuf1,
                ControlTransport::DcReliable,
                ControlTransport::DcLossy,
            ],
            ControlTransport::as_str,
        );
        assert_round_trip(
            &[AudioSourceKind::SyntheticTone, AudioSourceKind::Silence],
            AudioSourceKind::as_str,
        );
        assert_round_trip(
            &[
                Fault::BaselineSoak,
                Fault::BriefBlackout,
                Fault::FadeBurst,
                Fault::HandoverSim,
                Fault::IceRestart,
                Fault::SignalDrop,
            ],
            Fault::as_str,
        );
        assert_round_trip(
            &[
                Encoder::Auto,
                Encoder::Software,
                Encoder::Hardware,
                Encoder::Nvenc,
                Encoder::Vaapi,
                Encoder::VideoToolbox,
            ],
            Encoder::as_str,
        );
        assert_round_trip(&[RtspTransport::Tcp, RtspTransport::Udp], RtspTransport::as_str);
    }

    #[test]
    fn intervals_follow_rates() {
        let args = Args::try_parse_from([
            "teleop-harness",
            "--room-name",
            "r",
            "--duration-s",
            "10",
            "--control-rate-hz",
            "200",
            "--video-poll-hz",
            "10",
            "--snapshots-out",
            "/tmp/c.jsonl",
        ])
        .expect("parse");
        assert_eq!(args.control_interval(), std::time::Duration::from_millis(5));
        assert_eq!(args.video_interval(), std::time::Duration::from_millis(100));
        assert_eq!(args.stats_interval(), std::time::Duration::from_secs(1));
    }

    /// The probe rate is independent of the stats poll. Tying the two pinned probes to
    /// 1 Hz, which yielded 63 usable samples across a 105 s window — far too few for the
    /// percentiles the latency bar is scored against.
    #[test]
    fn probe_rate_is_independent_of_the_stats_poll() {
        let args = Args::try_parse_from([
            "teleop-harness",
            "--room-name",
            "r",
            "--duration-s",
            "10",
            "--stats-poll-hz",
            "1",
            "--probe-rate-hz",
            "20",
            "--snapshots-out",
            "/tmp/c.jsonl",
        ])
        .expect("parse");
        assert_eq!(args.stats_interval(), std::time::Duration::from_secs(1));
        assert_eq!(args.probe_interval(), std::time::Duration::from_millis(50));
    }

    /// A probe rides the next control sample, so the control rate is a hard ceiling on
    /// the probe rate. Above it the extra tokens would only queue.
    #[test]
    fn probe_rate_is_clamped_to_the_control_rate() {
        let args = Args::try_parse_from([
            "teleop-harness",
            "--room-name",
            "r",
            "--duration-s",
            "10",
            "--control-rate-hz",
            "200",
            "--probe-rate-hz",
            "5000",
            "--snapshots-out",
            "/tmp/c.jsonl",
        ])
        .expect("parse");
        assert_eq!(args.probe_interval(), args.control_interval());
    }

    #[test]
    fn nonpositive_rate_degrades_to_one_second() {
        assert_eq!(hz_to_interval(0.0), std::time::Duration::from_secs(1));
        assert_eq!(hz_to_interval(-5.0), std::time::Duration::from_secs(1));
    }

    /// A run that names no source must get the pattern. Camera is opt-in only: making it
    /// reachable by default would put scene-dependent bitrates into matrix cells.
    #[test]
    fn the_default_source_is_the_test_pattern() {
        let args = Args::try_parse_from([
            "teleop-harness",
            "--room-name",
            "r",
            "--duration-s",
            "10",
            "--snapshots-out",
            "/tmp/c.jsonl",
        ])
        .expect("parse");
        assert_eq!(args.camera_source, TEST_PATTERN_SOURCE);
        assert_eq!(args.video_source_selector(), None);
    }

    /// Parses a `--camera-source` value through the full CLI, as the matrix would.
    fn selector_for(value: &str) -> Option<VideoSourceSelector> {
        Args::try_parse_from([
            "teleop-harness",
            "--room-name",
            "r",
            "--duration-s",
            "10",
            "--snapshots-out",
            "/tmp/c.jsonl",
            "--camera-source",
            value,
        ])
        .expect("parse")
        .video_source_selector()
    }

    /// A camera is addressable by index or by name. The name is what survives moving to
    /// another host, where the same index is a different lens.
    #[test]
    fn a_camera_source_resolves_to_a_selector() {
        assert_eq!(selector_for("0"), Some(VideoSourceSelector::Device(CameraSelector::Index(0))));
        assert_eq!(
            selector_for("FaceTime HD Camera"),
            Some(VideoSourceSelector::Device(CameraSelector::Name(
                "FaceTime HD Camera".to_string()
            )))
        );
        // Surrounding whitespace comes from shell quoting, not from an operator naming a
        // device with a leading space.
        assert_eq!(selector_for("  test_pattern  "), None);
        // Case must not be the difference between the pattern and a device open.
        assert_eq!(selector_for("TEST_PATTERN"), None);
    }

    /// One flag serves all three sources, so the URL scheme is what routes an IP camera
    /// away from local device enumeration — `nokhwa` cannot open a network stream and would
    /// report the URL as a missing device.
    #[test]
    fn an_rtsp_url_routes_to_the_rtsp_source() {
        let url = "rtsp://192.168.100.123/full1080p";
        assert_eq!(selector_for(url), Some(VideoSourceSelector::Rtsp(RtspSelector::new(url))));
        assert_eq!(
            selector_for("rtsps://cam.local/4k"),
            Some(VideoSourceSelector::Rtsp(RtspSelector::new("rtsps://cam.local/4k")))
        );
        // Shell quoting again; the URL itself is left byte-for-byte, since RTSP paths are
        // case-sensitive on many cameras.
        assert_eq!(
            selector_for("  rtsp://192.168.100.123/Full1080p  "),
            Some(VideoSourceSelector::Rtsp(RtspSelector::new("rtsp://192.168.100.123/Full1080p")))
        );
        // A local device whose name merely mentions a scheme is still a device.
        assert!(matches!(selector_for("rtsp camera"), Some(VideoSourceSelector::Device(_))));
    }

    /// TCP transport and a bounded read are what turn a wedged RTSP session into an error
    /// rather than a run that hangs to its duration with nothing in the log.
    #[test]
    fn rtsp_transport_and_stall_timeout_have_safe_defaults() {
        let args = Args::try_parse_from([
            "teleop-harness",
            "--room-name",
            "r",
            "--duration-s",
            "10",
            "--snapshots-out",
            "/tmp/c.jsonl",
            "--camera-source",
            "rtsp://192.168.100.123/full1080p",
        ])
        .expect("parse");
        assert_eq!(args.rtsp_transport, RtspTransport::Tcp);
        assert_eq!(args.rtsp_stall_timeout(), std::time::Duration::from_secs(15));

        let explicit = Args::try_parse_from([
            "teleop-harness",
            "--room-name",
            "r",
            "--duration-s",
            "10",
            "--snapshots-out",
            "/tmp/c.jsonl",
            "--camera-source",
            "rtsp://192.168.100.123/4k",
            "--rtsp-transport",
            "udp",
            "--rtsp-stall-timeout-s",
            "30",
        ])
        .expect("parse");
        assert_eq!(explicit.rtsp_transport, RtspTransport::Udp);
        assert_eq!(explicit.rtsp_stall_timeout(), std::time::Duration::from_secs(30));

        // A zero timeout would make every read stall instantly; it degrades to one second
        // rather than to an unbounded wait.
        let zero = Args::try_parse_from([
            "teleop-harness",
            "--room-name",
            "r",
            "--duration-s",
            "10",
            "--snapshots-out",
            "/tmp/c.jsonl",
            "--rtsp-stall-timeout-s",
            "0",
        ])
        .expect("parse");
        assert_eq!(zero.rtsp_stall_timeout(), std::time::Duration::from_secs(1));
    }

    /// Only `zero_jitter` touches the process-global field trial. Getting this wrong
    /// mislabels an entire batch of runs.
    #[test]
    fn only_zero_jitter_uses_the_field_trial() {
        assert!(BufferingMode::ZeroJitter.needs_zero_playout_delay());
        assert!(!BufferingMode::Default.needs_zero_playout_delay());
    }
}
