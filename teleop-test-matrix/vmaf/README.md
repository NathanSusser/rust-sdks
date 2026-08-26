# VMAF codec comparison

Offline codec-quality measurement, using LiveKit's
[`webrtc-vmaf`](https://github.com/livekit/webrtc-vmaf) on **the same video content the
harness transports**.

This answers a question the matrix cannot: *at a fixed bitrate, which codec produces the
better picture.*

---

## Why this is separate from the harness

Three reasons, and they are all reasons of kind rather than convenience:

- **Different question.** The harness measures a live session — transport, latency, jitter,
  loss, the SFU. This measures one encoder against one file. Neither substitutes for the
  other.
- **Different lifecycle.** `webrtc-vmaf` is LiveKit's tool, maintained upstream. Vendoring
  or forking it would fork a measurement instrument, and a local fork that drifted from
  upstream would produce numbers that are not comparable to anyone else's. It stays a
  **separate clone you maintain alongside this repo.**
- **Different failure surface.** A VMAF sweep is CPU-bound and offline; it needs no SFU, no
  token and no network. Coupling it to the harness would put a network dependency in front
  of a measurement that does not have one.

Only the glue lives here: a source exporter (in the harness crate, so it reuses the real
generator) and a sweep wrapper.

## Why it exists at all

T-1 (video floor) encodes to a **bitrate target**, so quality is an *output* of the
experiment rather than a control. Each codec's rate control is handed a bitrate and picks
whatever quantizer reaches it. On the last sweep AV1 settled at QP 40 while H.264 sat at
QP 25–28: two different pictures, so their bitrates are not comparable and no efficiency
claim can be drawn. See `docs/MEASUREMENT-DESIGN.md`, amendment 2026-08-25.

The obvious fix — pin quality, measure bitrate — is blocked: this SDK exposes no QP or CQ
target at any layer (SDK-FINDINGS SDK-2).

VMAF inverts the experiment instead: **fix the bitrate, encode the same source with each
codec, measure the resulting quality.** That answers the efficiency question directly
("at 2 Mbps, AV1 scores VMAF 92 where H.264 scores 78") and needs no SDK change at all,
because the encoding happens offline in ffmpeg rather than in a live LiveKit session.

---

## Two-repo layout

```
~/code/rust-sdks/teleop-test-matrix/
    src/bin/export_source.rs      the exporter  (this repo)
    vmaf/run_vmaf.py              the sweep wrapper  (this repo)
    vmaf/sources/                 exported clips, gitignored (they are large)

~/code/webrtc-vmaf/               LiveKit's tool -- cloned separately, NOT vendored
```

```bash
git clone https://github.com/livekit/webrtc-vmaf.git ~/code/webrtc-vmaf
```

`--vmaf-repo` takes the path; nothing here assumes where you put it.

**Requires** `ffmpeg` and `ffprobe` on `PATH`, built with `libvmaf` and the encoders you
sweep (`libx264`, `libvpx`, `libvpx-vp9`, `libaom-av1`). On macOS: `brew install ffmpeg`.

---

## 1. Export the source

The comparison is only valid if VMAF and the harness encode **identical content**. The
exporter drives the harness's own `SyntheticFrameSource` rather than reimplementing the
pattern, so the exported file cannot drift from what the harness actually sends.

Output is **Y4M**, which carries geometry, frame rate and chroma subsampling in its header.
A headerless `.yuv` would decode at the wrong size without complaint and still score a
plausible VMAF; Y4M fails loudly at `ffprobe` instead.

```bash
cargo build -p teleop-test-matrix --bin export-source

# Synthetic pattern -- the matrix default, deterministic, no camera needed.
./target/debug/export-source \
    --output teleop-test-matrix/vmaf/sources/pattern.y4m \
    --width 1920 --height 1080 --fps 30 --duration-s 10
```

`--dry-run` prints the size first. **These files are large:** 10 s of 1080p30 is ~933 MB,
uncompressed by definition.

### Camera / RTSP source

Implemented in the same binary, reusing `CameraFrameSource` / `RtspFrameSource`, rather
than as a separate ffmpeg invocation in Python. Doing it in Rust means a camera export and
a camera *run* resolve the same `--camera-source` value through the same code to the same
pixels — an ffmpeg-side reimplementation would negotiate its own geometry and scaling and
could silently disagree with what the harness captures.

```bash
./target/debug/export-source \
    --output teleop-test-matrix/vmaf/sources/camera.y4m \
    --camera-source rtsp://192.168.100.123/full1080p \
    --width 1920 --height 1080 --fps 10 --duration-s 10

./target/debug/export-source --output cam.y4m --camera-source 0 --duration-s 10
```

> **Caveat — generation loss.** The Tier 2 camera (Pegatron "Muscat") delivers H.264 at
> ~10 fps 1080p, so a camera export is a **re-encode of already-compressed source**. Its
> compression artifacts are baked into the "reference", which depresses absolute VMAF for
> every codec and makes the scores non-comparable to scores from a pristine source. The
> cross-codec *ranking* should survive this, since every codec sees the same degraded
> reference — but never quote an absolute VMAF from a camera export as "the quality of
> this codec at this bitrate".

---

## 2. Sweep

```bash
python3 teleop-test-matrix/vmaf/run_vmaf.py \
    --source teleop-test-matrix/vmaf/sources/pattern.y4m \
    --vmaf-repo ~/code/webrtc-vmaf \
    --width 1920 --height 1080 \
    --json teleop-test-matrix/vmaf/results.json
```

```
    kbps       av1     h264      vp8      vp9
---------------------------------------------
     500     78.21    61.04    64.90    75.11
    1000     89.44    74.30    77.02    86.98
    ...
```

Defaults: codecs `av1 h264 vp8 vp9` — the matrix's set, so a VMAF row and a harness row
name the same codec — at `500 1000 2000 3000 5000` kbps, topping out at the PRD §8.0b
5 Mbps uplink ceiling.

**H.265 is off by default but supported** (`--codec h265`). The matrix excludes it because
it is the one codec with an automatic publish-time fallback to H.264, so an H.265 *cell*
cannot be trusted to have been H.265. That is a transport-path reason and does not apply
offline, where ffmpeg either runs `libx265` or fails. It stays off by default only so the
default sweep lines up 1:1 with the matrix.

`--dry-run` enumerates the cells without needing the clone. Expect minutes per cell:
`libaom-av1` at 1080p is slow even at `cpu-used 8`.

### Read the saturation warning

If most cells score ≥ 98 VMAF, the wrapper says so. **A sweep where every codec scores ~100
is not a codec result** — it means the source was too easy for the bitrates swept, and the
remaining differences are noise that will read as signal in a table.

This is not hypothetical. Measured on the synthetic pattern with `libx264` baseline:

| resolution | 500 kbps | 1000 kbps | 2000 kbps | 5000 kbps |
|---|---|---|---|---|
| 640x360 | 99.95 | — | — | — |
| 1920x1080 | — | 66.38 | 98.66 | 99.95 |

The pattern is deliberately simple — a scrolling gradient plus a moving block — because the
harness needs it to be *deterministic*, not because it is representative video. It
saturates by ~500 kbps at 360p and by ~2 Mbps at 1080p. **Consequences:**

- Sweep at **1080p**, not at a low resolution, or the entire PRD range sits in the
  saturated region.
- Treat the low rungs as the informative ones. Codecs separate where bits are scarce.
- For a codec comparison meant to inform a real deployment decision, prefer a **camera
  export** or a standard clip (`download_files.sh` in the `webrtc-vmaf` clone fetches
  xiph.org sources). Real content has the spatial and temporal complexity the synthetic
  pattern lacks, and does not saturate.

The synthetic pattern's value here is that it is *exactly what the harness sends*, so a
VMAF number can be tied to a specific harness run. That traceability is the reason to
export it; its simplicity is the reason not to stop there.

---

## What VMAF can and cannot tell you

**It measures one thing: how closely an encoded file resembles its source, offline, on one
machine, using a perceptual model.**

### It can tell you

- Which codec preserves more of the picture at a given bitrate, on this content.
- How quality scales with bitrate for one codec — where the knee is, where it saturates.
- Roughly how much bitrate codec A needs to match codec B's quality.

### It cannot tell you

- **Anything about transport.** No latency, no jitter, no packet loss, no recovery, no
  keyframe behavior, no bandwidth estimation, no congestion response.
- **Anything about the SFU.** No forwarding, no simulcast layer selection, no subscriber
  behavior.
- **Anything about a real-time encoder.** `webrtc-vmaf` uses ffmpeg with WebRTC-*like*
  settings; the harness uses libwebrtc's actual encoders, which may be **hardware** ones
  with entirely different rate-control behavior. An ffmpeg `libaom-av1` result is not a
  measurement of what VideoToolbox or NVENC would produce.
- **Whether the codec is usable for teleoperation.** That is a latency-and-reliability
  question, and it lives in the harness.
- **What a human operator will perceive.** VMAF is a model trained on consumer video, not
  on teleoperation tasks. A score is not an operability judgment.

### The two result sets must not be conflated

| | VMAF sweep | harness matrix |
|---|---|---|
| measures | offline encode quality | live transport behavior |
| encoder | ffmpeg, WebRTC-like settings | libwebrtc, possibly hardware |
| network | none | real, optionally shaped |
| output | VMAF score | bitrate, G2G latency, fps, loss, QP |

A VMAF score and a harness `video_bitrate_bps` come from different encoders on different
paths. **Never put them in one table and never let one adjudicate the other.** VMAF says
"AV1 encodes this content better at 2 Mbps"; the harness says "AV1 at 2 Mbps had this
latency and this frame rate over this network". Both can be true; neither implies the
other.

As everywhere else in this harness, **no thresholds and no verdicts live in this tooling.**
`run_vmaf.py` emits scores and one measurement-validity warning. Any pass/fail judgment
belongs in the analysis layer, where changing it does not require re-running a sweep.
