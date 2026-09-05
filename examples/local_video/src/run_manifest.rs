//! Provenance for a single capture run, written next to its CSV.
//!
//! Every provenance question in the 4 Sep programme — which bitrate cap was in
//! force, whether `--low-latency` was set, whether NVENC was actually compiled
//! in — was answerable only by inference from logs that were never intended as
//! evidence. Two runs were nearly discarded as uncitable and one review section
//! was written against a bitrate cap that had been guessed rather than read.
//!
//! The run has all of this at execution time. This module writes it down while
//! it is still fact, so the CSV and its provenance travel together.
//!
//! Capture is best-effort by design: a manifest field that cannot be read
//! becomes `null` rather than failing the run. Losing a capture because the
//! governor file moved would be a worse outcome than an incomplete manifest.

use std::path::Path;
use std::process::Command;

use serde_json::{json, Value};

/// Reads a sysfs/procfs value, trimmed. `None` if absent or unreadable.
fn read_trimmed(path: &str) -> Option<String> {
    std::fs::read_to_string(path).ok().map(|s| s.trim().to_string()).filter(|s| !s.is_empty())
}

fn git_output(args: &[&str]) -> Option<String> {
    let out = Command::new("git").args(args).output().ok()?;
    if !out.status.success() {
        return None;
    }
    let text = String::from_utf8(out.stdout).ok()?;
    Some(text.trim().to_string())
}

/// Everything about the machine and the build that the run did not choose.
fn environment() -> Value {
    // A dirty tree means the committed sha does not describe the binary, which
    // is the difference between a reproducible run and an anecdote.
    let dirty = git_output(&["status", "--porcelain"]).map(|s| !s.is_empty());

    json!({
        "hostname": read_trimmed("/proc/sys/kernel/hostname"),
        "kernel": read_trimmed("/proc/sys/kernel/osrelease"),
        "cpu_governor": read_trimmed("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor"),
        "cpu_epp": read_trimmed("/sys/devices/system/cpu/cpu0/cpufreq/energy_performance_preference"),
        "git_sha": git_output(&["rev-parse", "HEAD"]),
        "git_dirty": dirty,
        "cuda_home": std::env::var("CUDA_HOME").ok(),
        "ssl_cert_file": std::env::var("SSL_CERT_FILE").ok(),
    })
}

/// A run's manifest. Built at start, amended at end, written at both points so
/// a run killed mid-flight still leaves provenance behind.
pub(crate) struct RunManifest {
    path: std::path::PathBuf,
    root: Value,
}

impl RunManifest {
    /// `csv_path` is the run's CSV; the manifest sits beside it as
    /// `<stem>.manifest.json`.
    pub(crate) fn new(csv_path: &Path, role: &str) -> Self {
        let path = csv_path.with_extension("manifest.json");
        let root = json!({
            "role": role,
            "started_utc": chrono::Utc::now().to_rfc3339(),
            "invocation": {
                "argv": std::env::args().collect::<Vec<_>>(),
                "cwd": std::env::current_dir().ok().map(|p| p.display().to_string()),
            },
            "environment": environment(),
            // Filled by `set_media`, `set_window` and `finish`. Present as null
            // from the start so a truncated manifest still has the shape a
            // reader expects.
            "media": Value::Null,
            "window": Value::Null,
            "outcome": Value::Null,
        });
        Self { path, root }
    }

    /// What was *requested*. `encoder_implementation` is filled later, once the
    /// first stats tick reports what libwebrtc actually chose — the requested
    /// codec and the delivered encoder have disagreed before, silently.
    pub(crate) fn set_media(
        &mut self,
        width: u32,
        height: u32,
        fps: u32,
        codec: &str,
        max_bitrate: Option<u64>,
        test_pattern: Option<&str>,
    ) {
        self.root["media"] = json!({
            "requested_width": width,
            "requested_height": height,
            "requested_fps": fps,
            "requested_codec": codec,
            // Explicit null distinguishes "no cap passed, a preset applied" from
            // a cap of zero. That distinction is exactly what made A3's cap
            // ambiguous in review.
            "requested_max_bitrate_bps": max_bitrate,
            "test_pattern": test_pattern,
            "encoder_implementation": Value::Null,
        });
    }

    pub(crate) fn set_encoder_implementation(&mut self, implementation: &str) {
        if self.root["media"].is_object() {
            self.root["media"]["encoder_implementation"] = json!(implementation);
        }
    }

    pub(crate) fn set_window(&mut self, start_frame: Option<u32>, end_frame: Option<u32>) {
        self.root["window"] = json!({
            "log_start_frame_id": start_frame,
            "log_end_frame_id": end_frame,
        });
    }

    pub(crate) fn finish(&mut self, rows: u64, first_frame: Option<u32>, last_frame: Option<u32>, exit_reason: &str) {
        self.root["outcome"] = json!({
            "rows_written": rows,
            "first_frame_id": first_frame,
            "last_frame_id": last_frame,
            "exit_reason": exit_reason,
            "ended_utc": chrono::Utc::now().to_rfc3339(),
        });
    }

