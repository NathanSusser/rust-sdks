//! Session setup: credentials, room creation, publishing and subscribing.
//!
//! One process runs both ends of the session. The publisher and the subscriber join the
//! same room as two participants, which is what makes glass-to-glass, control delivery
//! and four-timestamp round-trip time measurable without a second host — every one of
//! those needs a send stamp and a receive stamp that can be joined.

use std::sync::Arc;

use livekit::options::{TrackPublishOptions, VideoEncoding};
use livekit::prelude::*;
use livekit::webrtc::audio_source::native::NativeAudioSource;
use livekit::webrtc::audio_source::AudioSourceOptions;
use livekit::webrtc::prelude::RtcAudioSource;
use livekit::webrtc::video_source::native::NativeVideoSource;
use livekit::webrtc::video_source::{RtcVideoSource, VideoResolution};
use livekit_api::access_token::{AccessToken, VideoGrants};
use livekit_api::services::room::RoomClient;

use crate::cli::{Args, BufferingMode};

/// Identity suffix for the publishing participant.
pub const PUBLISHER_IDENTITY_SUFFIX: &str = "pub";

/// Identity suffix for the subscribing participant.
pub const SUBSCRIBER_IDENTITY_SUFFIX: &str = "sub";

/// Track name carrying the synthetic video.
pub const VIDEO_TRACK_NAME: &str = "teleop-camera";

/// Track name carrying the synthetic audio tone.
pub const AUDIO_TRACK_NAME: &str = "teleop-audio";

/// Data track name carrying the control stream.
pub const CONTROL_TRACK_NAME: &str = "teleop-control";

/// Audio sample rate. 48 kHz is Opus's native rate, so no resampling is introduced.
pub const AUDIO_SAMPLE_RATE: u32 = 48_000;

/// Mono: the matrix measures audio as a concurrent load and a latency budget, not as a
/// spatial or quality study.
pub const AUDIO_CHANNELS: u32 = 1;

/// Why a session could not be established.
#[derive(Debug)]
pub enum SessionError {
    /// A required credential was absent from both the command line and the environment.
    MissingCredential(&'static str),
    /// An access token could not be minted.
    Token(String),
    /// The room refused the connection.
    Connect(String),
    /// A track could not be published. AV1 has no fallback path: a failed AV1 publish is
    /// an error, and reporting it as a different codec would be worse than failing.
    Publish(String),
}

impl SessionError {
    /// Whether a retry could plausibly succeed.
    ///
    /// Only [`SessionError::Connect`] qualifies. A missing credential and a failed publish
    /// are deterministic — the second especially so, since a failed AV1 publish is exactly
    /// what the AV1 cell exists to detect and retrying it would hide the result. Token
    /// minting is excluded because it is local and its failures are configuration errors.
    pub fn is_retryable(&self) -> bool {
        matches!(self, Self::Connect(_))
    }
}

impl std::fmt::Display for SessionError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::MissingCredential(name) => {
                write!(f, "{name} must be set on the command line or in the environment")
            }
            Self::Token(e) => write!(f, "token minting failed: {e}"),
            Self::Connect(e) => write!(f, "room connect failed: {e}"),
            Self::Publish(e) => write!(f, "track publish failed: {e}"),
        }
    }
}

impl std::error::Error for SessionError {}

/// Server URL and API credentials.
///
/// Resolved from the command line first and the environment second, matching the
/// convention the existing runner scripts already establish.
#[derive(Debug, Clone)]
pub struct Credentials {
    pub url: String,
    pub api_key: String,
    pub api_secret: String,
}

impl Credentials {
    /// Resolves credentials from arguments, falling back to the environment.
    pub fn resolve(args: &Args) -> Result<Self, SessionError> {
        let url = args
            .url
            .clone()
            .or_else(|| std::env::var("LIVEKIT_URL").ok())
            .ok_or(SessionError::MissingCredential("LIVEKIT_URL"))?;
        let api_key = args
            .api_key
            .clone()
            .or_else(|| std::env::var("LIVEKIT_API_KEY").ok())
            .ok_or(SessionError::MissingCredential("LIVEKIT_API_KEY"))?;
        let api_secret = args
            .api_secret
            .clone()
            .or_else(|| std::env::var("LIVEKIT_API_SECRET").ok())
            .ok_or(SessionError::MissingCredential("LIVEKIT_API_SECRET"))?;
        Ok(Self { url, api_key, api_secret })
    }

    /// Base HTTP URL for the room service, derived from the signalling URL.
    fn http_url(&self) -> String {
        self.url.replacen("wss://", "https://", 1).replacen("ws://", "http://", 1)
    }
}

