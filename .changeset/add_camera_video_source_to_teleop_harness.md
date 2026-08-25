---
---

<!--
No crates need bumping. `teleop-test-matrix` is `publish = false` and is not a
knope-tracked package (like the `examples/*` members), and this change is confined to
that crate plus its matrix and analysis files. No existing crate's public API changes.
-->

# Add a real-camera video source to `teleop-harness`

`--camera-source` accepts a capture device by enumeration index or by a substring of its
name, alongside the default `test_pattern`. Frames land in the same `NativeVideoSource`,
carry the same in-band capture timestamp and frame id, and are encoded, published and
sampled by the same code as the synthetic pattern — only the pixels differ, so a camera
run is comparable to a pattern run as a spot-check.

Camera is an opt-in realism check, never a matrix default and never a swept axis: a lens
makes bitrate depend on scene content, lighting and framing, which breaks the cross-host
comparability every cell rests on. Two consequences are enforced rather than documented:

- **A camera that cannot be opened fails the run.** There is no fallback to the pattern,
  because a run recorded as `camera` that actually carried the pattern would be pooled
  with pattern runs and nothing in the record could catch it afterwards.
- **`camera_source` is a `never_pool_across` dimension.** `parse_runs.py` refuses to
  aggregate a camera run with a pattern run, or two different cameras with each other.

The run metadata records the *resolved* device and the geometry and pixel format it
actually negotiated, not the requested values: a device that downgrades 1080p30 to 720p15
presents the encoder with a different problem, and without the negotiated values that run
is indistinguishable from one that got what it asked for.
