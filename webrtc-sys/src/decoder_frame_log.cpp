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

#include "livekit/decoder_frame_log.h"

#include <algorithm>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <map>
#include <mutex>
#include <optional>
#include <string>
#include <utility>
#include <vector>

#include "api/video/video_frame.h"
#include "api/video_codecs/video_codec.h"
#include "api/video_codecs/video_decoder.h"
#include "rtc_base/logging.h"

namespace livekit_ffi {
namespace {

using Clock = std::chrono::steady_clock;

// Decoder-side half of a row, waiting for the sink to supply the frame ids.
struct PendingRow {
  std::optional<uint8_t> qp;
  int width = 0;
  int height = 0;
  double decode_ms = -1;
  std::string codec;
  std::string implementation;
  uint64_t seq = 0;  // insertion order; RTP timestamps wrap, so not the key
};

// Rows whose frame never reaches the sink are written without ids once this
// many newer frames are pending, so the map stays small.
constexpr size_t kMaxPending = 256;

class FrameLog {
 public:
  static FrameLog* Get() {
    static FrameLog* log = Open();
    return log;
  }

  void OnDecoded(uint32_t rtp_timestamp, PendingRow row) {
    std::lock_guard<std::mutex> lock(mutex_);
    row.seq = next_seq_++;
    pending_[rtp_timestamp] = std::move(row);
    if (pending_.size() > kMaxPending) {
      auto oldest = pending_.begin();
      for (auto it = pending_.begin(); it != pending_.end(); ++it) {
        if (it->second.seq < oldest->second.seq) oldest = it;
      }
      WriteLocked(oldest->first, 0, 0, oldest->second);
      pending_.erase(oldest);
    }
  }

  void OnSink(uint32_t rtp_timestamp,
              uint32_t frame_id,
              uint64_t capture_timestamp_us) {
    std::lock_guard<std::mutex> lock(mutex_);
    auto it = pending_.find(rtp_timestamp);
    if (it == pending_.end()) {
      return;
    }
    WriteLocked(rtp_timestamp, frame_id, capture_timestamp_us, it->second);
    pending_.erase(it);
  }

 private:
  explicit FrameLog(FILE* file) : file_(file), last_flush_(Clock::now()) {
    std::fputs(
        "rtp_timestamp,frame_id,capture_timestamp_us,qp,width,height,"
        "decode_ms,codec,implementation\n",
        file_);
    std::fflush(file_);
  }

  static FrameLog* Open() {
    const char* path = std::getenv("LK_DECODER_FRAME_LOG");
    if (!path || !*path) {
      return nullptr;
    }
    FILE* file = std::fopen(path, "w");
    if (!file) {
      RTC_LOG(LS_ERROR) << "LK_DECODER_FRAME_LOG: cannot open " << path;
      return nullptr;
    }
    RTC_LOG(LS_INFO) << "Per-frame decoder log: " << path;
    // Intentionally leaked: frames can be decoded until process exit.
    static FrameLog* log = new FrameLog(file);
    // Frames decoded but never delivered to the sink are rows too; write them
    // (without ids) when the process exits. exit() also flushes the FILE.
    std::atexit([] { log->FlushPending(); });
    return log;
  }

  void FlushPending() {
    std::lock_guard<std::mutex> lock(mutex_);
    std::vector<std::pair<uint32_t, const PendingRow*>> rows;
    for (const auto& [rtp, row] : pending_) rows.emplace_back(rtp, &row);
    std::sort(rows.begin(), rows.end(), [](const auto& a, const auto& b) {
      return a.second->seq < b.second->seq;
    });
    for (const auto& [rtp, row] : rows) WriteLocked(rtp, 0, 0, *row);
    pending_.clear();
    std::fflush(file_);
  }

  // Buffered; flushed at most once a second so decode never waits on disk.
  void WriteLocked(uint32_t rtp_timestamp,
                   uint32_t frame_id,
                   uint64_t capture_timestamp_us,
                   const PendingRow& row) {
    std::fprintf(file_, "%u,", rtp_timestamp);
    if (frame_id != 0) std::fprintf(file_, "%u", frame_id);
    std::fputc(',', file_);
    if (capture_timestamp_us != 0) {
      std::fprintf(file_, "%llu",
                   static_cast<unsigned long long>(capture_timestamp_us));
    }
    std::fputc(',', file_);
    if (row.qp) std::fprintf(file_, "%u", *row.qp);
    std::fprintf(file_, ",%d,%d,", row.width, row.height);
    if (row.decode_ms >= 0) std::fprintf(file_, "%.3f", row.decode_ms);
    std::fprintf(file_, ",%s,%s\n", row.codec.c_str(),
                 row.implementation.c_str());
    const Clock::time_point now = Clock::now();
    if (now - last_flush_ >= std::chrono::seconds(1)) {
      std::fflush(file_);
      last_flush_ = now;
    }
  }

