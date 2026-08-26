---
---

<!--
No crates need bumping. `teleop-test-matrix` is `publish = false` and is not a
knope-tracked package (like the `examples/*` members), and this change is confined to
that crate plus its docs and the new `vmaf/` glue. No existing crate's public API changes.
-->

# Add a VMAF source exporter and codec sweep to `teleop-test-matrix`

Adds `export-source`, a second binary in the crate, and `vmaf/run_vmaf.py`, so LiveKit's
[`webrtc-vmaf`](https://github.com/livekit/webrtc-vmaf) can measure codec quality on the
exact content the harness transports.

This unblocks the codec-efficiency question. T-1 encodes to a bitrate target, so quality
floats and cross-codec bitrate comparison has no fixed point — the last sweep put AV1 at
QP 40 against H.264 at QP 25-28, which are two different pictures. The documented fix was
to pin a quality target, which this SDK exposes at no layer (SDK-FINDINGS SDK-2), so the
redesign was recorded as blocked. VMAF inverts the experiment the other way instead: fix
the bitrate, encode one source with each codec, measure the resulting quality. Because the
encoding happens offline in ffmpeg rather than in a live session, no SDK change is needed.
`docs/MEASUREMENT-DESIGN.md` is amended rather than rewritten — the old reasoning is why we
thought this was blocked, and the SDK gap is still real for fixed-quality *live* encoding.

`webrtc-vmaf` is **not vendored or forked**. It stays a separate clone the user maintains,
and `--vmaf-repo` takes its path; a locally forked measurement instrument would produce
numbers nobody else could reproduce. A missing clone fails with an actionable message
rather than a traceback.

The exporter **drives the harness's own frame sources** rather than reimplementing them:
`SyntheticFrameSource` for the pattern, `CameraFrameSource`/`RtspFrameSource` for a camera.
A second copy of the pattern generator would drift from the first and nothing in either
output would reveal it — both would be plausible moving test patterns. `--camera-source` is
resolved through the same `VideoSourceSelector::resolve` the harness uses, so a value names
the same source in both binaries.

Output is Y4M rather than headerless raw video. Y4M carries geometry, frame rate and chroma
subsampling in its header, so a file read back at the wrong dimensions fails at `ffprobe`
instead of decoding into a sheared image that still scores a plausible VMAF. Stride padding
is dropped on write, since `I420Buffer` pads its rows and Y4M is packed.

Two properties are load-bearing and tested:

- **Determinism.** Two exports at the same parameters are byte-identical, verified across
  separate process invocations. The entire cross-codec comparison rests on every codec
  being handed the same content; if exports differed, a VMAF gap between two codecs could
  be a difference in the source rather than in the encoder.
- **A short source is an error, not a shorter file.** A camera that drops out mid-export
  would otherwise leave a playable clip that scores perfectly happily against far less
  content than was requested.

**A measured limitation is recorded rather than left to be rediscovered: the synthetic
pattern saturates VMAF early.** With `libx264` baseline it scores 99.95 at 500 kbps at
360p, and 98.66 at 2 Mbps at 1080p — so most of the PRD's range up to the §8.0b 5 Mbps
ceiling would yield a table of 99.9s that reads exactly like a real codec result. The
pattern is deliberately simple because the harness needs determinism, not realism. Sweeps
therefore run at 1080p with the low rungs treated as informative, and `run_vmaf.py` warns
when most cells clear 98 VMAF. That warning is about measurement validity, not a threshold
on the measurement: no verdict is derived from it, consistent with the rule that scoring
lives in the analysis layer.

The default codec set (`av1 h264 vp8 vp9`) matches the matrix so a VMAF row and a harness
row name the same codec. H.265 is supported but off by default: the matrix excludes it
because it silently falls back to H.264 at publish time, which is a transport-path reason
that does not apply to an offline encode.

`vmaf/README.md` states what VMAF can and cannot tell you, and that VMAF and harness
results must never be conflated — one is offline encode quality from ffmpeg, the other is
live transport behavior from libwebrtc's possibly-hardware encoders over a real network.
