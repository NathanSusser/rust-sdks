//! Send-side abstraction over the three control transports.
//!
//! The data-track path and the legacy data-channel path have different publish calls and
//! different delivery semantics, but the harness measures them identically. Unifying only
//! the send side keeps the difference in one place; the receive side differs enough
//! (a stream versus a room event) that forcing it through one type would obscure it.

use livekit::prelude::*;
use livekit_datatrack::api::{DataTrackFrame, LocalDataTrack};

use crate::cli::ControlTransport;

/// Topic carrying control samples on the legacy data-channel path.
pub const CONTROL_TOPIC: &str = "teleop-control";

/// Topic carrying probe echoes back to the originator.
pub const PROBE_ECHO_TOPIC: &str = "teleop-probe-echo";

/// Why a control sample could not be sent.
#[derive(Debug)]
pub enum SendError {
    /// The data track rejected the frame, typically because its send queue is full.
    DataTrack(String),
    /// The room rejected the data packet.
    DataChannel(RoomError),
}

impl std::fmt::Display for SendError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::DataTrack(e) => write!(f, "data track send failed: {e}"),
            Self::DataChannel(e) => write!(f, "data channel send failed: {e}"),
        }
    }
}

impl std::error::Error for SendError {}

/// The send half of a control transport.
pub enum ControlSender {
    /// `livekit-datatrack` path. Lossy, drops older frames rather than delaying newer
    /// ones, which is the behavior the robotics guidance asks for.
    DataTrack(LocalDataTrack),
    /// Legacy SCTP data channel, reliable or lossy.
    DataChannel { room: std::sync::Arc<Room>, reliable: bool },
}

impl ControlSender {
    /// Sends one encoded control sample.
    ///
    /// The data-track send is non-blocking by design: a full queue drops the frame, which
    /// is the correct behavior for a control stream where a stale command is worse than a
    /// dropped one. The drop is reported so the publisher can count it.
    pub async fn send(&self, payload: &[u8]) -> Result<(), SendError> {
        match self {
            Self::DataTrack(track) => {
                let frame = DataTrackFrame::new(payload.to_vec());
                track.try_push(frame).map_err(|e| SendError::DataTrack(e.to_string()))
            }
            Self::DataChannel { room, reliable } => room
                .local_participant()
                .publish_data(DataPacket {
                    payload: payload.to_vec(),
                    topic: Some(CONTROL_TOPIC.to_owned()),
                    reliable: *reliable,
                    destination_identities: Vec::new(),
                })
                .await
                .map_err(SendError::DataChannel),
        }
    }

    /// Which transport this is, for the run record.
    pub fn kind(&self) -> &'static str {
        match self {
            Self::DataTrack(_) => ControlTransport::DataTrackBuf1.as_str(),
            Self::DataChannel { reliable: true, .. } => ControlTransport::DcReliable.as_str(),
            Self::DataChannel { reliable: false, .. } => ControlTransport::DcLossy.as_str(),
        }
    }
}

/// Sends a probe echo back to the originator.
///
/// Echoes always travel on the reliable data channel regardless of the control transport
/// under test. The echo is not the thing being measured — the four-timestamp form
/// subtracts the peer's dwell time, so echo-path delay cancels out — and putting it on a
/// lossy path would discard round-trip measurements for a reason unrelated to the metric.
pub async fn send_probe_echo(room: &Room, payload: &[u8]) -> Result<(), SendError> {
    room.local_participant()
        .publish_data(DataPacket {
            payload: payload.to_vec(),
            topic: Some(PROBE_ECHO_TOPIC.to_owned()),
            reliable: true,
            destination_identities: Vec::new(),
        })
        .await
        .map_err(SendError::DataChannel)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn topics_are_distinct() {
        assert_ne!(CONTROL_TOPIC, PROBE_ECHO_TOPIC);
    }

    /// The transport name in the run record must match the axis value in `matrix.yaml`,
    /// or the analysis cannot group runs.
    #[test]
    fn transport_names_match_axis_values() {
        assert_eq!(ControlTransport::DataTrackBuf1.as_str(), "data_track_buf1");
        assert_eq!(ControlTransport::DcReliable.as_str(), "dc_reliable");
        assert_eq!(ControlTransport::DcLossy.as_str(), "dc_lossy");
    }
}
