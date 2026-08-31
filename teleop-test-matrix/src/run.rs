//! Run orchestration: connect both ends, publish, subscribe, sample, write.

use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::Arc;
use std::time::Duration;

use futures_util::StreamExt;
use livekit::prelude::*;
use parking_lot::Mutex;
use tokio::sync::mpsc;

use crate::audio::AudioToneLoop;
use crate::camera::CameraFrameSource;
use crate::cli::{Args, VideoSourceSelector};
use crate::clock::RunClock;
use crate::control::payload::{ControlSample, ProbeEcho};
use crate::control::publisher::{ControlPublisher, PublisherCounters};
use crate::control::receiver::ControlReceiver;
use crate::control::transport::{self, ControlSender};
use crate::frame_timing::{PublisherFrameLog, SubscriberFrameLog};
use crate::probe::ProbeTracker;
use crate::sampler::StatsSampler;
use crate::session::{self, Credentials, SessionError};
use crate::video::{FrameSource, G2gTracker, SyntheticFrameSource, VideoCaptureLoop};
use crate::writer::JsonLinesWriter;

/// How long to wait for the subscriber to see the publisher's tracks before giving up.
const SUBSCRIPTION_TIMEOUT: Duration = Duration::from_secs(30);

/// How long to keep retrying the packet-trailer handler installation before giving up.
///
/// The handler can only be created once the track's transceiver is set, and on a full
/// reconnect the SDK publishes the track and sets the transceiver from a detached task, so
/// the first attempt can lose the race. A second of retries is far longer than that gap
/// while still bounded well inside the subscription timeout.
const TIMING_HANDLER_RETRY_LIMIT: Duration = Duration::from_secs(1);

/// Gap between packet-trailer handler installation attempts.
const TIMING_HANDLER_RETRY_INTERVAL: Duration = Duration::from_millis(20);

/// What a completed run produced.
#[derive(Debug, Clone, Copy)]
pub struct RunOutcome {
    pub snapshots_written: u64,
    pub seq_published: u64,
    pub distinct_seq_received: u64,
}

/// Why a run could not complete.
#[derive(Debug)]
pub enum RunError {
    /// The session could not be established or a track could not be published.
    Session(SessionError),
    /// An output file could not be written.
    Output(crate::writer::WriteError),
    /// The subscriber never saw the publisher's video track.
    NoSubscription,
    /// The session ended before the run duration elapsed.
    SessionLost(String),
    /// A camera was requested and could not be opened.
    ///
    /// Fatal, with no fallback to the synthetic pattern: a run recorded as `camera` that
    /// actually carried the pattern would be pooled with pattern runs and there would be
    /// nothing in the record to catch it.
    Camera(crate::camera::CameraError),
    /// An RTSP stream was requested and could not be started.
    ///
    /// Fatal for the same reason as [`Self::Camera`], and kept a separate variant because
    /// the two fail for entirely unrelated reasons — device enumeration versus a network
    /// path, credentials and an external decoder.
    Rtsp(crate::rtsp::RtspError),
}

impl std::fmt::Display for RunError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Session(e) => write!(f, "{e}"),
            Self::Output(e) => write!(f, "{e}"),
            Self::NoSubscription => {
                write!(f, "subscriber never received the published video track")
            }
            Self::SessionLost(reason) => write!(f, "session lost mid-run: {reason}"),
            Self::Camera(e) => write!(f, "{e}"),
            Self::Rtsp(e) => write!(f, "{e}"),
        }
    }
}

impl std::error::Error for RunError {}

impl RunError {
    /// Whether a retry of the same cell could plausibly succeed.
    ///
    /// True only when the session never established, which says nothing about the cell —
    /// a transient server-side or connectivity event, not a property of the codec or
    /// profile under test. Everything else is reported as a plain failure: a lost session
    /// or an absent subscription may be exactly the condition the suite is measuring, and
    /// retrying past it would discard the finding.
    pub fn is_retryable(&self) -> bool {
        matches!(self, Self::Session(e) if e.is_retryable())
    }
}

impl From<SessionError> for RunError {
    fn from(e: SessionError) -> Self {
        Self::Session(e)
    }
}

impl From<crate::writer::WriteError> for RunError {
    fn from(e: crate::writer::WriteError) -> Self {
        Self::Output(e)
    }
}

impl From<crate::camera::CameraError> for RunError {
    fn from(e: crate::camera::CameraError) -> Self {
        Self::Camera(e)
    }
}

impl From<crate::rtsp::RtspError> for RunError {
    fn from(e: crate::rtsp::RtspError) -> Self {
        Self::Rtsp(e)
    }
}

