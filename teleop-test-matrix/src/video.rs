//! Synthetic video generation and glass-to-glass accounting.
//!
//! The matrix needs a video source that is identical on every host and every run: a
//! camera would make bitrate depend on what the lens happened to see, and bitrate at a
//! fixed profile is the one claim that transfers across encoder tiers. The generated
//! pattern moves every frame so the encoder cannot collapse it to a static image, and its
//! motion is deterministic so two runs at the same settings present the same encoding
//! problem.
//!
//! A real camera is available through `--camera-source` (see [`crate::camera`]) and stays
//! deliberately outside that argument: it is an opt-in realism spot-check, never a swept
//! axis and never a cell default. Because a camera run and a synthetic run are different
//! experiments, `camera_source` is a non-poolable dimension in `matrix.yaml` and the two
//! are never aggregated. Both sources feed the same capture loop below, so the only
//! difference between them anywhere in the harness is the pixels.

use std::sync::Arc;
use std::time::Duration;

use livekit::webrtc::video_frame::{FrameMetadata, I420Buffer, VideoFrame, VideoRotation};
use livekit::webrtc::video_source::native::NativeVideoSource;

use crate::camera::CameraFrameSource;
use crate::clock::RunClock;
use crate::rtsp::RtspFrameSource;

/// A deterministic moving test pattern in I420.
///
/// Luma carries a diagonal gradient scrolling at a fixed rate plus a moving block, which
/// together give the encoder both low-frequency and high-frequency content to work on.
/// Chroma cycles slowly so colour conversion is exercised without dominating the bitrate.
pub struct SyntheticFrameSource {
    width: u32,
    height: u32,
    frame_index: u64,
}

impl SyntheticFrameSource {
    /// Creates a source producing frames of the given size.
    ///
    /// Dimensions are rounded up to even values because I420 subsamples chroma by two in
    /// each direction and an odd dimension has no valid chroma plane.
    pub fn new(width: u32, height: u32) -> Self {
        Self { width: round_up_even(width), height: round_up_even(height), frame_index: 0 }
    }

    /// Width after even-rounding.
    pub fn width(&self) -> u32 {
        self.width
    }

    /// Height after even-rounding.
    pub fn height(&self) -> u32 {
        self.height
    }

    /// Renders the next frame into a fresh I420 buffer.
    pub fn next_buffer(&mut self) -> I420Buffer {
        let mut buffer = I420Buffer::new(self.width, self.height);
        let index = self.frame_index;
        self.frame_index = self.frame_index.wrapping_add(1);

        let (stride_y, stride_u, stride_v) = buffer.strides();
        let (width, height) = (self.width as usize, self.height as usize);
        let (data_y, data_u, data_v) = buffer.data_mut();

        let scroll = (index * 3) as usize;
        // Block position advances on a coprime-ish stride so the path does not repeat
        // over a short cycle.
        let block_x = ((index * 7) as usize % width.max(1)) as usize;
        let block_y = ((index * 5) as usize % height.max(1)) as usize;
        let block_w = (width / 8).max(1);
        let block_h = (height / 8).max(1);

        for y in 0..height {
            let row = &mut data_y[y * stride_y as usize..y * stride_y as usize + width];
            let in_block_rows = y >= block_y && y < block_y + block_h;
            for (x, pixel) in row.iter_mut().enumerate() {
                let gradient = ((x + y + scroll) % 256) as u8;
                *pixel = if in_block_rows && x >= block_x && x < block_x + block_w {
                    235
                } else {
                    gradient
                };
            }
        }

        let chroma_width = width / 2;
        let chroma_height = height / 2;
        let u_value = (index % 256) as u8;
        let v_value = (255 - (index % 256)) as u8;
        for y in 0..chroma_height {
            let u_row = &mut data_u[y * stride_u as usize..y * stride_u as usize + chroma_width];
            u_row.fill(u_value);
            let v_row = &mut data_v[y * stride_v as usize..y * stride_v as usize + chroma_width];
            v_row.fill(v_value);
        }

        buffer
    }
}

