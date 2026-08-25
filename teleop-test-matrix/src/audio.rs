//! Synthetic audio source.
//!
//! A microphone would make the audio level depend on the room the machine happens to be
//! in, and every concealment metric is meaningless against a silent or wildly varying
//! source. The tone is deterministic so two runs present the encoder with the same signal.

use std::f64::consts::TAU;
use std::time::Duration;

use livekit::webrtc::audio_frame::AudioFrame;
use livekit::webrtc::audio_source::native::NativeAudioSource;

use crate::cli::AudioSourceKind;
use crate::clock::RunClock;
use crate::session::{AUDIO_CHANNELS, AUDIO_SAMPLE_RATE};

/// Tone frequency in hertz. Sits in the middle of the voice band Opus is tuned for, so
/// the codec is exercised the way speech would exercise it.
const TONE_HZ: f64 = 440.0;

/// Peak amplitude, at about half of full scale. Loud enough that the audio level is
/// unambiguously non-zero, quiet enough to leave headroom.
const TONE_AMPLITUDE: f64 = 0.5;

/// Frame length. 10 ms is the WebRTC native packetization interval, so no repacketization
/// is introduced between the source and the encoder.
const FRAME_MS: u32 = 10;

/// Generates one frame of the configured signal.
///
/// `phase` is carried across frames and returned advanced, so the waveform is continuous
/// at frame boundaries; a discontinuity would show up as broadband noise and inflate the
/// bitrate for a reason unrelated to the codec.
pub fn fill_tone_frame(
    samples: &mut [i16],
    kind: AudioSourceKind,
    phase: f64,
    sample_rate: u32,
) -> f64 {
    if kind == AudioSourceKind::Silence {
        samples.fill(0);
        return phase;
    }

    let step = TAU * TONE_HZ / sample_rate as f64;
    let mut current = phase;
    for sample in samples.iter_mut() {
        *sample = (current.sin() * TONE_AMPLITUDE * i16::MAX as f64) as i16;
        current += step;
    }
    // Wrapped so the accumulator cannot lose precision over a long run.
    current % TAU
}

/// Publishes generated audio frames for the life of the run.
pub struct AudioToneLoop {
    source: NativeAudioSource,
    kind: AudioSourceKind,
    clock: RunClock,
    duration: Duration,
}

impl AudioToneLoop {
    /// Creates a tone loop feeding the given source.
    pub fn new(
        source: NativeAudioSource,
        kind: AudioSourceKind,
        clock: RunClock,
        duration: Duration,
    ) -> Self {
        Self { source, kind, clock, duration }
    }

    /// Captures frames until the run duration elapses.
    pub async fn run(self) {
        let samples_per_channel = AUDIO_SAMPLE_RATE / 1000 * FRAME_MS;
        let total_samples = (samples_per_channel * AUDIO_CHANNELS) as usize;
        let origin = self.clock.monotonic_origin();
        let mut phase = 0.0;
        let mut data = vec![0i16; total_samples];

        while origin.elapsed() < self.duration {
            phase = fill_tone_frame(&mut data, self.kind, phase, AUDIO_SAMPLE_RATE);
            let frame = AudioFrame {
                data: std::borrow::Cow::Borrowed(&data),
                sample_rate: AUDIO_SAMPLE_RATE,
                num_channels: AUDIO_CHANNELS,
                samples_per_channel,
            };
            // The source is queue-backed and paces itself; a capture error means the
            // track is gone, at which point there is nothing left to feed.
            if let Err(e) = self.source.capture_frame(&frame).await {
                log::debug!("audio capture stopped: {e}");
                break;
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn tone_is_audible() {
        let mut samples = vec![0i16; 480];
        fill_tone_frame(&mut samples, AudioSourceKind::SyntheticTone, 0.0, AUDIO_SAMPLE_RATE);
        let peak = samples.iter().map(|s| s.unsigned_abs()).max().expect("non-empty");
        assert!(peak > 1000, "tone must produce a non-zero audio level");
    }

    /// The silent source exists to exercise the validity gate that invalidates the audio
    /// columns; it must actually be silent.
    #[test]
    fn silence_is_silent() {
        let mut samples = vec![1234i16; 480];
        fill_tone_frame(&mut samples, AudioSourceKind::Silence, 0.0, AUDIO_SAMPLE_RATE);
        assert!(samples.iter().all(|&s| s == 0));
    }

    /// Two runs must present the same signal, or audio bitrate is not comparable.
    #[test]
    fn generation_is_deterministic() {
        let mut a = vec![0i16; 480];
        let mut b = vec![0i16; 480];
        let pa = fill_tone_frame(&mut a, AudioSourceKind::SyntheticTone, 0.0, AUDIO_SAMPLE_RATE);
        let pb = fill_tone_frame(&mut b, AudioSourceKind::SyntheticTone, 0.0, AUDIO_SAMPLE_RATE);
        assert_eq!(a, b);
        assert_eq!(pa, pb);
    }

    /// A phase discontinuity at the frame boundary would read as broadband noise and
    /// inflate the bitrate for a reason that has nothing to do with the codec.
    #[test]
    fn phase_is_continuous_across_frames() {
        let mut first = vec![0i16; 480];
        let phase =
            fill_tone_frame(&mut first, AudioSourceKind::SyntheticTone, 0.0, AUDIO_SAMPLE_RATE);
        let mut second = vec![0i16; 480];
        fill_tone_frame(&mut second, AudioSourceKind::SyntheticTone, phase, AUDIO_SAMPLE_RATE);

        let step = (TAU * TONE_HZ / AUDIO_SAMPLE_RATE as f64 * i16::MAX as f64 * TONE_AMPLITUDE)
            .abs()
            * 2.0;
        let boundary_jump = (second[0] as f64 - first[479] as f64).abs();
        assert!(boundary_jump < step.max(64.0), "boundary jump {boundary_jump} too large");
    }

    #[test]
    fn phase_stays_bounded() {
        let mut samples = vec![0i16; 480];
        let mut phase = 0.0;
        for _ in 0..1000 {
            phase = fill_tone_frame(
                &mut samples,
                AudioSourceKind::SyntheticTone,
                phase,
                AUDIO_SAMPLE_RATE,
            );
            assert!(phase.abs() <= TAU);
        }
    }
}