/// Shared receive-side state, written by the receive tasks and read by the sampler.
pub struct SharedState {
    pub control: Mutex<ControlReceiver>,
    pub probe: Mutex<ProbeTracker>,
    pub g2g: Mutex<G2gTracker>,
    pub session_lost: Mutex<Option<String>>,
    pub reconnect_count: AtomicU64,
    /// Frames handed to WebRTC by the capture loop.
    ///
    /// This is the send-side denominator that separates "the harness never generated the
    /// frame" from "the frame was lost in transit" — without it, a stalled capture loop
    /// and a lossy network look identical on the receive side.
    pub frames_captured: Arc<AtomicU64>,
    /// Whether every video subscription got its packet trailer handler installed.
    ///
    /// False means at least one subscription produced frames with no capture timestamp, so
    /// the glass-to-glass figures cover only part of the run. Recorded rather than inferred
    /// downstream: a silently un-timestamped subscription is indistinguishable from a run
    /// that simply had no video once the snapshots are all that is left.
    pub g2g_handler_installed: AtomicBool,
    /// How many times a remote video track was subscribed.
    ///
    /// More than one means the session re-subscribed mid-run, which is what a full
    /// reconnect does; the G2G series then spans two subscriptions.
    pub video_subscriptions: AtomicU64,
    /// Per-frame subscriber stage log, when `--frame-csv-out` was given.
    ///
    /// Shared between the timing-event task and the decoded-frame loop: the former fills
    /// the receive, decode-start and decode-finish stages, the latter closes the row on
    /// delivery. Both run per subscription, so the lock is held only for the duration of a
    /// single row write.
    pub subscriber_frames: Option<Mutex<SubscriberFrameLog>>,
}

impl crate::probe::ProbeHost for SharedState {
    fn tracker(&self) -> &Mutex<ProbeTracker> {
        &self.probe
    }
}

impl Default for SharedState {
    fn default() -> Self {
        Self {
            control: Mutex::default(),
            probe: Mutex::default(),
            g2g: Mutex::default(),
            session_lost: Mutex::default(),
            reconnect_count: AtomicU64::new(0),
            frames_captured: Arc::default(),
            // Folded with AND as each subscription reports in, so it must start true.
            g2g_handler_installed: AtomicBool::new(true),
            video_subscriptions: AtomicU64::new(0),
            subscriber_frames: None,
        }
    }
}

/// Builds the run's frame source, opening a camera when one was requested.
///
/// Enumeration, device open and process spawn are all synchronous and can block for
/// hundreds of milliseconds while a backend powers a sensor up or ffmpeg dials an RTSP
/// session, so they run on the blocking pool rather than stalling the runtime the rest of
/// the run shares.
async fn open_frame_source(args: &Args) -> Result<FrameSource, RunError> {
    let Some(selector) = args.video_source_selector() else {
        return Ok(FrameSource::Synthetic(SyntheticFrameSource::new(args.width, args.height)));
    };

    let source = match selector {
        VideoSourceSelector::Device(selector) => open_local_camera(args, selector).await?,
        VideoSourceSelector::Rtsp(selector) => open_rtsp_stream(args, selector).await?,
    };

    if source.width() != args.width || source.height() != args.height {
        // Not fatal: the source's geometry is what the encoder sees and what the record
        // carries. Worth a warning because a downgraded capture changes the encoding
        // problem, and a bitrate from it is not comparable to one taken at the request.
        log::warn!(
            "video source negotiated {}x{}, not the requested {}x{}; the run record carries the \
             negotiated geometry",
            source.width(),
            source.height(),
            args.width,
            args.height
        );
    }

    Ok(source)
}

/// Opens a local capture device on the blocking pool.
async fn open_local_camera(
    args: &Args,
    selector: crate::camera::CameraSelector,
) -> Result<FrameSource, RunError> {
    let (width, height, fps) = (args.width, args.height, args.fps);
    let opened =
        tokio::task::spawn_blocking(move || CameraFrameSource::open(&selector, width, height, fps))
            .await;

    match opened {
        Ok(result) => Ok(FrameSource::Camera(Box::new(result?))),
        // The blocking pool only drops a task when it panics or the runtime is shutting
        // down. Either way no camera was opened, and continuing on the pattern would
        // mislabel the run.
        Err(e) => Err(RunError::Camera(crate::camera::CameraError::Open {
            device: args.redacted_camera_source(),
            source: format!("capture task did not complete: {e}"),
        })),
    }
}

/// Starts the RTSP decoder subprocess on the blocking pool.
///
/// Note that this returning `Ok` only means ffmpeg started: an unreachable host, a rejected
/// password or a wrong stream path all surface on the first frame read instead, carrying
/// ffmpeg's own diagnosis. That is deliberate — waiting here for a first frame would put a
/// second, separate timeout in front of the one the capture loop already applies.
async fn open_rtsp_stream(
    args: &Args,
    selector: crate::rtsp::RtspSelector,
) -> Result<FrameSource, RunError> {
    let options = crate::rtsp::RtspOptions {
        width: args.width,
        height: args.height,
        fps: args.fps,
        transport: args.rtsp_transport,
        stall_timeout: args.rtsp_stall_timeout(),
    };

    let opened = tokio::task::spawn_blocking(move || {
        crate::rtsp::RtspFrameSource::open(&selector, &options)
    })
    .await;

    match opened {
        Ok(result) => Ok(FrameSource::Rtsp(Box::new(result?))),
        Err(e) => Err(RunError::Rtsp(crate::rtsp::RtspError::Pipe(format!(
            "rtsp open task did not complete: {e}"
        )))),
    }
}

