//! Per-frame pipeline stage timing, in the `local_video` CSV format.
//!
//! The 1 Hz snapshot path answers "how did this cell behave"; this module answers "where
//! did a frame's latency go". They are complementary and both are written: the snapshots
//! carry the scored metrics and the validity gates, while the CSVs here feed
//! `examples/local_video/scripts/generate_frame_report.py` to produce a per-cell PDF.
//!
//! Every timestamp recorded here is emitted by WebRTC itself and delivered over the SDK's
//! [`publish_timing_events`] / [`subscribe_timing_events`] streams, rather than read from
//! a wall clock at the application layer. That is the whole point: an application-level
//! read of "when did encoding finish" measures when the harness's task was next scheduled,
//! which under the load the matrix deliberately creates is exactly when the measurement is
//! least trustworthy.
//!
//! The column set matches the example's writers byte for byte, because the report script
//! is shared. Columns the harness cannot fill are written empty rather than renamed or
//! dropped, which is what [`CsvOption`] is for: a reader that understands one file
//! understands the other.
//!
//! # Clock domains
//!
//! Publisher stages and subscriber stages are stamped on whichever host ran them. Within
//! one host every comparison is exact. Across hosts the difference contains the clock
//! offset between them, so the transport figure — packetize on the sender to first packet
//! on the receiver — is the one derived value that is only as good as clock sync. See
//! [`TransportSkew`] for how that is bounded and reported rather than assumed away.
//!
//! [`publish_timing_events`]: livekit::prelude::LocalVideoTrack::publish_timing_events
//! [`subscribe_timing_events`]: livekit::prelude::RemoteVideoTrack::subscribe_timing_events

use std::collections::{HashMap, VecDeque};
use std::fmt;
use std::fs::File;
use std::io::{self, BufWriter, Write};
use std::path::Path;

use livekit::track::{
    PublishTimingEvent, PublishTimingStage, SubscribeTimingEvent, SubscribeTimingStage,
};

/// Publisher CSV header, matching `examples/local_video/src/publisher.rs`.
const PUBLISHER_CSV_HEADER: &str = "sample,elapsed_ms,frame_id,capture_timestamp_us,\
frame_buffer_timestamp_us,encoder_upload_timestamp_us,encoder_output_timestamp_us,\
webrtc_packetize_timestamp_us,capture_to_buffer_ms,buffer_to_encoder_ms,encode_ms,\
encoder_to_packetize_ms,capture_to_packetize_ms,frame_id_gap,packetize_interval_ms";

/// Subscriber CSV header, matching `examples/local_video/src/subscriber_timing.rs` for
/// every stage the harness can observe.
///
/// The example's terminal stages are render-side — `frame_selected` through
/// `frame_gpu_complete` — because it draws to a window. The harness has no display, so its
/// pipeline ends at delivery to the application sink and those columns are absent rather
/// than fabricated: a GPU-completion time invented by a process that never drew anything
/// would be a worse answer than no answer.
///
/// The terminal columns are therefore named `e2e_latency_ms` and `render_interval_ms`,
/// which are the generic alternatives `generate_frame_report.py` already accepts
/// (`first_available_column`). One script reads both files and each names its own real
/// boundary, which is what keeps the PDF's end-to-end figure honest about where it stops.
const SUBSCRIBER_CSV_HEADER: &str = "sample,elapsed_ms,frame_id,capture_timestamp_us,\
webrtc_receive_timestamp_us,decoder_upload_timestamp_us,decoder_output_timestamp_us,\
frame_sink_timestamp_us,exposure_to_receive_ms,receive_and_assembly_ms,decode_ms,\
receive_to_sink_ms,e2e_latency_ms,frame_id_gap,render_interval_ms";

/// How many in-flight frames a side tracks before evicting the oldest.
///
/// A frame is complete once its terminal stage arrives, so the map only holds frames still
/// mid-pipeline. The bound exists so that a stage that stops arriving entirely — a decoder
/// that never reports, say — cannot grow the map for the length of the run.
const MAX_PENDING_FRAMES: usize = 600;

/// Displays an optional CSV cell as an empty field when absent.
///
/// Mirrors `frame_log::CsvOption` in the example. A missing stage is written as nothing at
/// all rather than as `0` or `NaN`, so the report script's `number()` returns `None` and
/// the stage is excluded from its statistics instead of biasing them toward zero.
struct CsvOption<T>(Option<T>);

