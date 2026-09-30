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

#pragma once

#include <cstdint>
#include <memory>

namespace webrtc {
class VideoDecoder;
}

namespace livekit_ffi {

// Per-frame decoder log, enabled by setting LK_DECODER_FRAME_LOG=<path>.
// One CSV row per decoded frame:
//   rtp_timestamp,frame_id,capture_timestamp_us,qp,width,height,decode_ms,
//   codec,implementation
// The decoder side (QP, size, decode time) is recorded when the decoder hands
// the frame back; frame_id and capture_timestamp_us come from the packet
// trailer, which only the sink side can resolve, so the row is written when
// the sink reports the same RTP timestamp.

// True when LK_DECODER_FRAME_LOG is set and the file opened.
bool decoder_frame_log_enabled();

// Completes and writes the row for `rtp_timestamp` (0 = unknown ids).
void decoder_frame_log_on_sink(uint32_t rtp_timestamp,
                               uint32_t frame_id,
                               uint64_t capture_timestamp_us);

// Wraps `decoder` so its output is logged; returns it unchanged when the log
// is disabled.
std::unique_ptr<webrtc::VideoDecoder> WrapDecoderForFrameLog(
    std::unique_ptr<webrtc::VideoDecoder> decoder);

}  // namespace livekit_ffi