/// Runs one cell of the matrix end to end.
pub async fn execute(args: Args) -> Result<RunOutcome, RunError> {
    let clock = RunClock::start();
    let credentials = Credentials::resolve(&args)?;
    let duration = Duration::from_secs(args.duration_s);

    session::reset_room(&credentials, &args.room_name).await;

    log::info!(
        "buffering_mode={} codec={} encoder={} source={} {}x{}@{}",
        args.buffering_mode.as_str(),
        args.codec.as_str(),
        args.encoder.as_str(),
        args.redacted_camera_source(),
        args.width,
        args.height,
        args.fps
    );

    let publisher_identity =
        session::identity_for(&args.room_name, session::PUBLISHER_IDENTITY_SUFFIX);
    let subscriber_identity =
        session::identity_for(&args.room_name, session::SUBSCRIBER_IDENTITY_SUFFIX);

    let (pub_room, pub_events) =
        session::connect(&credentials, &args.room_name, &publisher_identity).await?;
    let (sub_room, sub_events) =
        session::connect(&credentials, &args.room_name, &subscriber_identity).await?;

    // Both CSVs are timed from one origin so the two files share an x-axis in the report.
    let frame_csv_origin_us = clock.wall_us();
    let subscriber_frames = match args.frame_csv_out.as_ref() {
        Some(prefix) => {
            let path = frame_csv_path(prefix, "sub");
            Some(Mutex::new(SubscriberFrameLog::create(&path, frame_csv_origin_us).map_err(
                |source| {
                    RunError::Output(crate::writer::WriteError::Open {
                        path: path.display().to_string(),
                        source,
                    })
                },
            )?))
        }
        None => None,
    };

    let shared = Arc::new(SharedState { subscriber_frames, ..SharedState::default() });
    // The probe lifetime is a run parameter, so it is applied before any probe is issued.
    *shared.probe.lock() = ProbeTracker::with_lifetime_us(args.probe_lifetime_us());
    let shutdown = Arc::new(AtomicBool::new(false));

    let snapshots = Arc::new(Mutex::new(JsonLinesWriter::create(&args.snapshots_out)?));
    let seq_log = match args.publisher_seq_log.as_ref() {
        Some(path) => Some(Arc::new(Mutex::new(JsonLinesWriter::create(path)?))),
        None => None,
    };

    // Publish video before control so the subscriber has something to subscribe to while
    // the control stream is still warming up.
    let source = open_frame_source(&args).await?;
    // Read off before the source moves into the capture loop: the run record must name the
    // source even if the loop later fails, so a partially-written run is still attributable
    // to the experiment it actually ran.
    let video_source = VideoSourceRecord::of(&source);
    let (width, height) = (source.width(), source.height());
    let mut video = session::publish_video(&pub_room, &args, width, height).await?;
    let audio =
        if args.audio { Some(session::publish_audio(&pub_room, &args).await?) } else { None };

    let control_sender = build_control_sender(&pub_room, &args).await?;
    let counters = Arc::new(PublisherCounters::default());
    // Deep enough that the channel cannot realistically fill: probes are issued at the
    // stats cadence (1-10 Hz) and drained at the control rate (200 Hz), so a backlog
    // means the control publisher has stopped entirely — a condition the publish
    // shortfall metric already reports. The depth keeps a transient scheduling hiccup
    // from being miscounted as probe loss.
    let (probe_tx, probe_rx) = mpsc::channel(64);

    let mut tasks = Vec::new();

    // Publisher stages are read from the SDK's own event stream, subscribed inside
    // `publish_video` before the track was published — see the note there on why the
    // ordering is not negotiable.
    let publisher_frames = match (args.frame_csv_out.as_ref(), video.publish_timing.take()) {
        (Some(prefix), Some(mut events)) => {
            let path = frame_csv_path(prefix, "pub");
            let log = Arc::new(Mutex::new(
                PublisherFrameLog::create(&path, frame_csv_origin_us).map_err(|source| {
                    RunError::Output(crate::writer::WriteError::Open {
                        path: path.display().to_string(),
                        source,
                    })
                })?,
            ));
            let task_log = Arc::clone(&log);
            let task_shutdown = Arc::clone(&shutdown);
            tasks.push(tokio::spawn(async move {
                while let Some(event) = events.next().await {
                    if task_shutdown.load(Ordering::Acquire) {
                        break;
                    }
                    if let Err(e) = task_log.lock().record_event(event) {
                        log::warn!("publisher frame CSV write failed: {e}");
                        break;
                    }
                }
            }));
            Some(log)
        }
        _ => None,
    };

    tasks.push(tokio::spawn(
        VideoCaptureLoop::new(
            source,
            video.source.clone(),
            clock.clone(),
            Duration::from_secs_f64(1.0 / args.fps.max(1) as f64),
            duration,
            args.attach_timestamp,
            args.attach_frame_id,
            Arc::clone(&shared.frames_captured),
        )
        .with_frame_log(publisher_frames.clone())
        .run(),
    ));

    if let Some(audio) = audio.as_ref() {
        tasks.push(tokio::spawn(
            AudioToneLoop::new(audio.source.clone(), args.audio_source, clock.clone(), duration)
                .run(),
        ));
    }

    tasks.push(tokio::spawn(
        crate::probe::ProbeLoop::new(
            Arc::clone(&shared),
            clock.clone(),
            probe_tx,
            args.probe_interval(),
            duration,
            Arc::clone(&shutdown),
        )
        .run(),
    ));

    tasks.push(tokio::spawn(
        ControlPublisher::new(
            control_sender,
            clock.clone(),
            args.control_interval(),
            duration,
            Arc::clone(&counters),
            seq_log.clone(),
            probe_rx,
        )
        .run(),
    ));

    // The publisher end handles probe echoes coming back from the subscriber, and its
    // room events supply the send-side session timeline.
    tasks.push(tokio::spawn(publisher_event_loop(
        pub_events,
        Arc::clone(&shared),
        clock.clone(),
        Arc::clone(&shutdown),
    )));

    tasks.push(tokio::spawn(subscriber_event_loop(
        Arc::clone(&sub_room),
        sub_events,
        Arc::clone(&shared),
        clock.clone(),
        args.clone(),
        Arc::clone(&shutdown),
    )));

    let sampler = StatsSampler::new(
        args.clone(),
        clock.clone(),
        Arc::clone(&shared),
        Arc::clone(&counters),
        Arc::clone(&snapshots),
        video.track.clone(),
        audio.as_ref().map(|a| a.track.clone()),
        Arc::clone(&sub_room),
        Arc::clone(&shutdown),
    );
    let sampler_result = sampler.run().await;

    shutdown.store(true, Ordering::Release);
    for task in tasks {
        task.abort();
    }

    // Flushed after the writing tasks are down, so no row is half-written, and before the
    // rooms close, so a teardown failure cannot cost the run its per-frame data. Unlike
    // the snapshot writer these are block-buffered, so without this a run's final second
    // of frames would be lost on exit.
    if let Some(log) = publisher_frames.as_ref() {
        if let Err(e) = log.lock().flush() {
            log::warn!("publisher frame CSV flush failed: {e}");
        }
    }
    if let Some(log) = shared.subscriber_frames.as_ref() {
        if let Err(e) = log.lock().flush() {
            log::warn!("subscriber frame CSV flush failed: {e}");
        }
    }

    // Teardown failures are logged rather than swallowed. The matrix reuses room names
    // across repeats, so a room that fails to close can outlive its cell and the next
    // repeat inherits its participants; this warning is the only signal of that.
    if let Err(e) = pub_room.close().await {
        log::warn!("publisher room close failed: {e}");
    }
    if let Err(e) = sub_room.close().await {
        log::warn!("subscriber room close failed: {e}");
    }

    // Written after the last snapshot so its absence marks an incomplete run, and before
    // the failure checks so a run that ends badly still leaves the metadata explaining it.
    write_run_metadata(
        &snapshots,
        &args,
        &clock,
        &sampler_result,
        &shared,
        &counters,
        &video_source,
    );

    if let Some(reason) = shared.session_lost.lock().clone() {
        return Err(RunError::SessionLost(reason));
    }
    if !sampler_result.saw_subscription {
        return Err(RunError::NoSubscription);
    }

    let distinct_seq_received = shared.control.lock().distinct_received();
    Ok(RunOutcome {
        snapshots_written: sampler_result.snapshots_written,
        seq_published: counters.seq_published(),
        distinct_seq_received,
    })
}

