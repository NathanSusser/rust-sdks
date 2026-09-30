//! Receive side of the teleop control path, speaking `teleop-harness`'s wire format.
//!
//! The publisher (`teleop-harness` on Host A) sends a fixed 32-byte control sample on the
//! `teleop-control` data track, or on the data-channel topic of the same name. Each sample
//! is logged with its arrival time, and a sample carrying a probe token is echoed back on
//! the reliable data channel so the publisher can compute an offset-free round trip.

use std::{
    fs::File,
    io::{self, BufWriter, Write},
    path::Path,
    sync::Arc,
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};

use futures::StreamExt;
use livekit::prelude::*;
use log::{info, warn};
use parking_lot::Mutex;

/// Data track name and data-channel topic of the control stream.
pub(crate) const CONTROL_NAME: &str = "teleop-control";
/// Data-channel topic probe echoes travel on.
const PROBE_ECHO_TOPIC: &str = "teleop-probe-echo";
/// Wire size of a control sample and of a probe echo.
const SAMPLE_LEN: usize = 32;

const CSV_HEADER: &str = "seq,t_send_unix_us,t_recv_unix_us,owd_us,probe_token,transport\n";

/// Which path a control sample arrived on.
#[derive(Clone, Copy, Debug)]
pub(crate) enum Transport {
    DataTrack,
    DataChannel,
}

impl Transport {
    fn as_str(self) -> &'static str {
        match self {
            Self::DataTrack => "data_track",
            Self::DataChannel => "data_channel",
        }
    }
}

/// One decoded control sample (little-endian `seq, t_send_unix_us, probe_token, pad`).
struct Sample {
    seq: u64,
    t_send_unix_us: u64,
    probe_token: u64,
}

impl TryFrom<&[u8]> for Sample {
    type Error = usize;

    fn try_from(bytes: &[u8]) -> Result<Self, usize> {
        let buf: &[u8; SAMPLE_LEN] = bytes.try_into().map_err(|_| bytes.len())?;
        let word = |at: usize| {
            let mut w = [0u8; 8];
            w.copy_from_slice(&buf[at..at + 8]);
            u64::from_le_bytes(w)
        };
        Ok(Self { seq: word(0), t_send_unix_us: word(8), probe_token: word(16) })
    }
}

/// The four timestamps of a probe exchange, as `teleop-harness` expects them back.
fn encode_echo(token: u64, t0_us: u64, t1_us: u64, t2_us: u64) -> Vec<u8> {
    [token, t0_us, t1_us, t2_us].iter().flat_map(|v| v.to_le_bytes()).collect()
}

fn wall_us() -> u64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_micros() as u64).unwrap_or(0)
}

/// Buffered CSV of every control sample received, flushed at most once a second.
struct ControlLog {
    writer: BufWriter<File>,
    last_flush: Instant,
}

impl ControlLog {
    fn write(&mut self, sample: &Sample, t_recv_us: u64, transport: Transport) {
        let owd_us = t_recv_us as i64 - sample.t_send_unix_us as i64;
        let result = writeln!(
            self.writer,
            "{},{},{},{},{},{}",
            sample.seq,
            sample.t_send_unix_us,
            t_recv_us,
            owd_us,
            sample.probe_token,
            transport.as_str()
        );
        if let Err(e) = result {
            warn!("control log write failed: {e}");
        }
        if self.last_flush.elapsed() >= Duration::from_secs(1) {
            let _ = self.writer.flush();
            self.last_flush = Instant::now();
        }
    }
}

impl Drop for ControlLog {
    fn drop(&mut self) {
        let _ = self.writer.flush();
    }
}

/// Logs control samples and answers probes. Cheap to clone; clones share the log.
#[derive(Clone)]
pub(crate) struct ControlReceiver {
    room: Arc<Room>,
    log: Arc<Mutex<ControlLog>>,
    buffer_frames: usize,
}

impl ControlReceiver {
    /// Creates the receiver, truncating `path` and writing the CSV header.
    ///
    /// `buffer_frames` is the data track's receive queue depth; frames arriving while it is
    /// full are dropped by the SDK before this receiver sees them.
    pub(crate) fn create(room: Arc<Room>, path: &Path, buffer_frames: usize) -> io::Result<Self> {
        let mut writer = BufWriter::new(File::create(path)?);
        writer.write_all(CSV_HEADER.as_bytes())?;
        writer.flush()?;
        info!("Control path log: {}", path.display());
        let log = ControlLog { writer, last_flush: Instant::now() };
        Ok(Self { room, log: Arc::new(Mutex::new(log)), buffer_frames })
    }

    /// Subscribes to a remote `teleop-control` data track and consumes it until it ends.
    pub(crate) async fn run_data_track(self, track: RemoteDataTrack) {
        // A 32-byte sample never spans packets; more partial frames only add state.
        track
            .set_pipeline_options(RemoteDataTrackPipelineOptions::new().with_max_partial_frames(1));
        let options = DataTrackSubscribeOptions::new().with_buffer_size(self.buffer_frames);
        let mut stream = match track.subscribe_with_options(options).await {
            Ok(stream) => stream,
            Err(e) => {
                warn!("control data track subscribe failed: {e}");
                return;
            }
        };
        info!(
            "Subscribed to control data track from {} (buffer {} frames)",
            track.publisher_identity(),
            self.buffer_frames
        );
        while let Some(frame) = stream.next().await {
            self.on_payload(&frame.payload(), Transport::DataTrack).await;
        }
        info!("Control data track ended");
    }

    /// Logs one control payload and echoes it when it carries a probe token.
    pub(crate) async fn on_payload(&self, payload: &[u8], transport: Transport) {
        let t_recv_us = wall_us();
        let sample = match Sample::try_from(payload) {
            Ok(sample) => sample,
            Err(len) => {
                warn!("control payload of {len} bytes ignored (expected {SAMPLE_LEN})");
                return;
            }
        };
        self.log.lock().write(&sample, t_recv_us, transport);
        if sample.probe_token == 0 {
            return;
        }
        // Reliable channel regardless of the transport under test, as the harness does:
        // the four-timestamp form cancels echo-path delay, so only delivery matters here.
        let echo = encode_echo(sample.probe_token, sample.t_send_unix_us, t_recv_us, wall_us());
        let packet = DataPacket {
            payload: echo,
            topic: Some(PROBE_ECHO_TOPIC.to_owned()),
            reliable: true,
            destination_identities: Vec::new(),
        };
        if let Err(e) = self.room.local_participant().publish_data(packet).await {
            warn!("probe echo failed: {e}");
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn decodes_the_harness_wire_format() {
        let mut bytes = Vec::new();
        for v in [7u64, 1_790_000_000_000_000, 42, 0] {
            bytes.extend_from_slice(&v.to_le_bytes());
        }
        let sample = Sample::try_from(bytes.as_slice()).expect("32 bytes");
        assert_eq!(
            (sample.seq, sample.t_send_unix_us, sample.probe_token),
            (7, 1_790_000_000_000_000, 42)
        );
        assert_eq!(Sample::try_from(&bytes[..31]).err(), Some(31));
    }

    #[test]
    fn echo_is_four_le_words() {
        let echo = encode_echo(1, 2, 3, 4);
        assert_eq!(echo.len(), SAMPLE_LEN);
        assert_eq!(&echo[24..32], &4u64.to_le_bytes());
    }
}
