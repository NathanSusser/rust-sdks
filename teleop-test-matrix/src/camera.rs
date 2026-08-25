//! Real-camera capture, as an opt-in alternative to the synthetic pattern.
//!
//! This exists for realism spot-checks and for the eventual Tier 2 rig, **not** for the
//! matrix. See [`crate::video`] for why the matrix default is synthetic: a camera makes
//! bitrate depend on scene content, lighting and framing, and the matrix's cross-host
//! comparability rests on every host presenting the encoder with an identical problem.
//!
//! Everything downstream of this module is the same code the synthetic path uses. Frames
//! land in the same [`NativeVideoSource`], carry the same in-band capture timestamp and
//! frame id, and are encoded, published and sampled identically — only the pixels differ.
//! That is deliberate: a camera run that took a different publish path would not be
//! comparable to a synthetic run even as a spot-check.
//!
//! There is no fallback to synthetic. A camera that cannot be opened is an error, because
//! a run labelled `camera` that actually ran synthetic is indistinguishable from a real
//! camera run in the record, and the whole point of recording the source is that the two
//! must never be pooled.

use nokhwa::pixel_format::RgbFormat;
use nokhwa::utils::{
    ApiBackend, CameraFormat, CameraIndex, CameraInfo, FrameFormat, RequestedFormat,
    RequestedFormatType, Resolution,
};
use nokhwa::Camera;

use livekit::webrtc::video_frame::I420Buffer;

/// Failure to select or open a capture device.
///
/// Every variant is fatal by design: the caller must not substitute another source.
#[derive(Debug)]
pub enum CameraError {
    /// Device enumeration itself failed.
    Enumerate(String),
    /// No capture devices are present.
    NoDevices,
    /// The requested device does not match any present device.
    NotFound { requested: String, available: Vec<String> },
    /// The device was found but could not be opened or started.
    Open { device: String, source: String },
    /// The device opened but delivered a frame in a format the harness cannot convert.
    Convert(String),
}

impl std::fmt::Display for CameraError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Enumerate(e) => write!(f, "cannot enumerate capture devices: {e}"),
            Self::NoDevices => write!(
                f,
                "no capture devices present; --camera-source requires a camera and will not \
                 fall back to the synthetic pattern"
            ),
            Self::NotFound { requested, available } => write!(
                f,
                "no capture device matches {requested:?}; available: [{}]",
                available.join(", ")
            ),
            Self::Open { device, source } => {
                write!(f, "cannot open capture device {device:?}: {source}")
            }
            Self::Convert(e) => write!(f, "cannot convert captured frame to I420: {e}"),
        }
    }
}

impl std::error::Error for CameraError {}

/// Which device a run was told to use.
///
/// Parsed from the `--camera-source` value. A bare integer selects by index; anything else
/// is matched against device names, so a run record can name the lens rather than an
/// index that means a different camera on the next host.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CameraSelector {
    /// Positional index into the enumeration order.
    Index(u32),
    /// Case-insensitive substring of the device's human-readable name.
    Name(String),
}

impl CameraSelector {
    /// Parses a `--camera-source` value that is known not to be the synthetic pattern.
    pub fn parse(value: &str) -> Self {
        match value.parse::<u32>() {
            Ok(index) => Self::Index(index),
            Err(_) => Self::Name(value.to_string()),
        }
    }

    /// The value as it appeared on the command line, for the run record.
    pub fn as_requested(&self) -> String {
        match self {
            Self::Index(i) => i.to_string(),
            Self::Name(n) => n.clone(),
        }
    }

    /// Picks the matching device from an enumeration.
    ///
    /// Name matching is a case-insensitive substring so an operator can pass `facetime`
    /// rather than the full vendor string; an exact match wins over a substring match so a
    /// device whose name is a prefix of another's stays addressable.
    fn select<'a>(&self, devices: &'a [CameraInfo]) -> Option<&'a CameraInfo> {
        match self {
            Self::Index(index) => devices.get(*index as usize),
            Self::Name(name) => {
                let needle = name.to_lowercase();
                devices.iter().find(|d| d.human_name().to_lowercase() == needle).or_else(|| {
                    devices.iter().find(|d| d.human_name().to_lowercase().contains(&needle))
                })
            }
        }
    }
}

