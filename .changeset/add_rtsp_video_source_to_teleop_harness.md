---
---

<!--
No crates need bumping. `teleop-test-matrix` is `publish = false` and is not a
knope-tracked package (like the `examples/*` members), and this change is confined to
that crate plus its matrix, schema, analysis and docs files. No existing crate's public
API changes.
-->

# Add an RTSP / IP-camera video source to `teleop-harness`

`--camera-source` now also accepts an `rtsp://` or `rtsps://` URL, alongside
`test_pattern` and a local capture device. This unblocks the Tier 2 rig, whose camera is a
Pegatron "Muscat" IP camera reachable only over Ethernet: `nokhwa`, which the local-device
path uses, enumerates USB/AVFoundation/V4L2/MSMF devices and cannot open a network stream
at all.

The stream is decoded by an `ffmpeg` subprocess emitting raw I420 on stdout rather than by
an in-process RTSP or H.264 crate — real IP cameras are full of quirks ffmpeg has already
absorbed, and this keeps a decoder out of a build graph every measuring host has to
compile. The camera's audio is discarded; the harness publishes its own synthetic tone.

Everything the camera source already guarantees carries over: frames land in the same
`NativeVideoSource` and are stamped, encoded, published and sampled by the same code, RTSP
is opt-in and never a matrix default or a swept axis, and there is **no fallback to the
synthetic pattern** — a stream that cannot be opened fails the run, because a run recorded
as a camera run that actually carried the pattern would be pooled with pattern runs and
nothing in the record could catch it afterwards.

Three properties exist because this code will first be run against hardware that is not
reachable from where it was written, so it has to fail loudly rather than silently:

- **Every frame read is bounded** by `--rtsp-stall-timeout-s` (15 s, sourced from
  `matrix.yaml` `meta.parameters.rtsp_stall_timeout_s`), and a stall is its own error
  variant. A wedged RTSP session leaves ffmpeg alive holding its pipe open with no bytes
  flowing, which is indistinguishable from a slow stream; without the bound the capture
  loop blocks for the run's full duration and the failure appears nowhere.
- **A partial frame is never published.** A short read on a frame boundary (the stream
  ended) and one part-way through a frame (a torn frame) are separate errors, and the
  torn frame is discarded rather than encoded with uninitialised rows.
- **ffmpeg's stderr is drained and replayed into every error**, because it is the only
  place an auth failure, an unreachable host or a wrong stream path is ever explained.

TCP is the default media transport (`--rtsp-transport tcp|udp`): UDP RTSP degrades by
dropping media silently on a filtered or congested path, which reaches the record as a
broken camera rather than as the network problem it is.

The run record self-identifies an RTSP run — `camera_device.kind` is `rtsp` and
`camera_source` is `rtsp:<url>` — which is what `never_pool_across` needs to keep the three
sources from being aggregated. Credentials embedded in the URL are stripped everywhere the
source is logged or recorded, in both the harness and `run_matrix.py`.
