//! Y4M serialisation for the source exporter.
//!
//! The exporter's whole purpose is to put the *same picture* the harness transports into a
//! file that `webrtc-vmaf` can encode, so the format has to be one that cannot be
//! misinterpreted. Y4M carries dimensions, frame rate and chroma subsampling in a plain
//! ASCII header, so a file that is read back at the wrong geometry fails loudly at
//! `ffprobe` instead of decoding into a sheared image that still scores a plausible VMAF.
//! Headerless `rawvideo` gives no such protection: its geometry lives only in whatever
//! command line the next person types.
//!
//! Only the `420mpeg2` subsampling tag is emitted, because [`I420Buffer`] is the one pixel
//! layout every source in this crate produces.

use std::io::{self, Write};

use livekit::webrtc::video_frame::I420Buffer;

/// Magic beginning a Y4M stream header.
const STREAM_MAGIC: &str = "YUV4MPEG2";

/// Magic beginning each frame within the stream.
const FRAME_MAGIC: &str = "FRAME";

/// Geometry and cadence written into the Y4M stream header.
///
/// Frame rate is kept as an exact rational rather than a float: Y4M's `F` tag is a ratio,
/// and `webrtc-vmaf` passes the decoded rate into an ffmpeg `fps=` filter. A rate that
/// round-tripped through a float would make ffmpeg resample frames that need no
/// resampling.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Y4mParams {
    /// Frame width in pixels. Even, as I420 requires.
    pub width: u32,
    /// Frame height in pixels. Even, as I420 requires.
    pub height: u32,
    /// Frame rate numerator.
    pub fps_num: u32,
    /// Frame rate denominator.
    pub fps_den: u32,
}

impl Y4mParams {
    /// The stream header line, including its trailing newline.
    ///
    /// `Ip` (progressive) and `A1:1` (square pixels) are stated explicitly rather than
    /// left to the reader's default, since an assumed interlacing or aspect ratio would
    /// silently change what the encoder under test is handed.
    ///
    /// ```
    /// # use teleop_test_matrix::y4m::Y4mParams;
    /// let params = Y4mParams { width: 1280, height: 720, fps_num: 30, fps_den: 1 };
    /// assert_eq!(params.header(), "YUV4MPEG2 W1280 H720 F30:1 Ip A1:1 C420mpeg2\n");
    /// ```
    pub fn header(&self) -> String {
        format!(
            "{STREAM_MAGIC} W{} H{} F{}:{} Ip A1:1 C420mpeg2\n",
            self.width, self.height, self.fps_num, self.fps_den
        )
    }
}

/// Writes an I420 frame sequence as a Y4M stream.
///
/// Modelled as a writer rather than a whole-file function so the exporter can stream a long
/// capture without holding it in memory: a 60 s 1080p export is roughly 5.6 GB.
pub struct Y4mWriter<W: Write> {
    inner: W,
    params: Y4mParams,
    frames_written: u64,
}

impl<W: Write> Y4mWriter<W> {
    /// Writes the stream header and returns a writer ready for frames.
    pub fn new(mut inner: W, params: Y4mParams) -> io::Result<Self> {
        inner.write_all(params.header().as_bytes())?;
        Ok(Self { inner, params, frames_written: 0 })
    }

    /// Appends one frame.
    ///
    /// Planes are written packed at exactly the plane width, dropping the stride padding
    /// [`I420Buffer`] allocates. Copying the padding through would offset every row after
    /// the first by the pad width and diagonally shear the picture — and because the byte
    /// count would still be plausible, the result would decode and score rather than fail.
    pub fn write_frame(&mut self, buffer: &I420Buffer) -> io::Result<()> {
        self.inner.write_all(FRAME_MAGIC.as_bytes())?;
        self.inner.write_all(b"\n")?;

        let (width, height) = (self.params.width as usize, self.params.height as usize);
        let (chroma_width, chroma_height) = (width.div_ceil(2), height.div_ceil(2));
        let (stride_y, stride_u, stride_v) = buffer.strides();
        let (data_y, data_u, data_v) = buffer.data();

        for row in 0..height {
            let start = row * stride_y as usize;
            self.inner.write_all(&data_y[start..start + width])?;
        }
        for (plane, stride) in [(data_u, stride_u), (data_v, stride_v)] {
            for row in 0..chroma_height {
                let start = row * stride as usize;
                self.inner.write_all(&plane[start..start + chroma_width])?;
            }
        }

        self.frames_written += 1;
        Ok(())
    }

