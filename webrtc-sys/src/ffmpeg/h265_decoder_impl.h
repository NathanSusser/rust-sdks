/*
 * Copyright 2025 LiveKit, Inc.
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

#ifndef WEBRTC_FFMPEG_H265_DECODER_IMPL_H_
#define WEBRTC_FFMPEG_H265_DECODER_IMPL_H_

#include <api/video_codecs/video_decoder.h>
#include <common_video/h265/h265_bitstream_parser.h>
#include <common_video/include/video_frame_buffer_pool.h>

#include <optional>

struct AVBufferRef;
struct AVCodecContext;
struct AVFrame;
struct AVPacket;

namespace webrtc {

struct FfmpegApi;

// H.265 decoder backed by the system FFmpeg, using VA-API hardware decode
// (Intel/AMD GPUs) when a render node is available and FFmpeg's software HEVC
// decoder otherwise.
//
// libwebrtc.a statically links Chromium's own FFmpeg, of a different major
// version and built without HEVC, under the same symbol names. The system
// libavcodec/libavutil are therefore dlopened privately and every call goes
// through FfmpegApi; this file must never call an av* function directly, or
// the linker binds it to Chromium's copy against the system's struct layouts.
class FfmpegH265DecoderImpl : public VideoDecoder {
 public:
  // True when the system FFmpeg loads and provides an HEVC decoder.
  static bool IsSupported();

  FfmpegH265DecoderImpl();
  FfmpegH265DecoderImpl(const FfmpegH265DecoderImpl&) = delete;
  FfmpegH265DecoderImpl& operator=(const FfmpegH265DecoderImpl&) = delete;
  ~FfmpegH265DecoderImpl() override;

  bool Configure(const Settings& settings) override;
  int32_t Decode(const EncodedImage& input_image,
                 bool missing_frames,
                 int64_t render_time_ms) override;
  int32_t RegisterDecodeCompleteCallback(
      DecodedImageCallback* callback) override;
  int32_t Release() override;
  DecoderInfo GetDecoderInfo() const override;

 private:
  int32_t DeliverFrame(AVFrame* frame,
                       const EncodedImage& input_image,
                       std::optional<uint8_t> qp);

  const FfmpegApi* api_;
  AVCodecContext* context_ = nullptr;
  AVBufferRef* hw_device_ = nullptr;
  AVPacket* packet_ = nullptr;
  AVFrame* frame_ = nullptr;
  AVFrame* sw_frame_ = nullptr;
  bool hardware_ = false;

  DecodedImageCallback* decoded_complete_callback_ = nullptr;
  VideoFrameBufferPool buffer_pool_;
  // FFmpeg does not export QP; parse it from the slice header as libwebrtc's
  // own H.264 decoder does.
  H265BitstreamParser h265_parser_;
};

}  // namespace webrtc

#endif  // WEBRTC_FFMPEG_H265_DECODER_IMPL_H_