/// The device a run actually opened, and the format it actually negotiated.
///
/// This is what reaches the run record. The requested value alone is not enough: a request
/// for 1920x1080 at 30 fps that the device downgraded to 1280x720 changes the encoder's
/// problem, and a bitrate from that run is not comparable to one from a run that got what
/// it asked for.
#[derive(Debug, Clone)]
pub struct CameraIdentity {
    /// The `--camera-source` value as given.
    pub requested: String,
    /// The device's human-readable name.
    pub device_name: String,
    /// The device's index in enumeration order.
    pub device_index: String,
    /// Backend-reported extra identification, when the backend supplies any.
    pub device_description: String,
    /// Negotiated capture width, before any even-rounding.
    pub negotiated_width: u32,
    /// Negotiated capture height, before any even-rounding.
    pub negotiated_height: u32,
    /// Negotiated capture frame rate.
    pub negotiated_fps: u32,
    /// Negotiated pixel format, e.g. `YUYV` or `MJPEG`.
    pub negotiated_format: String,
}

/// An opened camera producing I420 frames at a fixed size.
///
/// Held by the capture loop, which drives it at the run's frame cadence rather than at the
/// device's own rate, exactly as the synthetic source is driven.
pub struct CameraFrameSource {
    camera: Camera,
    identity: CameraIdentity,
    width: u32,
    height: u32,
    is_yuyv: bool,
}

impl CameraFrameSource {
    /// Enumerates devices, opens the selected one and starts its stream.
    ///
    /// `width`, `height` and `fps` are requested; the negotiated values are recorded in
    /// [`CameraIdentity`] and are what the returned source produces. YUYV is preferred over
    /// MJPEG because it needs no decode step, which keeps the capture-to-publish interval
    /// closer to the synthetic path's.
    pub fn open(
        selector: &CameraSelector,
        width: u32,
        height: u32,
        fps: u32,
    ) -> Result<Self, CameraError> {
        let devices =
            nokhwa::query(ApiBackend::Auto).map_err(|e| CameraError::Enumerate(e.to_string()))?;
        if devices.is_empty() {
            return Err(CameraError::NoDevices);
        }

        let info = selector.select(&devices).ok_or_else(|| CameraError::NotFound {
            requested: selector.as_requested(),
            available: devices.iter().map(|d| d.human_name()).collect(),
        })?;
        let device_name = info.human_name();

        let mut camera = Camera::new(
            info.index().clone(),
            RequestedFormat::new::<RgbFormat>(RequestedFormatType::AbsoluteHighestFrameRate),
        )
        .map_err(|e| CameraError::Open { device: device_name.clone(), source: e.to_string() })?;

        // Ask for the run's exact geometry in each candidate format. A device that cannot
        // serve any of them keeps whatever the backend chose, and the negotiated values —
        // not the requested ones — are what the record carries.
        for frame_format in [FrameFormat::YUYV, FrameFormat::MJPEG] {
            let wanted = CameraFormat::new(Resolution::new(width, height), frame_format, fps);
            if camera
                .set_camera_requset(RequestedFormat::new::<RgbFormat>(RequestedFormatType::Exact(
                    wanted,
                )))
                .is_ok()
            {
                break;
            }
        }

        camera.open_stream().map_err(|e| CameraError::Open {
            device: device_name.clone(),
            source: e.to_string(),
        })?;

        let format = camera.camera_format();
        let identity = CameraIdentity {
            requested: selector.as_requested(),
            device_name,
            device_index: index_label(info.index()),
            device_description: info.description().to_string(),
            negotiated_width: format.width(),
            negotiated_height: format.height(),
            negotiated_fps: format.frame_rate(),
            negotiated_format: format.format().to_string(),
        };

        log::info!(
            "camera opened: {} ({}x{} @ {} fps, {})",
            identity.device_name,
            identity.negotiated_width,
            identity.negotiated_height,
            identity.negotiated_fps,
            identity.negotiated_format
        );

        Ok(Self {
            camera,
            width: round_up_even(format.width()),
            height: round_up_even(format.height()),
            is_yuyv: format.format() == FrameFormat::YUYV,
            identity,
        })
    }