/// Rounds a dimension up to the nearest even value, with a floor of two.
fn round_up_even(value: u32) -> u32 {
    let v = value.max(2);
    if v % 2 == 0 {
        v
    } else {
        v + 1
    }
}

/// The frame source feeding one run: the matrix default, or an opted-in camera.
///
/// The two are unified here rather than at the publish site so that everything after this
/// point — stamping, publishing, encoding, stats — is one code path. A camera run that
/// took a different path would not be comparable to a synthetic run even as a
/// spot-check, which is the only reason the camera exists.
pub enum FrameSource {
    /// The deterministic moving pattern. The default for every matrix cell.
    Synthetic(SyntheticFrameSource),
    /// A local capture device, requested explicitly.
    Camera(Box<CameraFrameSource>),
    /// An IP camera reached over RTSP, requested explicitly.
    Rtsp(Box<RtspFrameSource>),
}

impl FrameSource {
    /// Frame width this source produces, after even-rounding.
    pub fn width(&self) -> u32 {
        match self {
            Self::Synthetic(s) => s.width(),
            Self::Camera(c) => c.width(),
            Self::Rtsp(r) => r.width(),
        }
    }

    /// Frame height this source produces, after even-rounding.
    pub fn height(&self) -> u32 {
        match self {
            Self::Synthetic(s) => s.height(),
            Self::Camera(c) => c.height(),
            Self::Rtsp(r) => r.height(),
        }
    }

    /// The `camera_source` value for the run record.
    ///
    /// `test_pattern` for the synthetic source, matching the value `run_matrix.py`
    /// defaults to; the resolved device name for a local camera and a `rtsp:`-prefixed
    /// redacted URL for an IP camera, so a run whose bitrate depended on what a lens saw is
    /// self-identifying rather than merely flagged — and so the three are distinguishable
    /// from each other, since `camera_source` is a `never_pool_across` dimension.
    pub fn source_label(&self) -> String {
        match self {
            Self::Synthetic(_) => crate::cli::TEST_PATTERN_SOURCE.to_string(),
            Self::Camera(c) => c.identity().device_name.clone(),
            Self::Rtsp(r) => format!("rtsp:{}", r.identity().url),
        }
    }

    /// The resolved device or stream, for the run record. `None` for the synthetic pattern.
    ///
    /// Both camera kinds populate the same record field: they are the same claim about a
    /// run, and splitting them would give the analysis layer two shapes to handle for one
    /// `never_pool_across` dimension.
    pub fn camera_device(&self) -> Option<crate::snapshot::CameraDevice> {
        match self {
            Self::Synthetic(_) => None,
            Self::Camera(c) => Some(c.identity().into()),
            Self::Rtsp(r) => Some(r.identity().into()),
        }
    }

    /// Produces the next frame, or `None` when the source failed mid-run.
    ///
    /// A capture failure is logged and ends the loop rather than being retried: frames
    /// stop, the stats show it, and the run is not silently padded with stale content.
    ///
    /// Visible to the crate rather than to this module alone so [`crate::export`] can drive
    /// the same sources to a file. That sharing is the point: a VMAF comparison against a
    /// separately-generated pattern would be measuring some other video.
    pub(crate) fn next_buffer(&mut self) -> Option<I420Buffer> {
        match self {
            Self::Synthetic(s) => Some(s.next_buffer()),
            Self::Camera(c) => match c.next_buffer() {
                Ok(buffer) => Some(buffer),
                Err(e) => {
                    log::error!("camera capture stopped: {e}");
                    None
                }
            },
            Self::Rtsp(r) => match r.next_buffer() {
                Ok(buffer) => Some(buffer),
                Err(e) => {
                    log::error!("rtsp capture stopped: {e}");
                    None
                }
            },
        }
    }
}

