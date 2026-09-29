---
webrtc-sys: minor
---

Adds an H.265 decoder for Linux x86_64 hosts without NVDEC, backed by the system FFmpeg (libavcodec/libavutil, dlopened at runtime) with VA-API hardware decode on Intel/AMD GPUs and software fallback. Built when FFmpeg headers are found (`LK_FFMPEG_INCLUDE_DIR`); `LK_FFMPEG_H265_HWACCEL=0` forces software decode.