    /// Width of the frames this source produces.
    pub fn width(&self) -> u32 {
        self.width
    }

    /// Height of the frames this source produces.
    pub fn height(&self) -> u32 {
        self.height
    }

    /// The device and negotiated format, for the run record.
    pub fn identity(&self) -> &CameraIdentity {
        &self.identity
    }

    /// Reads one frame from the device and converts it to I420.
    ///
    /// The device's own capture timestamp is deliberately not used. Backends report it in
    /// inconsistent epochs — stream-relative on some, presentation-time on others — and a
    /// wrong epoch makes glass-to-glass latency read negative. The capture loop stamps the
    /// frame from the run clock at the same point in the loop that the synthetic path
    /// does, so the two sources' G2G figures measure the same interval.
    pub fn next_buffer(&mut self) -> Result<I420Buffer, CameraError> {
        let frame = self.camera.frame().map_err(|e| CameraError::Convert(e.to_string()))?;
        let (width, height) = (self.width, self.height);
        let mut buffer = I420Buffer::new(width, height);
        let (stride_y, stride_u, stride_v) = buffer.strides();
        let (data_y, data_u, data_v) = buffer.data_mut();

        let src = frame.buffer();
        if self.is_yuyv {
            // SAFETY: libyuv reads `src` as YUY2 with a stride of two bytes per pixel and
            // writes the three destination planes at the strides `I420Buffer` reported for
            // them. `src` is at least `width * height * 2` bytes for a YUYV frame of this
            // geometry, checked below, and the destination planes are owned by `buffer`
            // and outlive the call.
            if src.len() < (width as usize) * (height as usize) * 2 {
                return Err(CameraError::Convert(format!(
                    "YUYV frame is {} bytes, expected at least {} for {width}x{height}",
                    src.len(),
                    (width as usize) * (height as usize) * 2
                )));
            }
            unsafe {
                yuv_sys::rs_YUY2ToI420(
                    src.as_ptr(),
                    (width * 2) as i32,
                    data_y.as_mut_ptr(),
                    stride_y as i32,
                    data_u.as_mut_ptr(),
                    stride_u as i32,
                    data_v.as_mut_ptr(),
                    stride_v as i32,
                    width as i32,
                    height as i32,
                );
            }
            return Ok(buffer);
        }

        if src.len() == (width as usize) * (height as usize) * 3 {
            // SAFETY: the length check above establishes `src` holds exactly one RGB24
            // frame of this geometry, so libyuv's reads stay in bounds; destination planes
            // are as for the YUYV path.
            unsafe {
                yuv_sys::rs_RGB24ToI420(
                    src.as_ptr(),
                    (width * 3) as i32,
                    data_y.as_mut_ptr(),
                    stride_y as i32,
                    data_u.as_mut_ptr(),
                    stride_u as i32,
                    data_v.as_mut_ptr(),
                    stride_v as i32,
                    width as i32,
                    height as i32,
                );
            }
            return Ok(buffer);
        }

        // SAFETY: libyuv parses `src` as a self-describing MJPEG stream bounded by the
        // length passed alongside the pointer, and returns non-zero rather than reading
        // past it when the data is not a valid JPEG. Destination planes are as above.
        let converted = unsafe {
            yuv_sys::rs_MJPGToI420(
                src.as_ptr(),
                src.len(),
                data_y.as_mut_ptr(),
                stride_y as i32,
                data_u.as_mut_ptr(),
                stride_u as i32,
                data_v.as_mut_ptr(),
                stride_v as i32,
                width as i32,
                height as i32,
                width as i32,
                height as i32,
            )
        } == 0;
        if converted {
            return Ok(buffer);
        }

        // libyuv rejects some encoder-specific JPEG variants it cannot fast-path. Decoding
        // through the `image` crate is slower but accepts them, and a frame the harness
        // drops is a frame missing from a measurement rather than a slow one.
        let decoded = image::load_from_memory(src)
            .map_err(|e| CameraError::Convert(format!("MJPEG decode failed: {e}")))?
            .to_rgb8();
        if decoded.width() != width || decoded.height() != height {
            return Err(CameraError::Convert(format!(
                "decoded MJPEG is {}x{}, expected {width}x{height}",
                decoded.width(),
                decoded.height()
            )));
        }
        // SAFETY: `decoded` is RGB8 of exactly `width * height` pixels, so a stride of
        // `width * 3` keeps libyuv's reads inside its buffer; destinations are as above.
        unsafe {
            yuv_sys::rs_RGB24ToI420(
                decoded.as_raw().as_ptr(),
                (width * 3) as i32,
                data_y.as_mut_ptr(),
                stride_y as i32,
                data_u.as_mut_ptr(),
                stride_u as i32,
                data_v.as_mut_ptr(),
                stride_v as i32,
                width as i32,
                height as i32,
            );
        }
        Ok(buffer)
    }
}