/// Publishes frames at a fixed rate, stamping each one in band.
///
/// The capture timestamp and frame id travel with the encoded frame as packet-trailer
/// metadata rather than being echoed back over a side channel. That removes the echo-path
/// latency a side-channel scheme would fold into its own measurement, and it is why the
/// resulting figure decomposes cleanly against the encode, assembly, jitter-buffer and
/// decode terms from `RtcStats`.
pub struct VideoCaptureLoop {
    source: FrameSource,
    rtc_source: NativeVideoSource,
    clock: RunClock,
    interval: Duration,
    duration: Duration,
    attach_timestamp: bool,
    attach_frame_id: bool,
    frames_captured: Arc<std::sync::atomic::AtomicU64>,
    frame_log: Option<Arc<parking_lot::Mutex<crate::frame_timing::PublisherFrameLog>>>,
}

impl VideoCaptureLoop {
    /// Creates a capture loop for the given source and cadence.
    pub fn new(
        source: FrameSource,
        rtc_source: NativeVideoSource,
        clock: RunClock,
        interval: Duration,
        duration: Duration,
        attach_timestamp: bool,
        attach_frame_id: bool,
        frames_captured: Arc<std::sync::atomic::AtomicU64>,
    ) -> Self {
        Self {
            source,
            rtc_source,
            clock,
            interval,
            duration,
            attach_timestamp,
            attach_frame_id,
            frames_captured,
            frame_log: None,
        }
    }

    /// Attaches a per-frame publisher log, recording the hand-off to WebRTC.
    ///
    /// That hand-off is the one publish stage the SDK cannot emit, because it happens
    /// before WebRTC has the frame. Without it the first CSV column pair collapses and
    /// "the harness was slow to generate a frame" becomes indistinguishable from "the
    /// encoder was slow to take it".
    pub fn with_frame_log(
        mut self,
        log: Option<Arc<parking_lot::Mutex<crate::frame_timing::PublisherFrameLog>>>,
    ) -> Self {
        self.frame_log = log;
        self
    }

    /// Captures frames until the run duration elapses.
    pub async fn run(mut self) {
        let origin = self.clock.monotonic_origin();
        let mut frame_id: u32 = 0;

        while origin.elapsed() < self.duration {
            let tick = std::time::Instant::now();
            let Some(buffer) = self.source.next_buffer() else {
                break;
            };

            // Stamped as late as possible before handing the frame to WebRTC, so the
            // measured interval is transport and codec latency rather than the harness's
            // own frame-generation time.
            let capture_wall_us = self.clock.wall_us();
            let user_timestamp = self.attach_timestamp.then_some(capture_wall_us);
            let attached_id = self.attach_frame_id.then_some(frame_id);
            let metadata = (user_timestamp.is_some() || attached_id.is_some())
                .then(|| FrameMetadata { user_timestamp, frame_id: attached_id, user_data: None });

            let frame = VideoFrame {
                rotation: VideoRotation::VideoRotation0,
                timestamp_us: self.clock.monotonic_us() as i64,
                buffer,
                frame_metadata: metadata,
            };
            if let Some(log) = self.frame_log.as_ref() {
                log.lock().record_capture(capture_wall_us, attached_id);
            }
            self.rtc_source.capture_frame(&frame);
            self.frames_captured.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
            frame_id = frame_id.wrapping_add(1);

            let spent = tick.elapsed();
            if spent < self.interval {
                tokio::time::sleep(self.interval - spent).await;
            } else {
                // Generation overran the frame period. Yield so the runtime is not
                // starved, and let the frame rate fall — which the stats will show.
                tokio::task::yield_now().await;
            }
        }
    }
}