/// What produced the run's pixels, captured before the capture loop takes the source.
///
/// Resolved up front so a run that fails partway through is still attributable to the
/// source it actually ran, rather than losing the attribution along with the source.
struct VideoSourceRecord {
    /// `test_pattern`, the resolved device name, or `rtsp:<redacted url>`.
    label: String,
    /// The device or stream and its negotiated format, absent for the synthetic pattern.
    device: Option<crate::snapshot::CameraDevice>,
}

impl VideoSourceRecord {
    /// Captures the source's identity before ownership moves to the capture loop.
    fn of(source: &FrameSource) -> Self {
        Self { label: source.source_label(), device: source.camera_device() }
    }
}

/// Appends the run-level metadata record to the snapshot file.
///
/// These are facts only the harness can supply: its own clock origin, which bounds the
/// scored window that the delivered-share denominator is defined against, and the process
/// identity that proves the process-per-buffering-mode grouping held.
fn write_run_metadata(
    writer: &Arc<Mutex<JsonLinesWriter>>,
    args: &Args,
    clock: &RunClock,
    sampler_result: &crate::sampler::SamplerResult,
    shared: &Arc<SharedState>,
    counters: &Arc<PublisherCounters>,
    video_source: &VideoSourceRecord,
) {
    let warmup_us = args.warmup_s.saturating_mul(1_000_000);
    let duration_us = args.duration_s.saturating_mul(1_000_000);
    let origin_us = clock.wall_origin_us();

    let negotiated_codec = sampler_result
        .negotiated_codec_mime
        .as_deref()
        .and_then(crate::encoder::codec_from_mime_type);
    let encoder_tier = sampler_result.encoder_implementation.as_deref().map(|implementation| {
        crate::encoder::classify_encoder(implementation, sampler_result.power_efficient_encoder)
            .as_str()
            .to_owned()
    });

    // Both ends run in this process by construction: the zero-playout-delay field trial is
    // process-global, so publisher and subscriber must share one process for the mode
    // label to mean anything.
    let process_id = std::process::id();

    let metadata = crate::snapshot::RunMetadata {
        record: "run_metadata",
        scored_window_start_unix_us: origin_us.saturating_add(warmup_us),
        scored_window_end_unix_us: origin_us.saturating_add(duration_us),
        run_origin_unix_us: origin_us,
        warmup_excluded_s: args.warmup_s,
        subscriber_process_id: process_id,
        publisher_process_id: process_id,
        harness_version: env!("CARGO_PKG_VERSION"),
        // The SDK surfaces no server build string on the room handle, so this stays null
        // rather than being guessed. Recorded as a known gap, not silently omitted.
        sfu_version: None,
        // Set only by the analysis layer, which is what compares the measured jitter
        // buffer delay against the two competing unit hypotheses.
        playout_units_confirmed: None,
        requested_codec: args.codec.as_str().to_owned(),
        negotiated_codec,
        encoder_implementation: sampler_result.encoder_implementation.clone(),
        encoder_tier,
        camera_source: video_source.label.clone(),
        camera_device: video_source.device.clone(),
        seq_published: counters.seq_published(),
        send_failures: counters.send_failures(),
        reconnect_count: shared.reconnect_count.load(Ordering::Relaxed),
        buffering_mode: args.buffering_mode.as_str().to_owned(),
        // The room-level playout hint modes are retired: no run creates a room with a
        // hint, so this is constant. The field stays in the record so pre-existing runs
        // and new ones share one schema.
        playout_delay_applied: "not_requested".to_owned(),
        g2g_timing_handler_installed: shared.g2g_handler_installed.load(Ordering::Relaxed),
        video_subscription_count: shared.video_subscriptions.load(Ordering::Relaxed),
    };

    match metadata.to_jsonl() {
        Ok(line) => {
            if let Err(e) = writer.lock().write_line(&line) {
                log::error!("run metadata write failed: {e}");
            }
        }
        Err(e) => log::error!("run metadata serialize failed: {e}"),
    }
}