/// Deletes the room before the run starts, so this run's room is its own.
///
/// The matrix reuses room names across repeats, so without this a repeat can inherit
/// participants left behind by a predecessor that failed to close cleanly — an artifact
/// that looks like a concurrency or subscription anomaly in the snapshot data rather than
/// like the teardown bug it is.
///
/// A delete failure is not fatal, and the usual cause is that the room does not exist yet,
/// which is the desired state anyway. This is logged rather than propagated: refusing to
/// run because a room could not be deleted would turn a clean slate into a failed cell.
pub async fn reset_room(credentials: &Credentials, room_name: &str) {
    let client = RoomClient::with_api_key(
        &credentials.http_url(),
        &credentials.api_key,
        &credentials.api_secret,
    );
    if let Err(e) = client.delete_room(room_name).await {
        log::debug!("delete_room({room_name}) before the run: {e}");
    }
}

/// Mints an access token for one participant of the run.
pub fn mint_token(
    credentials: &Credentials,
    room_name: &str,
    identity: &str,
) -> Result<String, SessionError> {
    AccessToken::with_api_key(&credentials.api_key, &credentials.api_secret)
        .with_identity(identity)
        .with_name(identity)
        .with_grants(VideoGrants {
            room_join: true,
            room: room_name.to_owned(),
            can_publish: true,
            can_subscribe: true,
            can_publish_data: true,
            ..Default::default()
        })
        .to_jwt()
        .map_err(|e| SessionError::Token(e.to_string()))
}

/// Room options for every participant in the matrix.
///
/// Dynacast and adaptive stream both let the SFU vary what is sent based on what
/// subscribers want, which would make the measured bitrate a function of the harness's
/// own subscription behavior rather than of the codec and profile under test. Both are
/// fixed off and recorded, as a follow-on question rather than a swept axis.
pub fn room_options() -> RoomOptions {
    let mut options = RoomOptions::default();
    options.auto_subscribe = true;
    options.adaptive_stream = false;
    options.dynacast = false;
    options
}

/// Connects one participant to the room.
pub async fn connect(
    credentials: &Credentials,
    room_name: &str,
    identity: &str,
) -> Result<(Arc<Room>, tokio::sync::mpsc::UnboundedReceiver<RoomEvent>), SessionError> {
    let token = mint_token(credentials, room_name, identity)?;
    let (room, events) = Room::connect(&credentials.url, &token, room_options())
        .await
        .map_err(|e| SessionError::Connect(e.to_string()))?;
    Ok((Arc::new(room), events))
}

/// A published video track and the source feeding it.
pub struct PublishedVideo {
    pub track: LocalVideoTrack,
    pub source: NativeVideoSource,
    pub width: u32,
    pub height: u32,
}

/// Publishes the synthetic video track.
///
/// There is no codec fallback here, deliberately. The example publisher retries H.265 as
/// H.264, but this harness excludes H.265 precisely because a silent substitution turns
/// one cell into another. An AV1 publish that fails is an error the run must surface.
pub async fn publish_video(
    room: &Room,
    args: &Args,
    width: u32,
    height: u32,
) -> Result<PublishedVideo, SessionError> {
    let source = NativeVideoSource::new(VideoResolution { width, height }, false);
    let track = LocalVideoTrack::create_video_track(
        VIDEO_TRACK_NAME,
        RtcVideoSource::Native(source.clone()),
    );

    let mut frame_metadata_features = livekit::options::FrameMetadataFeatures::default();
    frame_metadata_features.user_timestamp = args.attach_timestamp;
    frame_metadata_features.frame_id = args.attach_frame_id;

    let options = TrackPublishOptions {
        source: TrackSource::Camera,
        // Simulcast changes bitrate behavior substantially and is fixed off for the core
        // matrix; it is a follow-on question, recorded in the run record. Dynacast and
        // adaptive stream are room-level and are disabled in [`room_options`].
        simulcast: false,
        video_codec: args.codec.into(),
        video_encoder: args.encoder.into(),
        frame_metadata_features,
        video_encoding: Some(VideoEncoding {
            max_bitrate: args.max_bitrate,
            max_framerate: args.fps as f64,
        }),
        ..Default::default()
    };

    room.local_participant()
        .publish_track(LocalTrack::Video(track.clone()), options)
        .await
        .map_err(|e| {
            SessionError::Publish(format!(
                "codec {} encoder {}: {e}",
                args.codec.as_str(),
                args.encoder.as_str()
            ))
        })?;

    Ok(PublishedVideo { track, source, width, height })
}

/// A published audio track and the source feeding it.
pub struct PublishedAudio {
    pub track: LocalAudioTrack,
    pub source: NativeAudioSource,
}

/// Publishes the synthetic audio track.
pub async fn publish_audio(room: &Room, args: &Args) -> Result<PublishedAudio, SessionError> {
    let source = NativeAudioSource::new(
        AudioSourceOptions::default(),
        AUDIO_SAMPLE_RATE,
        AUDIO_CHANNELS,
        // One buffered second: enough that a scheduling hiccup in the tone generator does
        // not starve the encoder and appear as concealment the network did not cause.
        1000,
    );
    let track = LocalAudioTrack::create_audio_track(
        AUDIO_TRACK_NAME,
        RtcAudioSource::Native(source.clone()),
    );

    let options = TrackPublishOptions {
        source: TrackSource::Microphone,
        audio_encoding: Some(livekit::options::AudioEncoding { max_bitrate: args.audio_bitrate }),
        ..Default::default()
    };

    room.local_participant()
        .publish_track(LocalTrack::Audio(track.clone()), options)
        .await
        .map_err(|e| SessionError::Publish(format!("audio: {e}")))?;

    Ok(PublishedAudio { track, source })
}

