# E1 — fixture verification

Host A's half of E1. Verifies the looped fixture before any cell runs against it,
because the plan's entire comparability argument rests on the source being
identical across cells.

Fixture: `ref.mp4` — 901 frames, 1600×1300, 30 fps, H.264 High, yuv420p — served
by `scripts/fixture-serve.sh` at `rtsp://127.0.0.1:8554/fixture`.

## Why mediamtx rather than ffmpeg alone

ffmpeg cannot serve this. Its RTSP muxer's `listen` flag is marked `.D.` —
decode-only — so ffmpeg can *receive* a push but cannot serve a reading client.
mediamtx serves; ffmpeg pushes into it with `-re -stream_loop -1 -c copy`.

`-c copy` matters: the same encoded frames are re-sent every pass, so the source
is bit-identical across cells rather than re-encoded per loop.

## Measured, not assumed

70 s captured from the served stream and hashed per decoded frame
(`-f framehash -hash md5`), which is 2.2 passes over a 30 s clip.

| Property | Result |
|---|---|
| Frames captured | 1989 |
| **Distinct frame hashes** | **901** — exactly the clip length |
| Frame *i* vs frame *i*+901 identical | **1088 / 1088 — 100.00%** |
| Best-matching lag over 880–920 | **901**, at 100.00% |
| Hashes seen more than twice | 187 — exactly `1989 − 2×901`, the partial third pass |

So the loop is **content-identical across the boundary**, with period exactly
901 frames and no drift.

## The property that matters most, and it was not the one being tested

**All 901 frames in the fixture are distinct.** Distinct hashes equals clip
length, so no frame repeats within a pass.

This is what makes frame-ID alignment *safe*. The publisher joins the loop at an
arbitrary point, so the mapping from harness frame ID to fixture frame index is
an unknown offset that must be **discovered per run, not assumed**. It is
discoverable precisely because every frame is unique: match one decoded sample
against all 901 fixture frames, and the offset is then fixed for the run
modulo 901.

And if alignment is ever wrong, PSNR **collapses** rather than quietly reading a
few dB low. With a fixture containing repeated frames — a static scene, a clip
with held frames — a misalignment could land on a visually similar frame and
produce a plausible number that is wrong. That failure mode does not exist here,
and the check that establishes it costs one hash pass.

## What this does not verify

- **Frame-ID alignment through the encode/SFU/decode path.** This measures the
  fixture as served. Whether the harness's frame IDs stay locked to fixture
  frames end-to-end is Host B's sampling work, and the offset discovery above is
  what closes it.
- **The 28.4 fps observed during capture** (1989 frames in 70 s). Consistent with
  RTSP connect time being counted inside the `-t` window; it is a property of the
  measurement, not evidence about the fixture, and it should not be quoted as a
  frame rate.
