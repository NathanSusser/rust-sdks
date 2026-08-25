//! `teleop-harness`: one process runs one cell of the teleoperation test matrix.
//!
//! Exits non-zero on session failure. The Python planner treats a non-zero exit as a bad
//! run, which is the correct behavior: a cell that could not join, could not publish its
//! requested codec, or lost its session mid-run did not measure the thing it was asked to
//! measure, and papering over that would put an unmeasured cell in the report.

use std::process::ExitCode;

use clap::Parser;

use teleop_test_matrix::cli::Args;
use teleop_test_matrix::run;

fn main() -> ExitCode {
    env_logger::Builder::from_env(env_logger::Env::default().default_filter_or("info")).init();

    let args = Args::parse();

    if args.validate_args {
        println!(
            "ok: codec={} encoder={} buffering_mode={} control_transport={} audio_source={} \
             camera_source={} fault={} {}x{}@{} snapshots={}",
            args.codec.as_str(),
            args.encoder.as_str(),
            args.buffering_mode.as_str(),
            args.control_transport.as_str(),
            args.audio_source.as_str(),
            args.camera_source,
            args.fault.map_or("none", |f| f.as_str()),
            args.width,
            args.height,
            args.fps,
            args.snapshots_out.display()
        );
        return ExitCode::SUCCESS;
    }

    // The zero-playout-delay field trial mutates process-global runtime state and fails
    // if the WebRTC runtime is already up without it. It has to happen before anything
    // constructs a room, so it runs before the async runtime is even built.
    if let Err(e) = teleop_test_matrix::session::apply_buffering_mode(args.buffering_mode) {
        log::error!("{e}");
        return ExitCode::FAILURE;
    }

    let runtime = match tokio::runtime::Runtime::new() {
        Ok(rt) => rt,
        Err(e) => {
            log::error!("cannot start async runtime: {e}");
            return ExitCode::FAILURE;
        }
    };

    match runtime.block_on(run::execute(args)) {
        Ok(outcome) => {
            log::info!(
                "run complete: {} snapshots, {} control samples published, {} received",
                outcome.snapshots_written,
                outcome.seq_published,
                outcome.distinct_seq_received
            );
            ExitCode::SUCCESS
        }
        Err(e) => {
            log::error!("run failed: {e}");
            // A connection failure is reported distinctly so the runner can retry it
            // without pattern-matching stderr. A 3-second window of a Tier 0 sweep lost
            // 15 consecutive runs this way, spanning all four codecs, and the cause was
            // plainly transient rather than a property of any cell.
            if e.is_retryable() {
                ExitCode::from(EXIT_RETRYABLE)
            } else {
                ExitCode::FAILURE
            }
        }
    }
}

/// Exit status for a failure the runner may retry: the session never established, so the
/// cell measured nothing and nothing about the cell caused it.
///
/// Distinct from [`ExitCode::FAILURE`], which still means "this run produced no usable
/// data" — a retryable exit is not a success and must never be scored as one.
pub const EXIT_RETRYABLE: u8 = 75;
