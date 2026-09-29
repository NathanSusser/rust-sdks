#ifndef WEBRTC_SYS_NVIDIA_NVENC_RATE_CONTROL_H_
#define WEBRTC_SYS_NVIDIA_NVENC_RATE_CONTROL_H_

#include <cstdint>
#include <cstdlib>
#include <string>

#include "rtc_base/logging.h"

namespace webrtc {

// Opt-in quality-constrained rate control, for the E6 measurement arm.
//
// The default path is CBR at whatever bitrate congestion control granted:
// bitrate is the constraint, the quantiser is the actuator, and when the
// quantiser saturates the encoder's remaining lever is resolution. This inverts
// that -- the quantiser is held near a target and the bitrate floats to
// whatever the content costs.
//
// Congestion control is NOT bypassed. NvEncoder::SetRates() re-caps maxBitRate
// at the granted bitrate on every rate update, so the bitrate floats only up to
// what the link has offered; the target is a quality request within that
// ceiling, not permission to ignore it. The rate-control mode and the target
// itself survive those reconfigures because GetInitializeParams() copies the
// whole stored NV_ENC_CONFIG, and SetRates() overwrites only the bitrate and
// VBV fields.
//
// Value is a QP on the 0-51 scale. Unset, unparseable, or out of range leaves
// CBR in place, so the default path cannot be left by accident.
inline constexpr char kNvencTargetQualityEnv[] = "LK_NVENC_TARGET_QUALITY";

inline uint8_t ReadNvencTargetQualityFromEnv() {
  const char* raw = std::getenv(kNvencTargetQualityEnv);
  if (raw == nullptr) {
    return 0;
  }
  char* end = nullptr;
  const long parsed = std::strtol(raw, &end, 10);
  if (end == raw || *end != '\0' || parsed < 1 || parsed > 51) {
    RTC_LOG(LS_WARNING) << kNvencTargetQualityEnv << "='" << raw
                        << "' is not an integer in 1..51; keeping CBR";
    return 0;
  }
  return static_cast<uint8_t>(parsed);
}

// VBV depth, in frames of bitrate. Under CBR the VBV (HRD) buffer is the
// encoder's per-frame size cap: with N frames of budget in the buffer, one
// frame may spend up to N frames' worth. At 5 the 2026-09-25 scene-change
// frames reached 92-218 kB against a 36 kB median at 8000 kbps -- 100-200 ms
// of serialization on a ~1 MB/s uplink, and two-thirds of every steady-state
// one-way-delay spike over 100 ms across six runs. At 1 every frame is held
// near its own budget; a scene change costs a few softer frames instead of one
// late one. Applied in InitEncode() and re-applied by NvEncoder::SetRates()
// on every rate update, so the value survives congestion-control reconfigures.
// Unset, unparseable, or outside 1..30 keeps 1.
inline constexpr char kNvencVbvFramesEnv[] = "LK_NVENC_VBV_FRAMES";

inline uint32_t ReadNvencVbvFramesFromEnv() {
  const char* raw = std::getenv(kNvencVbvFramesEnv);
  if (raw == nullptr) {
    return 1;
  }
  char* end = nullptr;
  const long parsed = std::strtol(raw, &end, 10);
  if (end == raw || *end != '\0' || parsed < 1 || parsed > 30) {
    RTC_LOG(LS_WARNING) << kNvencVbvFramesEnv << "='" << raw
                        << "' is not an integer in 1..30; keeping 1";
    return 1;
  }
  return static_cast<uint32_t>(parsed);
}

// Filler data: pad every frame up to its CBR budget. The VBV cap above bounds frames
// from above only; an easy frame still comes out small, so per-frame size wanders below
// the budget. With filler on, NVENC appends filler NAL units (type 12, which decoders
// discard) until each frame reaches bitrate / fps, so frame size on the wire is constant.
// Those bytes are real traffic up to the pinned cap. Unset or "1" keeps it on; "0" turns
// it off.
inline constexpr char kNvencFillerEnv[] = "LK_NVENC_FILLER";

inline bool ReadNvencFillerFromEnv() {
  const char* raw = std::getenv(kNvencFillerEnv);
  return raw == nullptr || std::string(raw) != "0";
}

// Intra-refresh period, in frames; 0 (the default) leaves it off. The GOP is
// already infinite, so the only IDR frames are the first one and any a
// receiver requests (PLI). With a period N the encoder spreads a refresh over
// N frames instead, so a requested keyframe no longer arrives as one frame
// several times the budget. Only matters on links where PLIs occur; the VBV
// knob above is what bounds ordinary frames. Unset, unparseable, or outside
// 0..600 keeps 0.
inline constexpr char kNvencIntraRefreshEnv[] = "LK_NVENC_INTRA_REFRESH_FRAMES";

inline uint32_t ReadNvencIntraRefreshFromEnv() {
  const char* raw = std::getenv(kNvencIntraRefreshEnv);
  if (raw == nullptr) {
    return 0;
  }
  char* end = nullptr;
  const long parsed = std::strtol(raw, &end, 10);
  if (end == raw || *end != '\0' || parsed < 0 || parsed > 600) {
    RTC_LOG(LS_WARNING) << kNvencIntraRefreshEnv << "='" << raw
                        << "' is not an integer in 0..600; keeping 0";
    return 0;
  }
  return static_cast<uint32_t>(parsed);
}

}  // namespace webrtc

#endif  // WEBRTC_SYS_NVIDIA_NVENC_RATE_CONTROL_H_