impl<T: fmt::Display> fmt::Display for CsvOption<T> {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match &self.0 {
            Some(v) => v.fmt(f),
            None => Ok(()),
        }
    }
}

/// Displays a millisecond delta between two microsecond timestamps.
///
/// Absent when either endpoint is missing, and absent when the pair is out of order rather
/// than negative: within one clock domain an inverted pair is not a small negative latency
/// but evidence that the two stamps did not come from the same frame, and averaging it in
/// would quietly pull the stage mean toward zero.
struct CsvLatency(Option<u64>);

impl CsvLatency {
    fn between(start_us: Option<u64>, end_us: Option<u64>) -> Self {
        Self(match (start_us, end_us) {
            (Some(s), Some(e)) => e.checked_sub(s),
            _ => None,
        })
    }
}

impl fmt::Display for CsvLatency {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self.0 {
            Some(us) => write!(f, "{:.3}", us as f64 / 1_000.0),
            None => Ok(()),
        }
    }
}

/// One frame's publisher-side stage timestamps, in Unix microseconds.
#[derive(Clone, Copy, Debug, Default)]
struct PublisherFrame {
    frame_id: Option<u32>,
    capture_timestamp_us: u64,
    frame_buffer_timestamp_us: Option<u64>,
    encoder_upload_timestamp_us: Option<u64>,
    encoder_output_timestamp_us: Option<u64>,
    webrtc_packetize_timestamp_us: Option<u64>,
}

impl PublisherFrame {
    /// Whether the terminal publish stage has been observed.
    fn is_complete(&self) -> bool {
        self.webrtc_packetize_timestamp_us.is_some()
    }
}

/// One frame's subscriber-side stage timestamps, in Unix microseconds.
#[derive(Clone, Copy, Debug, Default)]
struct SubscriberFrame {
    frame_id: Option<u32>,
    capture_timestamp_us: u64,
    webrtc_receive_timestamp_us: Option<u64>,
    decoder_upload_timestamp_us: Option<u64>,
    decoder_output_timestamp_us: Option<u64>,
    frame_sink_timestamp_us: Option<u64>,
}

/// Running count of transport samples that came out negative, and why that matters.
///
/// Publisher packetize and subscriber receive are stamped on different hosts whenever the
/// two roles run on different machines. If the receiver's clock lags the sender's, every
/// transport sample is negative by roughly the offset. Silently dropping those — which is
/// what filtering on `>= 0` amounts to — leaves a plausible-looking row computed from
/// whichever samples happened to survive, which is the confident-wrong-answer path the
/// measurement design exists to close.
///
/// So they are counted instead. A run whose negative fraction exceeds
/// [`TransportSkew::INVALID_NEGATIVE_PCT`] has a clock problem large enough that its
/// transport column means nothing, and the run record says so.
#[derive(Clone, Copy, Debug, Default)]
pub struct TransportSkew {
    /// Paired frames where both endpoints were present.
    pub paired: u64,
    /// Of those, how many had receive earlier than packetize.
    pub negative: u64,
}

impl TransportSkew {
    /// Negative fraction above which the transport column is not reportable.
    ///
    /// Non-zero rather than zero: a handful of inverted samples is consistent with
    /// ordinary sub-millisecond clock jitter on a shared host, whereas a systematic offset
    /// inverts essentially every sample. The gap between those two cases is wide, so the
    /// exact threshold is not delicate.
    pub const INVALID_NEGATIVE_PCT: f64 = 1.0;

    /// Percentage of paired samples that were negative, or `None` with no pairs.
    pub fn negative_pct(&self) -> Option<f64> {
        (self.paired > 0).then(|| self.negative as f64 / self.paired as f64 * 100.0)
    }

    /// Whether clock skew invalidates the transport figure for this run.
    pub fn is_invalid(&self) -> bool {
        self.negative_pct().is_some_and(|pct| pct > Self::INVALID_NEGATIVE_PCT)
    }
}