    /// Frames appended so far.
    pub fn frames_written(&self) -> u64 {
        self.frames_written
    }

    /// Flushes and returns the underlying writer.
    pub fn finish(mut self) -> io::Result<W> {
        self.inner.flush()?;
        Ok(self.inner)
    }
}

/// Bytes one Y4M frame occupies, including its `FRAME\n` marker.
///
/// Used by the tests and by the exporter's size estimate; an export that will not fit is
/// worth knowing about before spending minutes generating it.
pub fn frame_len(width: u32, height: u32) -> usize {
    let (w, h) = (width as usize, height as usize);
    FRAME_MAGIC.len() + 1 + w * h + 2 * (w.div_ceil(2) * h.div_ceil(2))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::video::SyntheticFrameSource;

    #[test]
    fn the_header_is_self_describing() {
        let params = Y4mParams { width: 640, height: 480, fps_num: 30, fps_den: 1 };
        assert_eq!(params.header(), "YUV4MPEG2 W640 H480 F30:1 Ip A1:1 C420mpeg2\n");

        // A fractional rate stays exact rather than becoming 29.97.
        let ntsc = Y4mParams { width: 1920, height: 1080, fps_num: 30000, fps_den: 1001 };
        assert!(ntsc.header().contains("F30000:1001"), "{}", ntsc.header());
    }

    /// The byte layout is what ffmpeg parses; an error here is not caught by anything else
    /// in this crate.
    #[test]
    fn a_stream_has_the_exact_expected_length() {
        let (w, h) = (16u32, 8u32);
        let params = Y4mParams { width: w, height: h, fps_num: 30, fps_den: 1 };
        let mut source = SyntheticFrameSource::new(w, h);

        let mut writer = Y4mWriter::new(Vec::new(), params).expect("header");
        for _ in 0..4 {
            writer.write_frame(&source.next_buffer()).expect("frame");
        }
        assert_eq!(writer.frames_written(), 4);
        let bytes = writer.finish().expect("finish");

        assert_eq!(bytes.len(), params.header().len() + 4 * frame_len(w, h));
        assert!(bytes.starts_with(b"YUV4MPEG2 "));
        assert_eq!(&bytes[params.header().len()..params.header().len() + 6], b"FRAME\n");
    }

    /// `I420Buffer` pads its rows; Y4M is packed. Writing the padding through would shear
    /// every frame while still producing a file of plausible size that decodes and scores.
    #[test]
    fn stride_padding_is_dropped_so_rows_stay_aligned() {
        // A width that is not a convenient multiple is the case where a stride is most
        // likely to exceed the row width.
        let (w, h) = (18u32, 6u32);
        let mut source = SyntheticFrameSource::new(w, h);
        let buffer = source.next_buffer();
        let params = Y4mParams { width: w, height: h, fps_num: 30, fps_den: 1 };

        let mut writer = Y4mWriter::new(Vec::new(), params).expect("header");
        writer.write_frame(&buffer).expect("frame");
        let bytes = writer.finish().expect("finish");

        let luma_start = params.header().len() + FRAME_MAGIC.len() + 1;
        let (stride_y, _, _) = buffer.strides();
        let (data_y, _, _) = buffer.data();
        for row in 0..h as usize {
            let written =
                &bytes[luma_start + row * w as usize..luma_start + (row + 1) * w as usize];
            let source_row = &data_y[row * stride_y as usize..row * stride_y as usize + w as usize];
            assert_eq!(written, source_row, "luma row {row} is misaligned");
        }
    }

    #[test]
    fn frame_length_is_the_marker_plus_the_plane_sum() {
        assert_eq!(frame_len(1920, 1080), 6 + 1920 * 1080 * 3 / 2);
        assert_eq!(frame_len(2, 2), 6 + 6);
    }
}