/// Builds the send half of the control transport for this run.
async fn build_control_sender(
    room: &Arc<Room>,
    args: &Args,
) -> Result<ControlSender, SessionError> {
    if args.control_transport.is_data_track() {
        let track = session::publish_control_track(room).await?;
        return Ok(ControlSender::DataTrack(track));
    }
    Ok(ControlSender::DataChannel {
        room: Arc::clone(room),
        reliable: matches!(args.control_transport, crate::cli::ControlTransport::DcReliable),
    })
}

/// Consumes publisher-side room events, completing probe exchanges and recording the
/// session timeline.
async fn publisher_event_loop(
    mut events: mpsc::UnboundedReceiver<RoomEvent>,
    shared: Arc<SharedState>,
    clock: RunClock,
    shutdown: Arc<AtomicBool>,
) {
    while let Some(event) = events.recv().await {
        if shutdown.load(Ordering::Acquire) {
            break;
        }
        match event {
            RoomEvent::DataReceived { payload, topic, .. }
                if topic.as_deref() == Some(transport::PROBE_ECHO_TOPIC) =>
            {
                let Ok(echo) = ProbeEcho::decode(&payload) else {
                    continue;
                };
                shared.probe.lock().complete_probe(&echo, clock.wall_us());
            }
            RoomEvent::Reconnecting => {
                shared.reconnect_count.fetch_add(1, Ordering::Relaxed);
                log::warn!("publisher reconnecting");
            }
            RoomEvent::Disconnected { reason } => {
                // A harness-initiated close arrives after the shutdown flag is set and is
                // not a session drop; anything earlier is.
                if !shutdown.load(Ordering::Acquire) {
                    *shared.session_lost.lock() =
                        Some(format!("publisher disconnected: {reason:?}"));
                }
                break;
            }
            _ => {}
        }
    }
}