/// Collects publisher-side stage events and writes one CSV row per packetized frame.
///
/// Rows are written when the terminal stage arrives rather than on a timer, so a row is
/// never half-populated. Frames whose terminal stage never arrives are evicted without a
/// row: the frame did not reach the wire, and the receive side's frame-ID gap is where
/// that shows up.
pub struct PublisherFrameLog {
    writer: BufWriter<File>,
    origin_us: u64,
    pending: HashMap<u64, PublisherFrame>,
    order: VecDeque<u64>,
    sample: u64,
    previous_frame_id: Option<u32>,
    last_packetize_us: Option<u64>,
}

impl PublisherFrameLog {
    /// Creates a publisher CSV at `path`, timing rows relative to `origin_us`.
    pub fn create(path: &Path, origin_us: u64) -> io::Result<Self> {
        Ok(Self {
            writer: create_csv(path, PUBLISHER_CSV_HEADER)?,
            origin_us,
            pending: HashMap::new(),
            order: VecDeque::new(),
            sample: 0,
            previous_frame_id: None,
            last_packetize_us: None,
        })
    }

    /// Records the moment the harness handed a frame to WebRTC.
    ///
    /// This is the one publisher stage the SDK does not emit, because it happens before
    /// WebRTC sees the frame. It is the boundary between "the harness was slow" and "the
    /// encoder was slow", which is worth separating when the matrix is deliberately
    /// loading the host.
    pub fn record_capture(&mut self, capture_timestamp_us: u64, frame_id: Option<u32>) {
        let entry = self.entry(capture_timestamp_us);
        entry.frame_id = frame_id;
        entry.capture_timestamp_us = capture_timestamp_us;
        entry.frame_buffer_timestamp_us = Some(capture_timestamp_us);
    }

    /// Records one SDK publish-timing event, writing a row when the frame completes.
    pub fn record_event(&mut self, event: PublishTimingEvent) -> io::Result<()> {
        let key = event.capture_timestamp_us;
        let entry = self.entry(key);
        entry.capture_timestamp_us = key;
        if entry.frame_id.is_none() {
            entry.frame_id = event.frame_id;
        }
        match event.stage {
            PublishTimingStage::EncoderUpload => {
                entry.encoder_upload_timestamp_us = Some(event.timestamp_us)
            }
            PublishTimingStage::EncoderOutput => {
                entry.encoder_output_timestamp_us = Some(event.timestamp_us)
            }
            PublishTimingStage::WebrtcPacketize => {
                entry.webrtc_packetize_timestamp_us = Some(event.timestamp_us)
            }
        }

        if entry.is_complete() {
            let frame = *entry;
            self.remove(key);
            self.write_row(frame)?;
        }
        Ok(())
    }

    /// Flushes buffered rows to disk.
    pub fn flush(&mut self) -> io::Result<()> {
        self.writer.flush()
    }

    fn entry(&mut self, key: u64) -> &mut PublisherFrame {
        if !self.pending.contains_key(&key) {
            self.order.push_back(key);
            self.evict_overflow();
        }
        self.pending.entry(key).or_default()
    }

    fn remove(&mut self, key: u64) {
        self.pending.remove(&key);
        if let Some(pos) = self.order.iter().position(|k| *k == key) {
            self.order.remove(pos);
        }
    }

    fn evict_overflow(&mut self) {
        while self.order.len() > MAX_PENDING_FRAMES {
            if let Some(oldest) = self.order.pop_front() {
                self.pending.remove(&oldest);
            }
        }
    }

    fn write_row(&mut self, frame: PublisherFrame) -> io::Result<()> {
        self.sample += 1;
        let capture = frame.capture_timestamp_us;
        let packetize = frame.webrtc_packetize_timestamp_us;

        let frame_id_gap =
            frame.frame_id.zip(self.previous_frame_id).and_then(|(id, prev)| id.checked_sub(prev));
        let packetize_interval =
            CsvLatency::between(self.last_packetize_us, packetize).0.map(|us| us as f64 / 1_000.0);

        writeln!(
            self.writer,
            "{},{:.3},{},{},{},{},{},{},{},{},{},{},{},{},{}",
            self.sample,
            elapsed_ms(self.origin_us, capture),
            CsvOption(frame.frame_id),
            capture,
            CsvOption(frame.frame_buffer_timestamp_us),
            CsvOption(frame.encoder_upload_timestamp_us),
            CsvOption(frame.encoder_output_timestamp_us),
            CsvOption(packetize),
            CsvLatency::between(Some(capture), frame.frame_buffer_timestamp_us),
            CsvLatency::between(frame.frame_buffer_timestamp_us, frame.encoder_upload_timestamp_us),
            CsvLatency::between(
                frame.encoder_upload_timestamp_us,
                frame.encoder_output_timestamp_us
            ),
            CsvLatency::between(frame.encoder_output_timestamp_us, packetize),
            CsvLatency::between(Some(capture), packetize),
            CsvOption(frame_id_gap),
            CsvOption(packetize_interval.map(|ms| format!("{ms:.3}"))),
        )?;

        if frame.frame_id.is_some() {
            self.previous_frame_id = frame.frame_id;
        }
        self.last_packetize_us = packetize;
        Ok(())
    }
}

