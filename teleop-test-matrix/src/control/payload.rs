//! The 32-byte control sample carried on every transport.
//!
//! Fixed size so that payload length never confounds a loss measurement: a run that drops
//! samples must do so because of the network, not because the harness varied the frame
//! size. Little-endian on the wire, since both ends are this same binary.

/// Wire size of an encoded [`ControlSample`].
pub const CONTROL_SAMPLE_LEN: usize = 32;

/// Marks a sample as carrying a probe request the receiver must echo.
pub const PROBE_TOKEN_NONE: u64 = 0;

/// One control-path sample: a body-state command in the shape the matrix measures.
///
/// The send timestamp lives in the payload rather than in
/// `DataTrackFrame::with_user_timestamp` because the control path needs a sequence number,
/// a send time, and a probe token travelling together, and that field carries one value.
/// Keeping it unset also avoids leaving `duration_since_timestamp` — which assumes
/// milliseconds — silently wrong for a later reader of these frames.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ControlSample {
    /// Monotonically increasing, starting at zero. Gaps are the loss signal.
    pub seq: u64,
    /// Wall-clock send time in microseconds since the Unix epoch.
    pub t_send_unix_us: u64,
    /// Non-zero when this sample is part of a probe exchange.
    pub probe_token: u64,
    /// Reserved, sent as zero, so the wire size is fixed at 32 bytes.
    pub pad: u64,
}

/// Why a received buffer could not be read as a control sample.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DecodeError {
    /// The buffer was not exactly [`CONTROL_SAMPLE_LEN`] bytes.
    WrongLength(usize),
}

impl std::fmt::Display for DecodeError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::WrongLength(n) => {
                write!(f, "control sample must be {CONTROL_SAMPLE_LEN} bytes, got {n}")
            }
        }
    }
}

impl std::error::Error for DecodeError {}

impl ControlSample {
    /// Encodes the sample for transmission.
    pub fn encode(&self) -> [u8; CONTROL_SAMPLE_LEN] {
        let mut out = [0u8; CONTROL_SAMPLE_LEN];
        out[0..8].copy_from_slice(&self.seq.to_le_bytes());
        out[8..16].copy_from_slice(&self.t_send_unix_us.to_le_bytes());
        out[16..24].copy_from_slice(&self.probe_token.to_le_bytes());
        out[24..32].copy_from_slice(&self.pad.to_le_bytes());
        out
    }

    /// Decodes a received buffer.
    pub fn decode(bytes: &[u8]) -> Result<Self, DecodeError> {
        let buf: &[u8; CONTROL_SAMPLE_LEN] =
            bytes.try_into().map_err(|_| DecodeError::WrongLength(bytes.len()))?;
        Ok(Self {
            seq: u64::from_le_bytes(read8(buf, 0)),
            t_send_unix_us: u64::from_le_bytes(read8(buf, 8)),
            probe_token: u64::from_le_bytes(read8(buf, 16)),
            pad: u64::from_le_bytes(read8(buf, 24)),
        })
    }

    /// Whether this sample carries a probe token the peer must echo.
    pub fn is_probe(&self) -> bool {
        self.probe_token != PROBE_TOKEN_NONE
    }
}

/// Extracts a fixed eight-byte window. The offset is always in range for a buffer of
/// [`CONTROL_SAMPLE_LEN`], which the type system has already guaranteed at the call site.
fn read8(buf: &[u8; CONTROL_SAMPLE_LEN], at: usize) -> [u8; 8] {
    let mut out = [0u8; 8];
    out.copy_from_slice(&buf[at..at + 8]);
    out
}

/// A probe echo: the four timestamps of one exchange, returned by the peer.
///
/// `rtt = (t3 - t0) - (t2 - t1)` is immune to clock offset between the two hosts by
/// construction, because each difference is taken on a single clock.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ProbeEcho {
    /// Token matching the originating request.
    pub token: u64,
    /// Origin send time, on the originator's clock.
    pub t0_us: u64,
    /// Receipt time at the peer, on the peer's clock.
    pub t1_us: u64,
    /// Echo send time at the peer, on the peer's clock.
    pub t2_us: u64,
}

/// Wire size of an encoded [`ProbeEcho`].
pub const PROBE_ECHO_LEN: usize = 32;