    /// Close the manifest from the CSV it accompanies.
    ///
    /// Reading the row count and frame range back off disk rather than
    /// threading counters out of the logger keeps this correct even when the
    /// writer is owned by a task that has already ended — and it measures the
    /// artifact that will actually be cited, not a tally of what we believe was
    /// written.
    pub(crate) fn finish_from_csv(&mut self, csv_path: &Path, exit_reason: &str) {
        let (mut rows, mut first, mut last) = (0u64, None, None);
        if let Ok(text) = std::fs::read_to_string(csv_path) {
            // Column 3 is frame_id; header is skipped by `skip(1)`.
            for line in text.lines().skip(1).filter(|l| !l.is_empty()) {
                rows += 1;
                if let Some(id) = line.split(',').nth(2).and_then(|v| v.parse::<u32>().ok()) {
                    first.get_or_insert(id);
                    last = Some(id);
                }
            }
        }
        self.finish(rows, first, last, exit_reason);
    }

    /// Write (or rewrite) the manifest. Cheap enough to call at start and end.
    pub(crate) fn write(&self) -> std::io::Result<()> {
        if let Some(parent) = self.path.parent().filter(|p| !p.as_os_str().is_empty()) {
            std::fs::create_dir_all(parent)?;
        }
        let text = serde_json::to_string_pretty(&self.root).unwrap_or_else(|_| "{}".to_string());
        std::fs::write(&self.path, text)
    }

    pub(crate) fn path(&self) -> &Path {
        &self.path
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn manifest_path_sits_beside_the_csv() {
        let m = RunManifest::new(Path::new("./results-a1/publisher.csv"), "publisher");
        assert_eq!(m.path(), Path::new("./results-a1/publisher.manifest.json"));
    }

    #[test]
    fn absent_max_bitrate_is_null_not_zero() {
        // The distinction that made A3's cap ambiguous in review: no cap passed
        // means a preset applied, which is not the same as a cap of zero.
        let mut m = RunManifest::new(Path::new("/tmp/x.csv"), "publisher");
        m.set_media(1920, 1080, 30, "h264", None, Some("2"));
        assert!(m.root["media"]["requested_max_bitrate_bps"].is_null());

        m.set_media(1920, 1080, 30, "h264", Some(10_000_000), Some("2"));
        assert_eq!(m.root["media"]["requested_max_bitrate_bps"], json!(10_000_000u64));
    }

    #[test]
    fn sections_are_present_as_null_before_they_are_filled() {
        // A run killed mid-flight still leaves a manifest with the expected
        // shape rather than missing keys.
        let m = RunManifest::new(Path::new("/tmp/y.csv"), "publisher");
        assert!(m.root["media"].is_null());
        assert!(m.root["outcome"].is_null());
        assert!(m.root["invocation"]["argv"].is_array());
    }

    #[test]
    fn finish_from_csv_reads_the_row_count_and_frame_range_off_disk() {
        // The arm-1 manifest shipped with outcome:null because finish() existed
        // and was never called. This asserts the close path actually reads the
        // artifact rather than trusting a counter.
        let dir = std::env::temp_dir().join(format!("manifest-test-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let csv = dir.join("publisher.csv");
        std::fs::write(
            &csv,
            "sample,elapsed_ms,frame_id,rest\n1,0.0,4,x\n2,33.3,5,x\n3,66.6,3600,x\n",
        )
        .unwrap();

        let mut m = RunManifest::new(&csv, "publisher");
        m.finish_from_csv(&csv, "completed");

        assert_eq!(m.root["outcome"]["rows_written"], json!(3));
        assert_eq!(m.root["outcome"]["first_frame_id"], json!(4));
        assert_eq!(m.root["outcome"]["last_frame_id"], json!(3600));
        assert_eq!(m.root["outcome"]["exit_reason"], json!("completed"));
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn finish_from_csv_on_a_missing_file_still_closes_the_manifest() {
        // A run that died before writing a CSV should still record an outcome
        // rather than leaving outcome:null, which is indistinguishable from an
        // emitter that never closed.
        let mut m = RunManifest::new(Path::new("/nonexistent/publisher.csv"), "publisher");
        m.finish_from_csv(Path::new("/nonexistent/publisher.csv"), "aborted");
        assert_eq!(m.root["outcome"]["rows_written"], json!(0));
        assert!(m.root["outcome"]["first_frame_id"].is_null());
        assert_eq!(m.root["outcome"]["exit_reason"], json!("aborted"));
    }

    #[test]
    fn finish_records_the_frame_range_and_reason() {
        let mut m = RunManifest::new(Path::new("/tmp/z.csv"), "publisher");
        m.finish(3601, Some(60), Some(3660), "end_frame_reached");
        assert_eq!(m.root["outcome"]["rows_written"], json!(3601));
        assert_eq!(m.root["outcome"]["first_frame_id"], json!(60));
        assert_eq!(m.root["outcome"]["exit_reason"], json!("end_frame_reached"));
    }
}