/// Receive-side glass-to-glass accounting, from in-band frame metadata.
///
/// The figure produced here is capture to application delivery. It excludes display and
/// compositor latency, so it is not camera-to-photons; the pixel measurement is a
/// separate manual procedure and the two are calibrated against each other once per
/// codec and encoder tier, not per run.
#[derive(Debug, Default)]
pub struct G2gTracker {
    latency_us_interval: Vec<i64>,
    frame_arrival_intervals_ms: Vec<f64>,
    last_arrival_us: Option<u64>,
    distinct_frame_ids: u64,
    min_frame_id: Option<u32>,
    max_frame_id: Option<u32>,
    frames_without_timestamp: u64,
    frames_seen: u64,
}

impl G2gTracker {
    /// Creates an empty tracker.
    pub fn new() -> Self {
        Self::default()
    }

    /// Records one delivered frame.
    ///
    /// `corrected_latency_us` is `None` while the clock offset estimate is invalid, in
    /// which case the frame still counts toward frame-loss accounting and arrival pacing
    /// but contributes no latency sample. Pacing is measured on a single clock and so
    /// stays valid even when the offset does not.
    pub fn on_frame(
        &mut self,
        arrival_wall_us: u64,
        user_timestamp_us: Option<u64>,
        frame_id: Option<u32>,
        corrected_latency_us: Option<i64>,
    ) {
        self.frames_seen += 1;

        if let Some(previous) = self.last_arrival_us {
            let spacing_us = arrival_wall_us.saturating_sub(previous);
            self.frame_arrival_intervals_ms.push(spacing_us as f64 / 1000.0);
        }
        self.last_arrival_us = Some(arrival_wall_us);

        if user_timestamp_us.is_none() {
            self.frames_without_timestamp += 1;
        }
        if let Some(latency) = corrected_latency_us {
            self.latency_us_interval.push(latency);
        }

        let Some(id) = frame_id else {
            return;
        };
        self.distinct_frame_ids += 1;
        self.min_frame_id = Some(self.min_frame_id.map_or(id, |m| m.min(id)));
        self.max_frame_id = Some(self.max_frame_id.map_or(id, |m| m.max(id)));
    }

    /// Frames delivered so far.
    pub fn frames_seen(&self) -> u64 {
        self.frames_seen
    }

    /// Span between the lowest and highest frame id seen, inclusive.
    ///
    /// Compared against the distinct count, this separates "latency is fine but half the
    /// frames vanished" from a genuine latency result.
    pub fn frame_id_span(&self) -> u64 {
        match (self.min_frame_id, self.max_frame_id) {
            (Some(min), Some(max)) => (max.wrapping_sub(min) as u64).saturating_add(1),
            _ => 0,
        }
    }

    /// Takes this interval's samples, resetting them.
    pub fn take_interval(&mut self) -> G2gInterval {
        G2gInterval {
            latency_us: std::mem::take(&mut self.latency_us_interval),
            frame_arrival_intervals_ms: std::mem::take(&mut self.frame_arrival_intervals_ms),
            distinct_frame_ids: self.distinct_frame_ids,
            frame_id_span: self.frame_id_span(),
            frames_without_timestamp: self.frames_without_timestamp,
        }
    }
}

/// One interval's worth of glass-to-glass observations.
#[derive(Debug, Default, Clone)]
pub struct G2gInterval {
    /// Corrected capture-to-delivery latencies, in microseconds.
    pub latency_us: Vec<i64>,
    /// Wall-clock spacing between delivered frames, in milliseconds. `RtcStats` gives a
    /// mean inter-frame delay but not a tail, and a freeze is a tail event.
    pub frame_arrival_intervals_ms: Vec<f64>,
    pub distinct_frame_ids: u64,
    pub frame_id_span: u64,
    pub frames_without_timestamp: u64,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn dimensions_are_rounded_to_even() {
        let source = SyntheticFrameSource::new(1919, 1079);
        assert_eq!(source.width(), 1920);
        assert_eq!(source.height(), 1080);
        assert_eq!(round_up_even(0), 2);
        assert_eq!(round_up_even(1920), 1920);
    }