/// Renders a [`CameraIndex`] as a stable string for the run record.
fn index_label(index: &CameraIndex) -> String {
    match index {
        CameraIndex::Index(i) => i.to_string(),
        CameraIndex::String(s) => s.clone(),
    }
}

/// Rounds a dimension up to the nearest even value, with a floor of two.
///
/// I420 subsamples chroma by two in each direction, so an odd dimension has no valid
/// chroma plane. Mirrors the synthetic source's rule so both produce the same geometry
/// from the same request.
fn round_up_even(value: u32) -> u32 {
    let v = value.max(2);
    if v.is_multiple_of(2) {
        v
    } else {
        v + 1
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_numeric_source_selects_by_index() {
        assert_eq!(CameraSelector::parse("0"), CameraSelector::Index(0));
        assert_eq!(CameraSelector::parse("2"), CameraSelector::Index(2));
    }

    /// An index means a different camera on every host, so a name has to be usable.
    #[test]
    fn a_non_numeric_source_selects_by_name() {
        assert_eq!(
            CameraSelector::parse("FaceTime HD Camera"),
            CameraSelector::Name("FaceTime HD Camera".to_string())
        );
        // A negative number is not an index; it must not be mistaken for one.
        assert_eq!(CameraSelector::parse("-1"), CameraSelector::Name("-1".to_string()));
    }

    #[test]
    fn the_requested_value_round_trips_for_the_run_record() {
        assert_eq!(CameraSelector::parse("1").as_requested(), "1");
        assert_eq!(CameraSelector::parse("logitech").as_requested(), "logitech");
    }

    #[test]
    fn camera_geometry_is_rounded_to_even_like_the_synthetic_source() {
        assert_eq!(round_up_even(1919), 1920);
        assert_eq!(round_up_even(1080), 1080);
        assert_eq!(round_up_even(0), 2);
    }

    /// A camera that is absent must produce an error naming what was asked for and what
    /// was there, never a silent substitution.
    #[test]
    fn a_missing_device_reports_what_was_available() {
        let err = CameraError::NotFound {
            requested: "logitech".to_string(),
            available: vec!["FaceTime HD Camera".to_string()],
        };
        let message = err.to_string();
        assert!(message.contains("logitech"), "{message}");
        assert!(message.contains("FaceTime HD Camera"), "{message}");
    }

    /// The no-devices message must say explicitly that there is no fallback, because the
    /// obvious reading of a camera failure is that the run continued on the pattern.
    #[test]
    fn the_no_devices_error_rules_out_a_synthetic_fallback() {
        let message = CameraError::NoDevices.to_string();
        assert!(message.contains("fall back"), "{message}");
    }
}
