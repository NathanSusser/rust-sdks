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

#include "h265_decoder_impl.h"

#include <dlfcn.h>

#include <cerrno>
#include <cstdlib>
#include <cstring>
#include <optional>
#include <span>

#include <api/video/color_space.h>
#include <api/video/i420_buffer.h>
#include <api/video/video_codec_type.h>
#include <api/video/video_frame.h>
#include <modules/video_coding/include/video_error_codes.h>
#include <third_party/libyuv/include/libyuv/convert.h>
#include <third_party/libyuv/include/libyuv/planar_functions.h>

#include "rtc_base/logging.h"

// These are the SYSTEM FFmpeg headers (see build.rs), not Chromium's copy
// under third_party/ffmpeg. Their major versions pick the sonames below, so
// the struct layouts used here always match the library that gets loaded.
#ifndef __STDC_CONSTANT_MACROS
#define __STDC_CONSTANT_MACROS
#endif
extern "C" {
#include <libavcodec/avcodec.h>
#include <libavutil/hwcontext.h>
#include <libavutil/log.h>
}

namespace webrtc {

// Every av* entry point this decoder uses, resolved from the system libraries.
// decltype(&::fn) is unevaluated, so naming a function here never creates a
// link-time reference to Chromium's statically linked copy.
struct FfmpegApi {
#define LK_FFMPEG_FN(name) decltype(&::name) name = nullptr;
  LK_FFMPEG_FN(avcodec_version)
  LK_FFMPEG_FN(avcodec_find_decoder)
  LK_FFMPEG_FN(avcodec_alloc_context3)
  LK_FFMPEG_FN(avcodec_open2)
  LK_FFMPEG_FN(avcodec_free_context)
  LK_FFMPEG_FN(avcodec_send_packet)
  LK_FFMPEG_FN(avcodec_receive_frame)
  LK_FFMPEG_FN(av_packet_alloc)
  LK_FFMPEG_FN(av_packet_free)
  LK_FFMPEG_FN(avutil_version)
  LK_FFMPEG_FN(av_frame_alloc)
  LK_FFMPEG_FN(av_frame_free)
  LK_FFMPEG_FN(av_frame_unref)
  LK_FFMPEG_FN(av_buffer_ref)
  LK_FFMPEG_FN(av_buffer_unref)
  LK_FFMPEG_FN(av_hwdevice_ctx_create)
  LK_FFMPEG_FN(av_hwframe_transfer_data)
  LK_FFMPEG_FN(av_log_set_level)
  LK_FFMPEG_FN(av_strerror)
#undef LK_FFMPEG_FN
};

namespace {

constexpr char kAvcodecSoname[] =
    "libavcodec.so." AV_STRINGIFY(LIBAVCODEC_VERSION_MAJOR);
constexpr char kAvutilSoname[] =
    "libavutil.so." AV_STRINGIFY(LIBAVUTIL_VERSION_MAJOR);

template <typename Fn>
bool Resolve(void* handle, const char* name, Fn* out) {
  *out = reinterpret_cast<Fn>(dlsym(handle, name));
  if (!*out) {
    RTC_LOG(LS_WARNING) << "FFmpeg H265: missing symbol " << name;
  }
  return *out != nullptr;
}

std::optional<FfmpegApi> LoadApi() {
  // RTLD_DEEPBIND keeps libavcodec's own references to libavutil inside the
  // system libraries even if a host process exports Chromium's av* symbols.
  const int flags = RTLD_NOW | RTLD_LOCAL | RTLD_DEEPBIND;
  void* avutil = dlopen(kAvutilSoname, flags);
  void* avcodec = avutil ? dlopen(kAvcodecSoname, flags) : nullptr;
  if (!avcodec) {
    RTC_LOG(LS_INFO) << "FFmpeg H265: " << kAvcodecSoname << "/"
                     << kAvutilSoname << " not loadable: " << dlerror();
    return std::nullopt;
  }

  FfmpegApi api;
  bool ok = true;
#define LK_RESOLVE(lib, name) ok &= Resolve(lib, #name, &api.name);
  LK_RESOLVE(avcodec, avcodec_version)
  LK_RESOLVE(avcodec, avcodec_find_decoder)
  LK_RESOLVE(avcodec, avcodec_alloc_context3)
  LK_RESOLVE(avcodec, avcodec_open2)
  LK_RESOLVE(avcodec, avcodec_free_context)
  LK_RESOLVE(avcodec, avcodec_send_packet)
  LK_RESOLVE(avcodec, avcodec_receive_frame)
  LK_RESOLVE(avcodec, av_packet_alloc)
  LK_RESOLVE(avcodec, av_packet_free)
  LK_RESOLVE(avutil, avutil_version)
  LK_RESOLVE(avutil, av_frame_alloc)
  LK_RESOLVE(avutil, av_frame_free)
  LK_RESOLVE(avutil, av_frame_unref)
  LK_RESOLVE(avutil, av_buffer_ref)
  LK_RESOLVE(avutil, av_buffer_unref)
  LK_RESOLVE(avutil, av_hwdevice_ctx_create)
  LK_RESOLVE(avutil, av_hwframe_transfer_data)
  LK_RESOLVE(avutil, av_log_set_level)
  LK_RESOLVE(avutil, av_strerror)
#undef LK_RESOLVE
  if (!ok) {
    return std::nullopt;
  }

  if (AV_VERSION_MAJOR(api.avcodec_version()) != LIBAVCODEC_VERSION_MAJOR ||
      AV_VERSION_MAJOR(api.avutil_version()) != LIBAVUTIL_VERSION_MAJOR) {
    RTC_LOG(LS_WARNING) << "FFmpeg H265: loaded library version does not "
                           "match the headers it was built against";
    return std::nullopt;
  }

  // FFmpeg logs to stderr; keep only errors so a stream does not flood it.
  api.av_log_set_level(AV_LOG_ERROR);
  return api;
}

// Loaded once per process; null when the system FFmpeg is unusable.
const FfmpegApi* Api() {
  static const std::optional<FfmpegApi> api = LoadApi();
  return api ? &*api : nullptr;
}

std::string ErrorString(const FfmpegApi* api, int error) {
  char buf[AV_ERROR_MAX_STRING_SIZE] = {};
  api->av_strerror(error, buf, sizeof(buf));
  return buf;
}

// Prefer VA-API surfaces. FFmpeg calls this again without VA-API if hardware
// setup fails (e.g. an unsupported profile), and the list always ends in the
// software format, so returning the last entry falls back to software decode.
AVPixelFormat GetFormat(AVCodecContext* context, const AVPixelFormat* formats) {
  const AVPixelFormat* last = formats;
  for (const AVPixelFormat* f = formats; *f != AV_PIX_FMT_NONE; ++f) {
    if (*f == AV_PIX_FMT_VAAPI && context->hw_device_ctx) {
      return *f;
    }
    last = f;
  }
  return *last;
}

bool HardwareDisabledByEnv() {
  const char* value = std::getenv("LK_FFMPEG_H265_HWACCEL");
  return value && std::strcmp(value, "0") == 0;
}

}  // namespace

bool FfmpegH265DecoderImpl::IsSupported() {
  const FfmpegApi* api = Api();
  return api && api->avcodec_find_decoder(AV_CODEC_ID_HEVC) != nullptr;
}

FfmpegH265DecoderImpl::FfmpegH265DecoderImpl()
    : api_(Api()), buffer_pool_(false) {}

FfmpegH265DecoderImpl::~FfmpegH265DecoderImpl() {
  Release();
}

VideoDecoder::DecoderInfo FfmpegH265DecoderImpl::GetDecoderInfo() const {
  VideoDecoder::DecoderInfo info;
  info.implementation_name =
      hardware_ ? "VAAPI H265 Decoder" : "FFmpeg H265 Decoder";
  info.is_hardware_accelerated = hardware_;
  return info;
}

bool FfmpegH265DecoderImpl::Configure(const Settings& settings) {
  if (settings.codec_type() != kVideoCodecH265) {
    RTC_LOG(LS_ERROR) << "FFmpeg H265: codec type is not H265";
    return false;
  }
  if (!api_) {
    RTC_LOG(LS_ERROR) << "FFmpeg H265: system FFmpeg unavailable";
    return false;
  }
  Release();

  const AVCodec* codec = api_->avcodec_find_decoder(AV_CODEC_ID_HEVC);
  if (!codec) {
    RTC_LOG(LS_ERROR) << "FFmpeg H265: no HEVC decoder in system FFmpeg";
    return false;
  }
  context_ = api_->avcodec_alloc_context3(codec);
  if (!context_) {
    return false;
  }
  context_->flags |= AV_CODEC_FLAG_LOW_DELAY;
  context_->get_format = &GetFormat;

  int ret = HardwareDisabledByEnv()
                ? AVERROR(ENODEV)
                : api_->av_hwdevice_ctx_create(
                      &hw_device_, AV_HWDEVICE_TYPE_VAAPI, nullptr, nullptr, 0);
  if (ret == 0) {
    context_->hw_device_ctx = api_->av_buffer_ref(hw_device_);
    context_->thread_count = 1;
    hardware_ = true;
  } else {
    RTC_LOG(LS_WARNING) << "FFmpeg H265: VA-API unavailable ("
                        << ErrorString(api_, ret)
                        << "), decoding in software";
    // Slice threads only: frame threading would hold frames back.
    context_->thread_count = 0;
    context_->thread_type = FF_THREAD_SLICE;
    hardware_ = false;
  }

  ret = api_->avcodec_open2(context_, codec, nullptr);
  if (ret < 0) {
    RTC_LOG(LS_ERROR) << "FFmpeg H265: avcodec_open2 failed: "
                      << ErrorString(api_, ret);
    Release();
    return false;
  }

  packet_ = api_->av_packet_alloc();
  frame_ = api_->av_frame_alloc();
  sw_frame_ = api_->av_frame_alloc();
  if (!packet_ || !frame_ || !sw_frame_) {
    Release();
    return false;
  }
  RTC_LOG(LS_INFO) << "FFmpeg H265: decoder configured, "
                   << (hardware_ ? "VA-API hardware" : "software");
  return true;
}

int32_t FfmpegH265DecoderImpl::RegisterDecodeCompleteCallback(
    DecodedImageCallback* callback) {
  decoded_complete_callback_ = callback;
  return WEBRTC_VIDEO_CODEC_OK;
}

int32_t FfmpegH265DecoderImpl::Release() {
  if (api_) {
    if (sw_frame_) api_->av_frame_free(&sw_frame_);
    if (frame_) api_->av_frame_free(&frame_);
    if (packet_) api_->av_packet_free(&packet_);
    if (context_) api_->avcodec_free_context(&context_);
    if (hw_device_) api_->av_buffer_unref(&hw_device_);
  }
  buffer_pool_.Release();
  return WEBRTC_VIDEO_CODEC_OK;
}

int32_t FfmpegH265DecoderImpl::Decode(const EncodedImage& input_image,
                                      bool /*missing_frames*/,
                                      int64_t /*render_time_ms*/) {
  if (!context_) {
    return WEBRTC_VIDEO_CODEC_UNINITIALIZED;
  }
  if (!decoded_complete_callback_) {
    RTC_LOG(LS_ERROR) << "FFmpeg H265: decode callback not set";
    return WEBRTC_VIDEO_CODEC_UNINITIALIZED;
  }
  if (!input_image.data() || !input_image.size()) {
    return WEBRTC_VIDEO_CODEC_ERR_PARAMETER;
  }

  // Not refcounted (buf == nullptr), so FFmpeg copies the data and the
  // packet only needs its fields cleared afterwards.
  packet_->data = const_cast<uint8_t*>(input_image.data());
  packet_->size = static_cast<int>(input_image.size());
  packet_->pts = input_image.RtpTimestamp();
  int ret = api_->avcodec_send_packet(context_, packet_);
  packet_->data = nullptr;
  packet_->size = 0;
  if (ret < 0) {
    RTC_LOG(LS_WARNING) << "FFmpeg H265: avcodec_send_packet failed: "
                        << ErrorString(api_, ret);
    return WEBRTC_VIDEO_CODEC_ERROR;
  }

  h265_parser_.ParseBitstream(
      std::span<const uint8_t>(input_image.data(), input_image.size()));
  std::optional<uint8_t> qp;
  if (std::optional<int> slice_qp = h265_parser_.GetLastSliceQp();
      slice_qp && *slice_qp >= 0 && *slice_qp <= 255) {
    qp = static_cast<uint8_t>(*slice_qp);
  }

  while ((ret = api_->avcodec_receive_frame(context_, frame_)) == 0) {
    int32_t result = DeliverFrame(frame_, input_image, qp);
    api_->av_frame_unref(frame_);
    if (result != WEBRTC_VIDEO_CODEC_OK) {
      return result;
    }
  }
  if (ret != AVERROR(EAGAIN) && ret != AVERROR_EOF) {
    RTC_LOG(LS_WARNING) << "FFmpeg H265: avcodec_receive_frame failed: "
                        << ErrorString(api_, ret);
    return WEBRTC_VIDEO_CODEC_ERROR;
  }
  return WEBRTC_VIDEO_CODEC_OK;
}

int32_t FfmpegH265DecoderImpl::DeliverFrame(AVFrame* frame,
                                            const EncodedImage& input_image,
                                            std::optional<uint8_t> qp) {
  const AVFrame* src = frame;
  if (frame->format == AV_PIX_FMT_VAAPI) {
    api_->av_frame_unref(sw_frame_);
    int ret = api_->av_hwframe_transfer_data(sw_frame_, frame, 0);
    if (ret < 0) {
      RTC_LOG(LS_ERROR) << "FFmpeg H265: VA-API surface download failed: "
                        << ErrorString(api_, ret);
      return WEBRTC_VIDEO_CODEC_ERROR;
    }
    src = sw_frame_;
    hardware_ = true;
  } else {
    hardware_ = false;
  }

  const int width = src->width;
  const int height = src->height;
  scoped_refptr<I420Buffer> buffer =
      buffer_pool_.CreateI420Buffer(width, height);
  if (!buffer) {
    RTC_LOG(LS_WARNING) << "FFmpeg H265: I420 buffer pool exhausted";
    return WEBRTC_VIDEO_CODEC_NO_OUTPUT;
  }

  int convert = -1;
  switch (src->format) {
    case AV_PIX_FMT_NV12:
      convert = libyuv::NV12ToI420(
          src->data[0], src->linesize[0], src->data[1], src->linesize[1],
          buffer->MutableDataY(), buffer->StrideY(), buffer->MutableDataU(),
          buffer->StrideU(), buffer->MutableDataV(), buffer->StrideV(), width,
          height);
      break;
    case AV_PIX_FMT_YUV420P:
    case AV_PIX_FMT_YUVJ420P:
      convert = libyuv::I420Copy(
          src->data[0], src->linesize[0], src->data[1], src->linesize[1],
          src->data[2], src->linesize[2], buffer->MutableDataY(),
          buffer->StrideY(), buffer->MutableDataU(), buffer->StrideU(),
          buffer->MutableDataV(), buffer->StrideV(), width, height);
      break;
    default:
      // 10-bit (Main10) output lands here; I420Buffer is 8-bit only.
      RTC_LOG(LS_ERROR) << "FFmpeg H265: unsupported output pixel format "
                        << src->format;
      return WEBRTC_VIDEO_CODEC_ERROR;
  }
  if (convert != 0) {
    RTC_LOG(LS_ERROR) << "FFmpeg H265: I420 conversion failed: " << convert;
    return WEBRTC_VIDEO_CODEC_ERROR;
  }

  const uint32_t rtp_timestamp =
      frame->pts != AV_NOPTS_VALUE ? static_cast<uint32_t>(frame->pts)
                                   : input_image.RtpTimestamp();
  VideoFrame::Builder builder;
  builder.set_video_frame_buffer(buffer).set_timestamp_rtp(rtp_timestamp);
  if (input_image.ColorSpace()) {
    builder.set_color_space(*input_image.ColorSpace());
  } else if (frame->color_range != AVCOL_RANGE_UNSPECIFIED) {
    // FFmpeg's enums carry the H.273 code points, as do webrtc's.
    builder.set_color_space(ColorSpace(
        static_cast<ColorSpace::PrimaryID>(frame->color_primaries),
        static_cast<ColorSpace::TransferID>(frame->color_trc),
        static_cast<ColorSpace::MatrixID>(frame->colorspace),
        static_cast<ColorSpace::RangeID>(frame->color_range)));
  }
  VideoFrame decoded_frame = builder.build();
  decoded_complete_callback_->Decoded(decoded_frame, std::nullopt, qp);
  return WEBRTC_VIDEO_CODEC_OK;
}

}  // namespace webrtc