/// Collects subscriber-side stage events and writes one CSV row per delivered frame.
///
/// The terminal stage is delivery to the application sink, which the harness observes
/// directly on the decoded-frame stream; the three stages before it arrive over the SDK
/// event stream. A row is written when the sink observes the frame, so a frame that
/// decoded but was never delivered produces no row.
pub struct SubscriberFrameLog {
    writer: BufWriter<File>,
    origin_us: u64,
    pending: HashMap<u64, SubscriberFrame>,
    order: VecDeque<u64>,
    sample: u64,
    previous_frame_id: Option<u32>,
    last_sink_us: Option<u64>,
}

impl SubscriberFrameLog {
    /// Creates a subscriber CSV at `path`, timing rows relative to `origin_us`.
    pub fn create(path: &Path, origin_us: u64) -> io::Result<Self> {
        Ok(Self {
            writer: create_csv(path, SUBSCRIBER_CSV_HEADER)?,
            origin_us,
            pending: HashMap::new(),
            order: VecDeque::new(),
            sample: 0,
            previous_frame_id: None,
            last_sink_us: None,
        })
    }

    /// Records one SDK subscribe-timing event.
    pub fn record_event(&mut self, event: SubscribeTimingEvent) {
        let key = event.capture_timestamp_us;
        let entry = self.entry(key);
        entry.capture_timestamp_us = key;
        if entry.frame_id.is_none() {
            entry.frame_id = event.frame_id;
        }
        match event.stage {
            SubscribeTimingStage::WebrtcReceive => {
                entry.webrtc_receive_timestamp_us = Some(event.timestamp_us)
            }
            SubscribeTimingStage::DecoderUpload => {
                entry.decoder_upload_timestamp_us = Some(event.timestamp_us)
            }
            SubscribeTimingStage::DecoderOutput => {
                entry.decoder_output_timestamp_us = Some(event.timestamp_us)
            }
        }
    }

    /// Records delivery to the application sink and writes the frame's row.
    ///
    /// Called from the decoded-frame loop, which is the only place that observes delivery.
    /// A frame with no prior stage events still produces a row: the stage columns are
    /// empty, and that a frame arrived with no timing is itself the finding.
    pub fn record_sink(
        &mut self,
        capture_timestamp_us: u64,
        frame_id: Option<u32>,
        sink_timestamp_us: u64,
    ) -> io::Result<()> {
        let entry = self.entry(capture_timestamp_us);
        entry.capture_timestamp_us = capture_timestamp_us;
        if entry.frame_id.is_none() {
            entry.frame_id = frame_id;
        }
        entry.frame_sink_timestamp_us = Some(sink_timestamp_us);
        let frame = *entry;
        self.remove(capture_timestamp_us);
        self.write_row(frame)
    }

    /// Flushes buffered rows to disk.
    pub fn flush(&mut self) -> io::Result<()> {
        self.writer.flush()
    }

    fn entry(&mut self, key: u64) -> &mut SubscriberFrame {
        if !self.pending.contains_key(&key) {
            self.order.push_back(key);
            self.evict_overflow();
        }
        self.pending.entry(key).or_default()
    }

    fn remove(&mut self, key: u64) {
        self.pending.remove(&key);
        if let Some(pos) = self.order.iter().position(|k| *k == key) {
            self.order.remove(pos);
        }
    }

    fn evict_overflow(&mut self) {
        while self.order.len() > MAX_PENDING_FRAMES {
            if let Some(oldest) = self.order.pop_front() {
                self.pending.remove(&oldest);
            }
        }
    }