    /// A static pattern would let the encoder collapse the stream to near-zero bitrate
    /// and every bitrate measurement would be meaningless.
    #[test]
    fn successive_frames_differ() {
        let mut source = SyntheticFrameSource::new(64, 64);
        let first = source.next_buffer();
        let second = source.next_buffer();
        let (first_y, _, _) = first.data();
        let (second_y, _, _) = second.data();
        assert_ne!(first_y, second_y);
    }

    /// Two runs at the same settings must present the encoder with the same problem, or
    /// bitrate is not comparable between them.
    #[test]
    fn generation_is_deterministic() {
        let mut a = SyntheticFrameSource::new(64, 64);
        let mut b = SyntheticFrameSource::new(64, 64);
        for _ in 0..5 {
            let (fa, fb) = (a.next_buffer(), b.next_buffer());
            assert_eq!(fa.data().0, fb.data().0);
            assert_eq!(fa.data().1, fb.data().1);
        }
    }

    #[test]
    fn g2g_collects_latency_samples() {
        let mut tracker = G2gTracker::new();
        tracker.on_frame(1_000_000, Some(950_000), Some(0), Some(50_000));
        tracker.on_frame(1_033_000, Some(983_000), Some(1), Some(50_000));
        let interval = tracker.take_interval();
        assert_eq!(interval.latency_us, vec![50_000, 50_000]);
        assert_eq!(interval.distinct_frame_ids, 2);
        assert_eq!(interval.frame_id_span, 2);
    }

    /// A frame with no usable clock correction still counts for pacing and frame-loss
    /// accounting; only the latency sample is withheld.
    #[test]
    fn frames_count_even_without_a_valid_clock_offset() {
        let mut tracker = G2gTracker::new();
        tracker.on_frame(1_000_000, Some(950_000), Some(0), None);
        tracker.on_frame(1_033_000, Some(983_000), Some(1), None);
        let interval = tracker.take_interval();
        assert!(interval.latency_us.is_empty());
        assert_eq!(interval.distinct_frame_ids, 2);
        assert_eq!(interval.frame_arrival_intervals_ms, vec![33.0]);
    }

    /// The span-versus-count comparison is what distinguishes a latency result from
    /// vanished frames: ids 0, 1, 8 span nine but only three arrived.
    #[test]
    fn frame_id_span_exposes_missing_frames() {
        let mut tracker = G2gTracker::new();
        for id in [0u32, 1, 8] {
            tracker.on_frame(1_000_000 + id as u64 * 33_000, Some(1), Some(id), Some(1));
        }
        let interval = tracker.take_interval();
        assert_eq!(interval.distinct_frame_ids, 3);
        assert_eq!(interval.frame_id_span, 9);
    }

    #[test]
    fn frames_without_timestamps_are_counted() {
        let mut tracker = G2gTracker::new();
        tracker.on_frame(1_000_000, None, Some(0), None);
        tracker.on_frame(1_033_000, Some(1), Some(1), Some(5));
        let interval = tracker.take_interval();
        assert_eq!(interval.frames_without_timestamp, 1);
    }

    #[test]
    fn arrival_intervals_need_two_frames() {
        let mut tracker = G2gTracker::new();
        tracker.on_frame(1_000_000, Some(1), Some(0), Some(1));
        assert!(tracker.take_interval().frame_arrival_intervals_ms.is_empty());
        tracker.on_frame(1_050_000, Some(1), Some(1), Some(1));
        assert_eq!(tracker.take_interval().frame_arrival_intervals_ms, vec![50.0]);
    }

    #[test]
    fn interval_samples_reset_but_cumulative_state_persists() {
        let mut tracker = G2gTracker::new();
        tracker.on_frame(1_000_000, Some(1), Some(0), Some(10));
        let first = tracker.take_interval();
        assert_eq!(first.latency_us.len(), 1);
        let second = tracker.take_interval();
        assert!(second.latency_us.is_empty());
        assert_eq!(second.distinct_frame_ids, 1);
        assert_eq!(tracker.frames_seen(), 1);
    }
}
