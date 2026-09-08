#ifndef WEBRTC_SYS_NVIDIA_NVENC_RATE_CONTROL_H_
#define WEBRTC_SYS_NVIDIA_NVENC_RATE_CONTROL_H_

#include <cstdint>
#include <cstdlib>

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

}  // namespace webrtc

#endif  // WEBRTC_SYS_NVIDIA_NVENC_RATE_CONTROL_H_