    fn write_row(&mut self, frame: SubscriberFrame) -> io::Result<()> {
        self.sample += 1;
        let capture = frame.capture_timestamp_us;
        let sink = frame.frame_sink_timestamp_us;

        let frame_id_gap =
            frame.frame_id.zip(self.previous_frame_id).and_then(|(id, prev)| id.checked_sub(prev));
        let sink_interval =
            CsvLatency::between(self.last_sink_us, sink).0.map(|us| us as f64 / 1_000.0);

        writeln!(
            self.writer,
            "{},{:.3},{},{},{},{},{},{},{},{},{},{},{},{},{}",
            self.sample,
            elapsed_ms(self.origin_us, capture),
            CsvOption(frame.frame_id),
            capture,
            CsvOption(frame.webrtc_receive_timestamp_us),
            CsvOption(frame.decoder_upload_timestamp_us),
            CsvOption(frame.decoder_output_timestamp_us),
            CsvOption(sink),
            CsvLatency::between(Some(capture), frame.webrtc_receive_timestamp_us),
            CsvLatency::between(
                frame.webrtc_receive_timestamp_us,
                frame.decoder_upload_timestamp_us
            ),
            CsvLatency::between(
                frame.decoder_upload_timestamp_us,
                frame.decoder_output_timestamp_us
            ),
            CsvLatency::between(frame.webrtc_receive_timestamp_us, sink),
            CsvLatency::between(Some(capture), sink),
            CsvOption(frame_id_gap),
            CsvOption(sink_interval.map(|ms| format!("{ms:.3}"))),
        )?;

        if frame.frame_id.is_some() {
            self.previous_frame_id = frame.frame_id;
        }
        self.last_sink_us = sink;
        Ok(())
    }
}

/// Milliseconds from the run origin, clamped at zero.
///
/// A frame captured microseconds before the origin was stamped is not a negative elapsed
/// time; it is the same instant seen from two sides of a boundary.
fn elapsed_ms(origin_us: u64, timestamp_us: u64) -> f64 {
    timestamp_us.saturating_sub(origin_us) as f64 / 1_000.0
}

