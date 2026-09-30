---
webrtc-sys: minor
libwebrtc: patch
---

Adds an opt-in per-frame decoder log: with `LK_DECODER_FRAME_LOG=<path>` set, every decoded frame is written as a CSV row (`rtp_timestamp,frame_id,capture_timestamp_us,qp,width,height,decode_ms,codec,implementation`), with QP taken from the decoder callback and frame ids from the packet trailer. The FFmpeg H.265 decoder now reports slice QP.