/// Publishes the control data track.
pub async fn publish_control_track(room: &Room) -> Result<LocalDataTrack, SessionError> {
    room.local_participant()
        .publish_data_track(CONTROL_TRACK_NAME)
        .await
        .map_err(|e| SessionError::Publish(format!("control data track: {e}")))
}

/// Applies the process-global zero-playout-delay field trial when the mode requires it.
///
/// This mutates shared runtime state and fails if the WebRTC runtime is already up
/// without it, so it must run before anything constructs a room. It also cannot be
/// undone: one process serves exactly one buffering mode, which is why the runner batches
/// runs by mode. Calling it for any other mode would silently relabel the whole batch.
pub fn apply_buffering_mode(mode: BufferingMode) -> Result<(), SessionError> {
    if !mode.needs_zero_playout_delay() {
        return Ok(());
    }
    livekit::webrtc::enable_zero_playout_delay().map_err(|e| {
        SessionError::Connect(format!(
            "enable_zero_playout_delay failed ({e}); the WebRTC runtime was already \
             initialized without it, so this process cannot serve a zero_jitter run"
        ))
    })
}

/// Builds the identity for one end of the session.
pub fn identity_for(room_name: &str, suffix: &str) -> String {
    format!("{room_name}-{suffix}-{}", std::process::id())
}

#[cfg(test)]
mod tests {
    use super::*;
    use clap::Parser;

    fn args_with(extra: &[&str]) -> Args {
        let mut argv = vec![
            "teleop-harness",
            "--room-name",
            "teleop-test",
            "--duration-s",
            "10",
            "--snapshots-out",
            "/tmp/teleop-session-test.jsonl",
        ];
        argv.extend_from_slice(extra);
        Args::try_parse_from(argv).expect("parse")
    }

    #[test]
    fn credentials_prefer_arguments_over_environment() {
        let args = args_with(&[
            "--url",
            "ws://cli:7880",
            "--api-key",
            "cli-key",
            "--api-secret",
            "cli-secret",
        ]);
        let creds = Credentials::resolve(&args).expect("resolve");
        assert_eq!(creds.url, "ws://cli:7880");
        assert_eq!(creds.api_key, "cli-key");
    }

    #[test]
    fn missing_credentials_are_named() {
        let args = args_with(&["--url", "ws://x:7880"]);
        // The environment may legitimately carry a key; only assert on the error shape.
        if let Err(SessionError::MissingCredential(name)) = Credentials::resolve(&args) {
            assert!(name.starts_with("LIVEKIT_"));
        }
    }

    /// `reset_room` talks to the room service over HTTP while the run connects over
    /// WebSocket, so the one configured URL has to serve both.
    #[test]
    fn signalling_url_maps_to_the_http_service_url() {
        let secure = Credentials {
            url: "wss://example.livekit.cloud".to_owned(),
            api_key: "k".to_owned(),
            api_secret: "s".to_owned(),
        };
        assert_eq!(secure.http_url(), "https://example.livekit.cloud");

        let plain = Credentials {
            url: "ws://127.0.0.1:7880".to_owned(),
            api_key: "k".to_owned(),
            api_secret: "s".to_owned(),
        };
        assert_eq!(plain.http_url(), "http://127.0.0.1:7880");
    }

    #[test]
    fn tokens_are_minted_for_the_requested_room_and_identity() {
        let creds = Credentials {
            url: "ws://127.0.0.1:7880".to_owned(),
            api_key: "devkey".to_owned(),
            api_secret: "secret-that-is-long-enough-for-hmac".to_owned(),
        };
        let jwt = mint_token(&creds, "teleop-room", "teleop-room-pub-1").expect("mint");
        // Three dot-separated segments is the JWT shape; the contents are the SDK's.
        assert_eq!(jwt.split('.').count(), 3);
    }

    /// The two ends must be distinguishable participants, or they cannot subscribe to
    /// each other and every receive-side metric is empty.
    #[test]
    fn publisher_and_subscriber_identities_differ() {
        let pub_id = identity_for("teleop-room", PUBLISHER_IDENTITY_SUFFIX);
        let sub_id = identity_for("teleop-room", SUBSCRIBER_IDENTITY_SUFFIX);
        assert_ne!(pub_id, sub_id);
        assert!(pub_id.contains("teleop-room"));
    }

    /// Only `zero_jitter` may touch the process-global field trial. Calling it for any
    /// other mode would relabel every run in the batch.
    #[test]
    fn non_zero_jitter_modes_do_not_touch_the_field_trial() {
        assert!(apply_buffering_mode(BufferingMode::Default).is_ok());
    }
}