/// Creates a CSV file and its parent directories, writing `header` as the first line.
fn create_csv(path: &Path, header: &str) -> io::Result<BufWriter<File>> {
    if let Some(parent) = path.parent().filter(|p| !p.as_os_str().is_empty()) {
        std::fs::create_dir_all(parent)?;
    }
    let mut writer = BufWriter::new(File::create(path)?);
    writeln!(writer, "{header}")?;
    writer.flush()?;
    Ok(writer)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn publish_event(
        stage: PublishTimingStage,
        capture_timestamp_us: u64,
        timestamp_us: u64,
        frame_id: Option<u32>,
    ) -> PublishTimingEvent {
        PublishTimingEvent { stage, timestamp_us, capture_timestamp_us, frame_id }
    }

    fn subscribe_event(
        stage: SubscribeTimingStage,
        capture_timestamp_us: u64,
        timestamp_us: u64,
        frame_id: Option<u32>,
    ) -> SubscribeTimingEvent {
        SubscribeTimingEvent { stage, timestamp_us, capture_timestamp_us, frame_id }
    }

    fn read(path: &Path) -> Vec<String> {
        std::fs::read_to_string(path)
            .expect("csv should exist")
            .lines()
            .map(str::to_owned)
            .collect()
    }

    #[test]
    fn publisher_writes_a_row_once_packetize_arrives() {
        let dir = std::env::temp_dir().join(format!("ftl-pub-{}", std::process::id()));
        let path = dir.join("publisher.csv");
        let mut log = PublisherFrameLog::create(&path, 1_000).expect("create");

        log.record_capture(1_000, Some(7));
        log.record_event(publish_event(PublishTimingStage::EncoderUpload, 1_000, 1_200, Some(7)))
            .expect("event");
        log.record_event(publish_event(PublishTimingStage::EncoderOutput, 1_000, 1_500, Some(7)))
            .expect("event");
        // No row until the terminal stage lands.
        log.flush().expect("flush");
        assert_eq!(read(&path).len(), 1, "header only");

        log.record_event(publish_event(PublishTimingStage::WebrtcPacketize, 1_000, 1_900, Some(7)))
            .expect("event");
        log.flush().expect("flush");

        let lines = read(&path);
        assert_eq!(lines.len(), 2);
        let row = &lines[1];
        // encode_ms = (1_500 - 1_200) / 1000, capture_to_packetize_ms = 900us.
        assert!(row.contains("0.300"), "encode stage: {row}");
        assert!(row.contains("0.900"), "capture to packetize: {row}");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn subscriber_row_is_written_on_sink_delivery() {
        let dir = std::env::temp_dir().join(format!("ftl-sub-{}", std::process::id()));
        let path = dir.join("subscriber.csv");
        let mut log = SubscriberFrameLog::create(&path, 0).expect("create");

        log.record_event(subscribe_event(
            SubscribeTimingStage::WebrtcReceive,
            500,
            40_000,
            Some(3),
        ));
        log.record_event(subscribe_event(
            SubscribeTimingStage::DecoderUpload,
            500,
            52_000,
            Some(3),
        ));
        log.record_event(subscribe_event(
            SubscribeTimingStage::DecoderOutput,
            500,
            53_000,
            Some(3),
        ));
        log.flush().expect("flush");
        assert_eq!(read(&path).len(), 1, "no row before delivery");

        log.record_sink(500, Some(3), 55_000).expect("sink");
        log.flush().expect("flush");

        let lines = read(&path);
        assert_eq!(lines.len(), 2);
        let row = &lines[1];
        // receive_and_assembly = 12ms, decode = 1ms.
        assert!(row.contains("12.000"), "assembly: {row}");
        assert!(row.contains("1.000"), "decode: {row}");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn a_frame_with_no_stage_events_still_produces_a_row() {
        let dir = std::env::temp_dir().join(format!("ftl-bare-{}", std::process::id()));
        let path = dir.join("subscriber.csv");
        let mut log = SubscriberFrameLog::create(&path, 0).expect("create");

        log.record_sink(900, None, 4_000).expect("sink");
        log.flush().expect("flush");

        let lines = read(&path);
        assert_eq!(lines.len(), 2);
        // Empty stage cells, not zeros: the report script must exclude them.
        assert!(lines[1].contains(",,"), "absent stages are empty: {}", lines[1]);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn missing_endpoints_yield_empty_latency_cells() {
        assert_eq!(CsvLatency::between(None, Some(5)).to_string(), "");
        assert_eq!(CsvLatency::between(Some(5), None).to_string(), "");
        // Out of order within one clock domain is not a negative latency.
        assert_eq!(CsvLatency::between(Some(9), Some(4)).to_string(), "");
        assert_eq!(CsvLatency::between(Some(1_000), Some(2_500)).to_string(), "1.500");
    }

    #[test]
    fn transport_skew_flags_a_systematic_offset_but_tolerates_jitter() {
        // A couple of inverted samples out of hundreds is clock jitter, not skew.
        let jitter = TransportSkew { paired: 500, negative: 2 };
        assert!(!jitter.is_invalid());

        // A lagging receiver clock inverts essentially everything.
        let skewed = TransportSkew { paired: 500, negative: 480 };
        assert!(skewed.is_invalid());
        assert_eq!(skewed.negative_pct(), Some(96.0));

        // No pairs is not a skew verdict, it is an absence of evidence.
        assert_eq!(TransportSkew::default().negative_pct(), None);
        assert!(!TransportSkew::default().is_invalid());
    }

    #[test]
    fn pending_frames_are_bounded() {
        let dir = std::env::temp_dir().join(format!("ftl-bound-{}", std::process::id()));
        let path = dir.join("publisher.csv");
        let mut log = PublisherFrameLog::create(&path, 0).expect("create");

        // Frames that never reach packetize must not accumulate for the whole run.
        for i in 0..(MAX_PENDING_FRAMES as u64 * 2) {
            log.record_capture(i, Some(i as u32));
        }
        assert!(log.pending.len() <= MAX_PENDING_FRAMES);
        assert!(log.order.len() <= MAX_PENDING_FRAMES);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn elapsed_never_goes_negative() {
        assert_eq!(elapsed_ms(1_000, 500), 0.0);
        assert_eq!(elapsed_ms(1_000, 2_000), 1.0);
    }
}
