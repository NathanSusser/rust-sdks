//! `run.json`: what the publisher actually ran, written as soon as it is known.
//!
//! The cell manifest used to be filled from the requested parameters, and a requested
//! value is not a measured one: a run on 2026-09-29 was labelled 512 kbps but ran at 2500,
//! and nothing in its record said so. This file exists so the manifest records what
//! actually ran rather than what was requested. It is written at publish time rather than
//! at exit so that a run killed mid-flight still leaves it behind.
//!
//! The encoder implementation is only known once WebRTC's stats report it, which is
//! usually after the track is published. The file is therefore written twice: once at
//! publish with `encoder_implementation: null`, and again, replacing it, on the first
//! stats poll that names the encoder. Every write is a temp file plus a rename, so a
//! reader polling the cell directory sees either the old file or the new one and never a
//! truncated one.

use std::io::Write;
use std::path::{Path, PathBuf};

use serde::Serialize;

use crate::snapshot::VideoOutbound;

/// The contents of `run.json`. Field names are the contract in `grid/CONTRACT.md`.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct RunJson {
    /// The encoder libwebrtc selected, or `null` until a stats poll has reported it.
    pub encoder_implementation: Option<String>,
    pub width: u32,
    pub height: u32,
    pub fps: u32,
    pub max_bitrate_bps: u64,
    /// Lowercase codec name, e.g. `h264` or `av1`.
    pub codec: String,
    /// Unix seconds at which the video track was published.
    pub started_at: f64,
    pub room: String,
    pub identity: String,
}

/// Owns `run.json` for one run and decides when it must be rewritten.
pub struct RunJsonWriter {
    path: PathBuf,
    record: RunJson,
}

impl RunJsonWriter {
    /// Writes the publish-time record and returns the writer that will complete it.
    ///
    /// A write failure is logged, not returned: a missing `run.json` costs the manifest
    /// one fallback to the harness log, while failing the run would cost the whole cell.
    pub fn start(path: PathBuf, record: RunJson) -> Self {
        let writer = Self { path, record };
        writer.write();
        writer
    }

    /// Whether the encoder implementation is still unknown, i.e. a rewrite is pending.
    pub fn is_pending(&self) -> bool {
        self.record.encoder_implementation.is_none()
    }

    /// Completes the record from a stats reading, rewriting the file the first time the
    /// encoder implementation is reported. Later readings are ignored: this records how
    /// the run started, and per-poll changes are already in the snapshots.
    ///
    /// Geometry and codec are taken from the same reading when it carries them, since
    /// that is what the encoder negotiated; the publish-time values stand otherwise.
    pub fn observe(&mut self, out: &VideoOutbound) {
        if !self.is_pending() || out.encoder_implementation.is_empty() {
            return;
        }
        self.record.encoder_implementation = Some(out.encoder_implementation.clone());
        if out.frame_width > 0 && out.frame_height > 0 {
            self.record.width = out.frame_width;
            self.record.height = out.frame_height;
        }
        if let Some(codec) =
            out.codec_mime_type.as_deref().and_then(crate::encoder::codec_from_mime_type)
        {
            self.record.codec = codec;
        }
        self.write();
    }

    fn write(&self) {
        if let Err(e) = write_atomically(&self.path, &self.record) {
            log::warn!("run.json write to {} failed: {e}", self.path.display());
        }
    }
}

