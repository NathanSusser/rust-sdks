//! `export-source`: writes the harness's video source to a Y4M file for offline analysis.
//!
//! This exists so `webrtc-vmaf` can encode **the same content the matrix transports**. See
//! [`teleop_test_matrix::export`] for why the pattern is exported by driving the harness's
//! own generator rather than by reimplementing it, and `vmaf/README.md` for what the
//! resulting VMAF numbers do and do not say.
//!
//! No LiveKit room is involved: exporting is a local file operation and stays runnable on a
//! host with no credentials and no network.
//!
//! ```text
//! export-source --output pattern.y4m --width 1280 --height 720 --fps 30 --duration-s 10
//! export-source --output cam.y4m --camera-source rtsp://192.168.100.123/full1080p --duration-s 10
//! ```

use std::path::PathBuf;
use std::process::ExitCode;

use clap::Parser;

use teleop_test_matrix::cli::{VideoSourceSelector, TEST_PATTERN_SOURCE};
use teleop_test_matrix::export::{
    estimated_bytes, export, frames_for_duration, ExportRequest, ExportSource,
};
use teleop_test_matrix::rtsp::{RtspTransport, DEFAULT_STALL_TIMEOUT_S};

/// Exports the synthetic pattern, a capture device or an RTSP stream to a Y4M file.
#[derive(Parser, Debug)]
#[command(name = "export-source", about = "Export a harness video source to Y4M for VMAF")]
struct Args {
    /// Destination file. Y4M, which is self-describing: geometry and frame rate travel in
    /// the file rather than in whatever command line reads it next.
    #[arg(long)]
    output: PathBuf,

    /// Frame width. Rounded up to even, exactly as in a live run.
    #[arg(long, default_value_t = 1280)]
    width: u32,

    /// Frame height.
    #[arg(long, default_value_t = 720)]
    height: u32,

    /// Frame rate, written into the Y4M header and requested of a camera.
    #[arg(long, default_value_t = 30)]
    fps: u32,

    /// How many frames to write. Takes precedence over `--duration-s`.
    #[arg(long)]
    frames: Option<u64>,

    /// How many seconds to write, at `--fps`.
    ///
    /// The default is 10 s rather than something shorter because `webrtc-vmaf` seeks to
    /// 5 s to capture a preview still and fails on a clip that does not reach it.
    #[arg(long, default_value_t = 10.0)]
    duration_s: f64,

    /// Which source to export: `test_pattern`, a device index or name, or an `rtsp://` URL.
    ///
    /// Same contract as the harness's flag of the same name, so an export and a run
    /// resolve the same value to the same source.
    #[arg(long, default_value = TEST_PATTERN_SOURCE)]
    camera_source: String,

    /// RTSP media transport, when `--camera-source` is a URL.
    #[arg(long, value_enum, default_value_t = RtspTransport::Tcp)]
    rtsp_transport: RtspTransport,

    /// Per-frame read deadline for an RTSP source, in seconds.
    #[arg(long, default_value_t = DEFAULT_STALL_TIMEOUT_S)]
    rtsp_stall_timeout_s: u64,

    /// Print what would be written and exit, without opening a source or a file.
    #[arg(long)]
    dry_run: bool,
}

impl Args {
    /// Frames to write: an explicit count if given, otherwise the duration at `--fps`.
    fn frame_count(&self) -> u64 {
        self.frames.unwrap_or_else(|| frames_for_duration(self.duration_s, self.fps))
    }

    /// Resolves `--camera-source` the way [`teleop_test_matrix::cli::Args`] does.
    /// Reuses the harness's own resolution so `test_pattern` versus a device name versus a
    /// URL is decided identically in both binaries.
    fn source(&self) -> ExportSource {
        match VideoSourceSelector::resolve(&self.camera_source) {
            None => ExportSource::Synthetic,
            Some(VideoSourceSelector::Rtsp(selector)) => ExportSource::Rtsp(
                selector,
                self.rtsp_transport,
                std::time::Duration::from_secs(self.rtsp_stall_timeout_s.max(1)),
            ),
            Some(VideoSourceSelector::Device(selector)) => ExportSource::Device(selector),
        }
    }

    /// The source as it may be printed, with any RTSP credential redacted.
    ///
    /// An `rtsp://` `--camera-source` commonly embeds `user:pass@`, and `--dry-run` output
    /// is routinely pasted into a terminal log. A non-URL value passes through unchanged.
    fn redacted_source(&self) -> String {
        teleop_test_matrix::rtsp::redact_url(&self.camera_source)
    }
}

