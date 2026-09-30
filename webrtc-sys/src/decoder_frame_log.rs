// Copyright 2025 LiveKit, Inc.
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

#[cxx::bridge(namespace = "livekit_ffi")]
pub mod ffi {
    unsafe extern "C++" {
        include!("livekit/decoder_frame_log.h");

        /// True when `LK_DECODER_FRAME_LOG` is set and its file opened.
        fn decoder_frame_log_enabled() -> bool;

        /// Completes the per-frame decoder log row for `rtp_timestamp` with the
        /// packet-trailer ids resolved on the sink side (0 = unknown).
        fn decoder_frame_log_on_sink(rtp_timestamp: u32, frame_id: u32, capture_timestamp_us: u64);
    }
}