/// Writes `record` to a sibling temp file and renames it over `path`.
fn write_atomically(path: &Path, record: &RunJson) -> std::io::Result<()> {
    if let Some(parent) = path.parent() {
        if !parent.as_os_str().is_empty() {
            std::fs::create_dir_all(parent)?;
        }
    }
    // The temp file must sit in the same directory: rename is only atomic within one
    // filesystem.
    let mut tmp = path.as_os_str().to_owned();
    tmp.push(".tmp");
    let tmp = PathBuf::from(tmp);
    let mut body = serde_json::to_vec_pretty(record).map_err(std::io::Error::other)?;
    body.push(b'\n');
    let mut file = std::fs::File::create(&tmp)?;
    file.write_all(&body)?;
    file.flush()?;
    drop(file);
    std::fs::rename(&tmp, path)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn record() -> RunJson {
        RunJson {
            encoder_implementation: None,
            width: 1008,
            height: 816,
            fps: 30,
            max_bitrate_bps: 512_000,
            codec: "h264".to_owned(),
            started_at: 1_790_384_410.25,
            room: "room".to_owned(),
            identity: "room-pub-1".to_owned(),
        }
    }

    fn outbound(encoder: &str, width: u32, height: u32, mime: Option<&str>) -> VideoOutbound {
        VideoOutbound {
            bytes_sent: 0,
            header_bytes_sent: 0,
            packets_sent: 0,
            retransmitted_packets_sent: 0,
            frames_encoded: 0,
            key_frames_encoded: 0,
            frames_sent: 0,
            frames_per_second: 0.0,
            frame_width: width,
            frame_height: height,
            total_encode_time_s: 0.0,
            target_bitrate_bps: 0.0,
            qp_sum: 0,
            nack_count: 0,
            pli_count: 0,
            fir_count: 0,
            quality_limitation_reason: "none".to_owned(),
            quality_limitation_cpu_s: 0.0,
            quality_limitation_bandwidth_s: 0.0,
            quality_limitation_other_s: 0.0,
            quality_limitation_none_s: 0.0,
            quality_limitation_resolution_changes: 0,
            encoder_implementation: encoder.to_owned(),
            power_efficient_encoder: false,
            malformed_bitstream: false,
            codec_mime_type: mime.map(str::to_owned),
        }
    }

    fn scratch(name: &str) -> PathBuf {
        let dir = std::env::temp_dir()
            .join(format!("teleop-run-json-{}-{name}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        dir.join("hosta").join("run.json")
    }

    fn read(path: &Path) -> serde_json::Value {
        serde_json::from_slice(&std::fs::read(path).expect("run.json exists")).expect("json")
    }

    /// The publish-time file must exist with an explicit null, so a reader can tell
    /// "not yet known" from "field missing".
    #[test]
    fn publish_time_file_has_null_encoder() {
        let path = scratch("start");
        let writer = RunJsonWriter::start(path.clone(), record());
        assert!(writer.is_pending());
        let v = read(&path);
        assert!(v["encoder_implementation"].is_null());
        assert_eq!(v["max_bitrate_bps"], 512_000);
        assert_eq!(v["codec"], "h264");
        assert!(!path.with_extension("json.tmp").exists());
        let _ = std::fs::remove_dir_all(path.parent().unwrap().parent().unwrap());
    }

    /// The first reading naming the encoder rewrites the file with the negotiated
    /// geometry and codec; later readings do not touch it.
    #[test]
    fn first_named_encoder_completes_the_record_once() {
        let path = scratch("observe");
        let mut writer = RunJsonWriter::start(path.clone(), record());

        writer.observe(&outbound("", 640, 480, Some("video/H264")));
        assert!(writer.is_pending(), "an unnamed encoder must not complete the record");

        writer.observe(&outbound("NvEnc", 1008, 800, Some("video/AV1")));
        assert!(!writer.is_pending());
        let v = read(&path);
        assert_eq!(v["encoder_implementation"], "NvEnc");
        assert_eq!((v["width"].as_u64(), v["height"].as_u64()), (Some(1008), Some(800)));
        assert_eq!(v["codec"], "av1");
        assert_eq!(v["started_at"], 1_790_384_410.25);

        writer.observe(&outbound("libvpx", 320, 240, Some("video/VP8")));
        assert_eq!(read(&path)["encoder_implementation"], "NvEnc");
        let _ = std::fs::remove_dir_all(path.parent().unwrap().parent().unwrap());
    }

    /// A reading with the encoder named but no frame encoded yet reports 0x0; that must
    /// not overwrite the publish-time geometry.
    #[test]
    fn zero_geometry_keeps_the_publish_time_values() {
        let path = scratch("zero");
        let mut writer = RunJsonWriter::start(path.clone(), record());
        writer.observe(&outbound("OpenH264", 0, 0, None));
        let v = read(&path);
        assert_eq!((v["width"].as_u64(), v["height"].as_u64()), (Some(1008), Some(816)));
        assert_eq!(v["codec"], "h264");
        let _ = std::fs::remove_dir_all(path.parent().unwrap().parent().unwrap());
    }
}
