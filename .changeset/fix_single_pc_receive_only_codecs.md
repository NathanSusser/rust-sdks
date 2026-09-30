---
livekit: patch
---

Single peer connection mode no longer drops decode-only codecs (e.g. H.265 on a host without an H.265 encoder) from the offer: receiving transceivers are offered with `offer_to_receive_*` set, instead of the default that libwebrtc treats as "don't receive" and turns inactive.