fn main() -> ExitCode {
    env_logger::Builder::from_env(env_logger::Env::default().default_filter_or("info")).init();

    let args = Args::parse();
    let frames = args.frame_count();

    if args.dry_run {
        println!(
            "would write {} frames of {} at {}x{}@{} ({:.1} s, ~{:.1} MB) to {}",
            frames,
            args.redacted_source(),
            args.width,
            args.height,
            args.fps,
            frames as f64 / args.fps.max(1) as f64,
            estimated_bytes(args.width, args.height, args.fps, 1, frames) as f64 / 1e6,
            args.output.display()
        );
        return ExitCode::SUCCESS;
    }

    let request = ExportRequest {
        source: args.source(),
        width: args.width,
        height: args.height,
        fps: args.fps,
        frames,
        output: args.output.clone(),
    };

    match export(&request) {
        Ok(summary) => {
            // Printed on stdout, not only logged, so a wrapper can read the geometry back
            // without parsing log lines.
            println!(
                "wrote {} frames {}x{}@{} ({:.1} s, {:.1} MB) source={} path={}",
                summary.frames,
                summary.width,
                summary.height,
                summary.fps,
                summary.duration_s(),
                summary.bytes as f64 / 1e6,
                summary.source_label,
                args.output.display()
            );
            if summary.duration_s() < 5.0 {
                // webrtc-vmaf seeks to 5 s for its preview still and raises otherwise, so a
                // short export fails there rather than here without this warning.
                log::warn!(
                    "clip is {:.1} s; webrtc-vmaf seeks to 5 s for a preview still and will \
                     fail on a shorter one",
                    summary.duration_s()
                );
            }
            ExitCode::SUCCESS
        }
        Err(e) => {
            log::error!("{e}");
            ExitCode::FAILURE
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn args_for(source: &str) -> Args {
        Args::parse_from(["export-source", "--output", "o.y4m", "--camera-source", source])
    }

    /// The same guarantee the harness makes: a credential on the command line must not
    /// reach stdout, since `--dry-run` output is pasted into logs and tickets.
    #[test]
    fn a_credential_never_reaches_the_printed_source() {
        let printed = args_for("rtsp://admin:hunter2@10.0.0.5/s").redacted_source();
        assert!(!printed.contains("hunter2"), "{printed}");
        assert_eq!(printed, "rtsp://***@10.0.0.5/s");
        // A device name is not a URL and must survive verbatim.
        assert_eq!(args_for("FaceTime HD Camera").redacted_source(), "FaceTime HD Camera");
    }

    /// `--camera-source` must name the same source here as in a run, or an export is
    /// compared against a run of different content.
    #[test]
    fn the_source_resolves_the_same_way_the_harness_resolves_it() {
        assert!(matches!(args_for("test_pattern").source(), ExportSource::Synthetic));
        // Case-insensitively, so `Test_Pattern` does not go looking for a camera.
        assert!(matches!(args_for("Test_Pattern").source(), ExportSource::Synthetic));
        assert!(matches!(args_for("rtsp://10.0.0.5/s").source(), ExportSource::Rtsp(..)));
        assert!(matches!(args_for("0").source(), ExportSource::Device(_)));
    }

    /// An explicit `--frames` must win over the duration default, and a duration must
    /// convert at the requested rate rather than at a hardcoded 30.
    #[test]
    fn frame_count_prefers_an_explicit_count_over_a_duration() {
        let explicit = Args::parse_from([
            "export-source",
            "--output",
            "o.y4m",
            "--frames",
            "7",
            "--duration-s",
            "60",
        ]);
        assert_eq!(explicit.frame_count(), 7);

        let timed = Args::parse_from([
            "export-source",
            "--output",
            "o.y4m",
            "--duration-s",
            "4",
            "--fps",
            "10",
        ]);
        assert_eq!(timed.frame_count(), 40);
    }

    /// The default has to clear webrtc-vmaf's 5 s preview seek, or the very first sweep a
    /// user runs fails inside the tool for a reason that has nothing to do with codecs.
    #[test]
    fn the_default_clip_is_long_enough_for_webrtc_vmaf() {
        let defaults = Args::parse_from(["export-source", "--output", "o.y4m"]);
        assert!(defaults.duration_s >= 5.0);
        assert_eq!(defaults.frame_count(), 300);
    }
}