/// Consumes subscriber-side room events: subscribes to the video and control streams and
/// records everything the receive side alone can know.
async fn subscriber_event_loop(
    room: Arc<Room>,
    mut events: mpsc::UnboundedReceiver<RoomEvent>,
    shared: Arc<SharedState>,
    clock: RunClock,
    args: Args,
    shutdown: Arc<AtomicBool>,
) {
    // Cancellation flag for the video receive loop currently owning the subscription.
    let mut video_cancel: Option<Arc<AtomicBool>> = None;

    while let Some(event) = events.recv().await {
        if shutdown.load(Ordering::Acquire) {
            break;
        }
        match event {
            RoomEvent::TrackSubscribed { track: RemoteTrack::Video(video), .. } => {
                log::info!("subscribed to remote video track {}", video.sid());
                shared.video_subscriptions.fetch_add(1, Ordering::Relaxed);

                // A full reconnect re-subscribes, and the SDK hands out a brand new track
                // object rather than the previous one. Retiring the running loop keeps the
                // superseded subscription from feeding the same tracker in parallel, which
                // would double-count frames across the reconnect.
                let cancel = Arc::new(AtomicBool::new(false));
                if let Some(previous) = video_cancel.replace(Arc::clone(&cancel)) {
                    previous.store(true, Ordering::Release);
                }

                tokio::spawn(video_receive_loop(
                    video,
                    Arc::clone(&shared),
                    clock.clone(),
                    Arc::clone(&shutdown),
                    cancel,
                ));
            }
            RoomEvent::TrackUnsubscribed { track: RemoteTrack::Video(video), .. } => {
                log::info!("unsubscribed from remote video track {}", video.sid());
                if let Some(cancel) = video_cancel.take() {
                    cancel.store(true, Ordering::Release);
                }
            }
            RoomEvent::DataTrackPublished(track) => {
                if args.control_transport.is_data_track() {
                    tokio::spawn(data_track_receive_loop(
                        track,
                        Arc::clone(&room),
                        Arc::clone(&shared),
                        clock.clone(),
                        args.clone(),
                        Arc::clone(&shutdown),
                    ));
                }
            }
            RoomEvent::DataReceived { payload, topic, .. }
                if topic.as_deref() == Some(transport::CONTROL_TOPIC) =>
            {
                on_control_payload(&payload, &room, &shared, &clock, &args).await;
            }
            RoomEvent::Reconnecting => {
                shared.reconnect_count.fetch_add(1, Ordering::Relaxed);
                log::warn!("subscriber reconnecting");
            }
            RoomEvent::Disconnected { reason } => {
                if !shutdown.load(Ordering::Acquire) {
                    *shared.session_lost.lock() =
                        Some(format!("subscriber disconnected: {reason:?}"));
                }
                break;
            }
            _ => {}
        }
    }
}

/// Builds one side's per-frame CSV path from the `--frame-csv-out` prefix.
///
/// A prefix rather than two flags, so the pair is named consistently and the report script
/// can be pointed at `<prefix>.pub.csv` and `<prefix>.sub.csv` without the runner having to
/// keep two paths in step. A prefix ending in `.csv` would otherwise produce
/// `run.csv.pub.csv`, so that extension is dropped first.
fn frame_csv_path(prefix: &std::path::Path, side: &str) -> std::path::PathBuf {
    let stem = prefix
        .extension()
        .filter(|ext| ext.eq_ignore_ascii_case("csv"))
        .map_or_else(|| prefix.to_path_buf(), |_| prefix.with_extension(""));
    let mut name = stem.file_name().unwrap_or_default().to_os_string();
    name.push(format!(".{side}.csv"));
    stem.with_file_name(name)
}

/// Installs the receive-side packet trailer handler, retrying until it takes.
///
/// Returns whether a handler is present on the track. `subscribe_timing_events` reports no
/// error when it installs nothing, so the only way to know it worked is to look for the
/// handler afterwards.
async fn install_timing_handler(
    track: &RemoteVideoTrack,
    shutdown: &AtomicBool,
    cancel: &AtomicBool,
) -> bool {
    let deadline = std::time::Instant::now() + TIMING_HANDLER_RETRY_LIMIT;
    let mut attempts: u32 = 0;
    loop {
        // The returned stream is dropped: the harness reads timing off the decoded frames
        // rather than off this event stream, and the call is made for its side effect of
        // allocating the handler. Dropping it does not uninstall the handler.
        drop(track.subscribe_timing_events());
        attempts += 1;

        if track.rtc_track().packet_trailer_handler().is_some() {
            if attempts > 1 {
                log::warn!(
                    "packet trailer handler for video track {} installed on attempt {attempts}; \
                     the transceiver was not ready on the first try",
                    track.sid()
                );
            }
            return true;
        }

        if shutdown.load(Ordering::Acquire)
            || cancel.load(Ordering::Acquire)
            || std::time::Instant::now() >= deadline
        {
            return false;
        }
        tokio::time::sleep(TIMING_HANDLER_RETRY_INTERVAL).await;
    }
}