impl ProbeEcho {
    /// Encodes the echo for transmission back to the originator.
    pub fn encode(&self) -> [u8; PROBE_ECHO_LEN] {
        let mut out = [0u8; PROBE_ECHO_LEN];
        out[0..8].copy_from_slice(&self.token.to_le_bytes());
        out[8..16].copy_from_slice(&self.t0_us.to_le_bytes());
        out[16..24].copy_from_slice(&self.t1_us.to_le_bytes());
        out[24..32].copy_from_slice(&self.t2_us.to_le_bytes());
        out
    }

    /// Decodes a received echo.
    pub fn decode(bytes: &[u8]) -> Result<Self, DecodeError> {
        let buf: &[u8; PROBE_ECHO_LEN] =
            bytes.try_into().map_err(|_| DecodeError::WrongLength(bytes.len()))?;
        Ok(Self {
            token: u64::from_le_bytes(read8(buf, 0)),
            t0_us: u64::from_le_bytes(read8(buf, 8)),
            t1_us: u64::from_le_bytes(read8(buf, 16)),
            t2_us: u64::from_le_bytes(read8(buf, 24)),
        })
    }

    /// Four-timestamp round-trip time in microseconds, given the arrival time `t3` on the
    /// originator's clock.
    ///
    /// Returns `None` when the arithmetic would go negative, which means one of the two
    /// clocks stepped mid-exchange and the sample is not usable.
    pub fn rtt_us(&self, t3_us: u64) -> Option<u64> {
        let round_trip = t3_us.checked_sub(self.t0_us)?;
        let peer_dwell = self.t2_us.checked_sub(self.t1_us)?;
        round_trip.checked_sub(peer_dwell)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn control_sample_round_trips() {
        let sample = ControlSample {
            seq: 1234567890,
            t_send_unix_us: 1_787_611_950_123_456,
            probe_token: 42,
            pad: 0,
        };
        let encoded = sample.encode();
        assert_eq!(encoded.len(), CONTROL_SAMPLE_LEN);
        assert_eq!(ControlSample::decode(&encoded).expect("decode"), sample);
    }

    #[test]
    fn payload_size_is_fixed_regardless_of_values() {
        let small = ControlSample { seq: 0, t_send_unix_us: 0, probe_token: 0, pad: 0 };
        let large = ControlSample {
            seq: u64::MAX,
            t_send_unix_us: u64::MAX,
            probe_token: u64::MAX,
            pad: u64::MAX,
        };
        assert_eq!(small.encode().len(), large.encode().len());
    }

    #[test]
    fn wrong_length_is_rejected() {
        assert_eq!(ControlSample::decode(&[0u8; 16]), Err(DecodeError::WrongLength(16)));
        assert_eq!(ControlSample::decode(&[]), Err(DecodeError::WrongLength(0)));
    }

    #[test]
    fn probe_flag_tracks_token() {
        let mut sample = ControlSample { seq: 1, t_send_unix_us: 2, probe_token: 0, pad: 0 };
        assert!(!sample.is_probe());
        sample.probe_token = 7;
        assert!(sample.is_probe());
    }

    /// The four-timestamp form must cancel a constant clock offset between the two hosts.
    /// Here the peer's clock is 10 s ahead and the true RTT is 30 ms.
    #[test]
    fn rtt_is_immune_to_clock_offset() {
        let offset_us = 10_000_000;
        let t0 = 1_000_000;
        let t1 = t0 + 15_000 + offset_us;
        let t2 = t1 + 2_000;
        let t3 = t0 + 15_000 + 2_000 + 15_000;
        let echo = ProbeEcho { token: 1, t0_us: t0, t1_us: t1, t2_us: t2 };
        assert_eq!(echo.rtt_us(t3), Some(30_000));
    }

    #[test]
    fn rtt_rejects_backwards_clocks() {
        let echo = ProbeEcho { token: 1, t0_us: 500, t1_us: 100, t2_us: 50 };
        assert_eq!(echo.rtt_us(400), None);
    }

    #[test]
    fn probe_echo_round_trips() {
        let echo = ProbeEcho { token: 9, t0_us: 1, t1_us: 2, t2_us: 3 };
        assert_eq!(ProbeEcho::decode(&echo.encode()).expect("decode"), echo);
    }
}