  std::mutex mutex_;
  FILE* file_;
  Clock::time_point last_flush_;
  std::map<uint32_t, PendingRow> pending_;
  uint64_t next_seq_ = 0;
};

// Records decode start per RTP timestamp, forwards everything to the real
// callback unchanged, and hands QP/size/decode time to the log.
class LoggingDecoder : public webrtc::VideoDecoder,
                       public webrtc::DecodedImageCallback {
 public:
  LoggingDecoder(std::unique_ptr<webrtc::VideoDecoder> decoder, FrameLog* log)
      : decoder_(std::move(decoder)), log_(log) {}

  bool Configure(const Settings& settings) override {
    codec_ = webrtc::CodecTypeToPayloadString(settings.codec_type());
    return decoder_->Configure(settings);
  }

  int32_t Decode(const webrtc::EncodedImage& input_image,
                 bool missing_frames,
                 int64_t render_time_ms) override {
    {
      std::lock_guard<std::mutex> lock(mutex_);
      starts_[input_image.RtpTimestamp()] = Clock::now();
      while (starts_.size() > kMaxPending) starts_.erase(starts_.begin());
    }
    return decoder_->Decode(input_image, missing_frames, render_time_ms);
  }

  int32_t RegisterDecodeCompleteCallback(
      webrtc::DecodedImageCallback* callback) override {
    callback_ = callback;
    return decoder_->RegisterDecodeCompleteCallback(callback ? this : nullptr);
  }

  int32_t Release() override { return decoder_->Release(); }

  DecoderInfo GetDecoderInfo() const override {
    return decoder_->GetDecoderInfo();
  }

  const char* ImplementationName() const override {
    return decoder_->ImplementationName();
  }

  int32_t Decoded(webrtc::VideoFrame& frame) override {
    Decoded(frame, std::nullopt, std::nullopt);
    return 0;
  }

  int32_t Decoded(webrtc::VideoFrame& frame, int64_t decode_time_ms) override {
    Decoded(frame, static_cast<int32_t>(decode_time_ms), std::nullopt);
    return 0;
  }

  void Decoded(webrtc::VideoFrame& frame,
               std::optional<int32_t> decode_time_ms,
               std::optional<uint8_t> qp) override {
    PendingRow row;
    row.qp = qp;
    row.width = frame.width();
    row.height = frame.height();
    row.codec = codec_;
    row.implementation = decoder_->GetDecoderInfo().implementation_name;
    {
      std::lock_guard<std::mutex> lock(mutex_);
      auto it = starts_.find(frame.rtp_timestamp());
      if (it != starts_.end()) {
        row.decode_ms =
            std::chrono::duration<double, std::milli>(Clock::now() - it->second)
                .count();
        starts_.erase(it);
      } else if (decode_time_ms) {
        row.decode_ms = *decode_time_ms;
      }
    }
    log_->OnDecoded(frame.rtp_timestamp(), std::move(row));
    if (callback_) {
      callback_->Decoded(frame, decode_time_ms, qp);
    }
  }

 private:
  std::unique_ptr<webrtc::VideoDecoder> decoder_;
  FrameLog* log_;
  webrtc::DecodedImageCallback* callback_ = nullptr;
  std::string codec_;
  std::mutex mutex_;
  std::map<uint32_t, Clock::time_point> starts_;
};

}  // namespace

bool decoder_frame_log_enabled() {
  return FrameLog::Get() != nullptr;
}

void decoder_frame_log_on_sink(uint32_t rtp_timestamp,
                               uint32_t frame_id,
                               uint64_t capture_timestamp_us) {
  if (FrameLog* log = FrameLog::Get()) {
    log->OnSink(rtp_timestamp, frame_id, capture_timestamp_us);
  }
}

std::unique_ptr<webrtc::VideoDecoder> WrapDecoderForFrameLog(
    std::unique_ptr<webrtc::VideoDecoder> decoder) {
  FrameLog* log = FrameLog::Get();
  if (!log || !decoder) {
    return decoder;
  }
  return std::make_unique<LoggingDecoder>(std::move(decoder), log);
}

}  // namespace livekit_ffi