/// Reads decoded frames and derives glass-to-glass latency from their in-band metadata.
async fn video_receive_loop(
    track: RemoteVideoTrack,
    shared: Arc<SharedState>,
    clock: RunClock,
    shutdown: Arc<AtomicBool>,
    cancel: Arc<AtomicBool>,
) {
    use livekit::webrtc::video_stream::native::NativeVideoStream;

    // The receive-side packet trailer handler is allocated lazily, and this call is what
    // allocates it. It must happen *before* the stream is constructed: a stream built
    // first picks up no handler, so `frame_metadata` arrives empty and every
    // glass-to-glass sample is silently lost while the video itself looks fine.
    //
    // The call can also silently install nothing: it needs the track's transceiver, and it
    // returns a perfectly ordinary-looking stream when that is not set yet. On a full
    // reconnect the SDK re-subscribes from a detached task, so this loop can observe the
    // new track before its transceiver lands and lose the race. Retrying is safe because
    // installation is idempotent, and a late success fixes every subsequent frame.
    // When per-frame CSVs are on, the same call that installs the handler yields the
    // stage-event stream, so it is consumed here rather than dropped. The stages it
    // carries — first packet on the interface, decode start, decode finish — are stamped
    // inside WebRTC and exist nowhere else: reading a clock in this loop would time when
    // this task was next scheduled, which under matrix load is precisely when an
    // application-level read is least trustworthy.
    let stage_events = shared.subscriber_frames.is_some().then(|| track.subscribe_timing_events());

    let installed = install_timing_handler(&track, &shutdown, &cancel).await;
    if !installed {
        log::error!(
            "packet trailer handler not installed for video track {} after {:?}; \
             glass-to-glass timestamps will be absent for this subscription",
            track.sid(),
            TIMING_HANDLER_RETRY_LIMIT
        );
    }
    shared.g2g_handler_installed.fetch_and(installed, Ordering::Relaxed);

    // Drains stage events for the life of this subscription. Kept separate from the frame
    // loop because the two are not in lockstep: a frame's receive stage is emitted well
    // before it is decoded and delivered, and interleaving them on one task would let a
    // slow frame callback stall the event drain and lose stages to the broadcast buffer.
    let stage_task = stage_events.map(|mut events| {
        let shared = Arc::clone(&shared);
        let shutdown = Arc::clone(&shutdown);
        let cancel = Arc::clone(&cancel);
        tokio::spawn(async move {
            while let Some(event) = events.next().await {
                if shutdown.load(Ordering::Acquire) || cancel.load(Ordering::Acquire) {
                    break;
                }
                if let Some(log) = shared.subscriber_frames.as_ref() {
                    log.lock().record_event(event);
                }
            }
        })
    });

    let mut stream = NativeVideoStream::new(track.rtc_track());
    while let Some(frame) = stream.next().await {
        if shutdown.load(Ordering::Acquire) || cancel.load(Ordering::Acquire) {
            break;
        }
        let arrival_us = clock.wall_us();
        let metadata = frame.frame_metadata.as_ref();
        let user_timestamp = metadata.and_then(|m| m.user_timestamp);
        let frame_id = metadata.and_then(|m| m.frame_id);

        // Corrected only when the clock offset estimate is valid; otherwise the frame
        // still counts for pacing and frame-loss accounting but yields no latency sample.
        let corrected = user_timestamp.and_then(|sent| {
            let raw = arrival_us as i64 - sent as i64;
            shared.probe.lock().correct_owd_us(raw)
        });

        shared.g2g.lock().on_frame(arrival_us, user_timestamp, frame_id, corrected);

        // Delivery closes the frame's row. Only frames carrying a capture stamp can be
        // keyed to their stage events, so an un-stamped frame is left to the G2G
        // coverage gate above rather than written as a row with no origin.
        if let (Some(log), Some(capture_us)) = (shared.subscriber_frames.as_ref(), user_timestamp) {
            if let Err(e) = log.lock().record_sink(capture_us, frame_id, arrival_us) {
                log::warn!("subscriber frame CSV write failed: {e}");
            }
        }
    }

    if let Some(task) = stage_task {
        task.abort();
    }
}

/// Subscribes to the control data track with an explicit one-frame receive buffer.
async fn data_track_receive_loop(
    track: RemoteDataTrack,
    room: Arc<Room>,
    shared: Arc<SharedState>,
    clock: RunClock,
    args: Args,
    shutdown: Arc<AtomicBool>,
) {
    // The SDK default is 16 frames. At 200 Hz that is up to 80 ms of queued staleness,
    // which is exactly what the control-path deadline forbids, so the depth is set
    // explicitly rather than inherited.
    let options = livekit_datatrack::api::DataTrackSubscribeOptions::new()
        .with_buffer_size(args.control_buffer_size);
    // A 32-byte control payload never spans packets, so tracking more than one partial
    // frame buys nothing and adds reassembly state.
    track.set_pipeline_options(
        livekit_datatrack::api::RemoteDataTrackPipelineOptions::new().with_max_partial_frames(1),
    );

    let mut stream = match track.subscribe_with_options(options).await {
        Ok(stream) => stream,
        Err(e) => {
            log::error!("control data track subscribe failed: {e}");
            return;
        }
    };
    log::info!("subscribed to control data track with buffer_size={}", args.control_buffer_size);

    while let Some(frame) = stream.next().await {
        if shutdown.load(Ordering::Acquire) {
            break;
        }
        on_control_payload(&frame.payload(), &room, &shared, &clock, &args).await;
    }
}

/// Records one received control sample and echoes it when it carries a probe token.
async fn on_control_payload(
    payload: &[u8],
    room: &Room,
    shared: &Arc<SharedState>,
    clock: &RunClock,
    args: &Args,
) {
    let Ok(sample) = ControlSample::decode(payload) else {
        return;
    };
    let arrival_us = clock.wall_us();

    let raw_owd = arrival_us as i64 - sample.t_send_unix_us as i64;

    // The two locks are taken in sequence, never nested. The probe tracker is also
    // touched from the video receive path, and holding the control lock across it would
    // couple two hot paths that have no reason to contend.
    let corrected = {
        let mut probe = shared.probe.lock();
        probe.record_owd(raw_owd);
        probe.correct_owd_us(raw_owd)
    };

    {
        let mut control = shared.control.lock();
        control.on_sample(&sample, arrival_us);
        // Lateness is judged only on a corrected delay: scoring it on a raw one would
        // measure the clock offset between the two ends rather than the network.
        if let (Some(window_ms), Some(corrected)) = (args.playout_window_ms, corrected) {
            control.score_lateness(corrected, window_ms as i64 * 1000);
        }
    }

    if !sample.is_probe() {
        return;
    }
    let echo = ProbeEcho {
        token: sample.probe_token,
        t0_us: sample.t_send_unix_us,
        t1_us: arrival_us,
        t2_us: clock.wall_us(),
    };
    if let Err(e) = transport::send_probe_echo(room, &echo.encode()).await {
        log::debug!("probe echo failed: {e}");
    }
}

