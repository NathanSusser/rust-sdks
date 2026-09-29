//! Encoder tier classification and negotiated-codec identification.
//!
//! Encoder selection is automatic: the SDK uses a hardware encoder when the platform
//! provides one for the negotiated codec and falls back to software otherwise. The tier
//! that was actually chosen therefore has to be read back rather than requested, and it
//! must travel with every encoder-sensitive number — an AV1 result produced by a
//! CPU-starved software encoder is not an AV1 result.

/// Which encoder implementation libwebrtc actually selected.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum EncoderTier {
    /// Software encoder: libvpx, libaom, OpenH264 and friends.
    Sw,
    /// Apple VideoToolbox. H.264 and H.265 only — there is no AV1 hardware encoder on
    /// Apple Silicon, so an AV1 run on a Mac is software AV1.
    VideoToolbox,
    /// Intel or AMD VAAPI.
    Vaapi,
    /// Nvidia NVENC.
    Nvenc,
    /// Nvidia Jetson multimedia API.
    Jetson,
    /// A hardware encoder libwebrtc named in a form this classifier does not recognize.
    /// Recorded as unknown rather than guessed, so it cannot be pooled with a named tier.
    Unknown,
}

impl EncoderTier {
    /// Lowercase name as it appears in the run record and `matrix.yaml`.
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Sw => "sw",
            Self::VideoToolbox => "videotoolbox",
            Self::Vaapi => "vaapi",
            Self::Nvenc => "nvenc",
            Self::Jetson => "jetson",
            Self::Unknown => "unknown",
        }
    }
}

/// Classifies the encoder tier from libwebrtc's own strings.
///
/// `power_efficient` is libwebrtc's hardware signal and is used only to separate software
/// from unrecognized hardware; it never overrides an implementation string that names a
/// specific backend, because the two disagree on some platforms and the name is the more
/// specific evidence.
pub fn classify_encoder(implementation: &str, power_efficient: bool) -> EncoderTier {
    let name = implementation.to_ascii_lowercase();
    if name.is_empty() {
        return EncoderTier::Unknown;
    }
    // Ordered most specific first: a Jetson string also contains "nv".
    if name.contains("jetson") || name.contains("mmapi") || name.contains("v4l2") {
        return EncoderTier::Jetson;
    }
    if name.contains("nvenc") || name.contains("nvidia") {
        return EncoderTier::Nvenc;
    }
    if name.contains("videotoolbox") || name.contains("vtb") {
        return EncoderTier::VideoToolbox;
    }
    if name.contains("vaapi") || name.contains("libva") {
        return EncoderTier::Vaapi;
    }
    if is_software_implementation(&name) {
        return EncoderTier::Sw;
    }
    if power_efficient {
        EncoderTier::Unknown
    } else {
        EncoderTier::Sw
    }
}

/// Whether the implementation string names a known software encoder.
fn is_software_implementation(lowercase_name: &str) -> bool {
    const SOFTWARE_MARKERS: [&str; 7] =
        ["libvpx", "libaom", "openh264", "ffmpeg", "dav1d", "software", "external"];
    SOFTWARE_MARKERS.iter().any(|marker| lowercase_name.contains(marker))
}

/// Extracts the codec name from a `CodecStats` MIME type such as `video/AV1`.
///
/// Returns the lowercase short name used on the codec axis, so that the negotiated codec
/// can be compared directly against the requested one.
pub fn codec_from_mime_type(mime_type: &str) -> Option<String> {
    let (_, subtype) = mime_type.split_once('/')?;
    let name = subtype.to_ascii_lowercase();
    let normalized = match name.as_str() {
        "h264" | "avc1" => "h264",
        "h265" | "hevc" => "h265",
        "vp8" => "vp8",
        "vp9" => "vp9",
        "av1" | "av01" => "av1",
        other => other,
    };
    Some(normalized.to_owned())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn recognizes_software_encoders() {
        assert_eq!(classify_encoder("libvpx", false), EncoderTier::Sw);
        assert_eq!(classify_encoder("libaom", false), EncoderTier::Sw);
        assert_eq!(classify_encoder("OpenH264", false), EncoderTier::Sw);
        assert_eq!(classify_encoder("SimulcastEncoderAdapter (libvpx)", false), EncoderTier::Sw);
    }

    #[test]
    fn recognizes_hardware_encoders() {
        assert_eq!(classify_encoder("VideoToolbox", true), EncoderTier::VideoToolbox);
        assert_eq!(classify_encoder("NVENC H.264", true), EncoderTier::Nvenc);
        assert_eq!(classify_encoder("VAAPI", true), EncoderTier::Vaapi);
        assert_eq!(classify_encoder("jetson mmapi", true), EncoderTier::Jetson);
    }

    /// A Jetson implementation string can also mention Nvidia. The more specific tier
    /// must win, because Jetson and discrete NVENC are different hardware with different
    /// encode latency and must never be pooled.
    #[test]
    fn jetson_wins_over_nvidia() {
        assert_eq!(classify_encoder("nvidia jetson mmapi", true), EncoderTier::Jetson);
    }

    /// An unrecognized hardware encoder is unknown, not silently assigned a tier. A wrong
    /// tier label would let two different platforms be pooled.
    #[test]
    fn unrecognized_hardware_is_unknown_not_guessed() {
        assert_eq!(classify_encoder("SomeVendorHwEncoder", true), EncoderTier::Unknown);
    }

    #[test]
    fn unrecognized_non_power_efficient_is_software() {
        assert_eq!(classify_encoder("SomeUnknownEncoder", false), EncoderTier::Sw);
    }

    /// The field is empty until the first frame encodes. Reporting software then would
    /// misclassify every run whose first poll landed early.
    #[test]
    fn empty_implementation_is_unknown() {
        assert_eq!(classify_encoder("", false), EncoderTier::Unknown);
        assert_eq!(classify_encoder("", true), EncoderTier::Unknown);
    }

    #[test]
    fn parses_codec_mime_types() {
        assert_eq!(codec_from_mime_type("video/AV1").as_deref(), Some("av1"));
        assert_eq!(codec_from_mime_type("video/H264").as_deref(), Some("h264"));
        assert_eq!(codec_from_mime_type("video/VP9").as_deref(), Some("vp9"));
        assert_eq!(codec_from_mime_type("video/VP8").as_deref(), Some("vp8"));
        assert_eq!(codec_from_mime_type("audio/opus").as_deref(), Some("opus"));
    }

    #[test]
    fn rejects_malformed_mime_types() {
        assert_eq!(codec_from_mime_type("AV1"), None);
        assert_eq!(codec_from_mime_type(""), None);
    }

    /// The whole point of reading the negotiated codec: a run that requested AV1 and
    /// negotiated VP9 must be detectable as a mismatch.
    #[test]
    fn negotiated_codec_can_be_compared_against_requested() {
        let negotiated = codec_from_mime_type("video/VP9").expect("parses");
        assert_ne!(negotiated, crate::cli::Codec::Av1.as_str());
        assert_eq!(negotiated, crate::cli::Codec::Vp9.as_str());
    }
}