/// How long the harness waits for the subscription to establish before treating the run
/// as having failed to measure anything.
pub fn subscription_timeout() -> Duration {
    SUBSCRIPTION_TIMEOUT
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn session_errors_propagate_as_run_errors() {
        let err: RunError = SessionError::MissingCredential("LIVEKIT_URL").into();
        assert!(err.to_string().contains("LIVEKIT_URL"));
    }

    #[test]
    fn frame_csv_paths_pair_off_one_prefix() {
        let prefix = std::path::Path::new("runs/cell-a-r1");
        assert_eq!(frame_csv_path(prefix, "pub"), std::path::Path::new("runs/cell-a-r1.pub.csv"));
        assert_eq!(frame_csv_path(prefix, "sub"), std::path::Path::new("runs/cell-a-r1.sub.csv"));
    }

    /// A prefix that already ends in `.csv` must not produce `run.csv.pub.csv`.
    #[test]
    fn frame_csv_prefix_drops_a_trailing_csv_extension() {
        let prefix = std::path::Path::new("runs/run.csv");
        assert_eq!(frame_csv_path(prefix, "pub"), std::path::Path::new("runs/run.pub.csv"));
    }

    /// A run id containing dots is not an extension; only `.csv` is stripped.
    #[test]
    fn frame_csv_prefix_keeps_other_dotted_segments() {
        let prefix = std::path::Path::new("runs/q7.av1.r3");
        assert_eq!(frame_csv_path(prefix, "sub"), std::path::Path::new("runs/q7.av1.r3.sub.csv"));
    }

    /// A run that never saw the published track measured nothing, and must not be
    /// reported as a successful cell.
    #[test]
    fn missing_subscription_is_an_error_not_an_empty_run() {
        assert!(RunError::NoSubscription.to_string().contains("never received"));
    }

    /// Only a failure to connect may be retried. A lost session or an absent
    /// subscription can be exactly what the suite is measuring, and a failed AV1 publish
    /// is what the AV1 cell exists to detect — retrying past any of those would discard
    /// the finding rather than recover from a blip.
    #[test]
    fn only_connect_failures_are_retryable() {
        assert!(RunError::Session(SessionError::Connect("refused".into())).is_retryable());

        assert!(!RunError::Session(SessionError::Publish("av1 unsupported".into())).is_retryable());
        assert!(!RunError::Session(SessionError::MissingCredential("LIVEKIT_URL")).is_retryable());
        assert!(!RunError::Session(SessionError::Token("bad key".into())).is_retryable());
        assert!(!RunError::NoSubscription.is_retryable());
        assert!(!RunError::SessionLost("peer left".into()).is_retryable());
    }

    #[test]
    fn session_loss_names_the_reason() {
        let err = RunError::SessionLost("publisher disconnected: SignalClose".to_owned());
        assert!(err.to_string().contains("SignalClose"));
    }

    /// The handler flag is folded with AND across subscriptions, so it has to start true or
    /// a run with no video would report a failure that never happened.
    #[test]
    fn handler_flag_starts_installed_and_only_ever_clears() {
        let shared = SharedState::default();
        assert!(shared.g2g_handler_installed.load(Ordering::Relaxed));
        assert_eq!(shared.video_subscriptions.load(Ordering::Relaxed), 0);

        shared.g2g_handler_installed.fetch_and(true, Ordering::Relaxed);
        assert!(shared.g2g_handler_installed.load(Ordering::Relaxed));

        // One failed subscription condemns the run's G2G series, and a later success must
        // not paper over it: the frames that arrived untimestamped are already gone.
        shared.g2g_handler_installed.fetch_and(false, Ordering::Relaxed);
        shared.g2g_handler_installed.fetch_and(true, Ordering::Relaxed);
        assert!(!shared.g2g_handler_installed.load(Ordering::Relaxed));
    }

    /// A retiring subscription must stop its loop, or two loops feed one tracker in
    /// parallel across a reconnect and every frame is counted twice.
    #[test]
    fn superseding_a_subscription_cancels_the_previous_one() {
        let mut video_cancel: Option<Arc<AtomicBool>> = None;

        let first = Arc::new(AtomicBool::new(false));
        video_cancel.replace(Arc::clone(&first));

        let second = Arc::new(AtomicBool::new(false));
        if let Some(previous) = video_cancel.replace(Arc::clone(&second)) {
            previous.store(true, Ordering::Release);
        }
        assert!(first.load(Ordering::Acquire), "superseded loop must be cancelled");
        assert!(!second.load(Ordering::Acquire), "current loop must keep running");

        // TrackUnsubscribed retires the current one without installing a replacement.
        if let Some(cancel) = video_cancel.take() {
            cancel.store(true, Ordering::Release);
        }
        assert!(second.load(Ordering::Acquire));
        assert!(video_cancel.is_none());
    }
}
