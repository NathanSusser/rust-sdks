# Handoff — LiveKit Rust teleoperation test matrix

What exists, what it proves, what's still blocked, and what to do first.

## What's unblocked right now

Everything up to and including a live Tier 0 smoke test on your MacBook.
Follow `SETUP-AND-TESTS.md` from the top — it opens with exactly that
runbook, has been executed command-by-command against this checkout (not
just described), and every path/cwd/field-location issue found in review has
been fixed. Concretely, you can today:

- Build `teleop-harness` and run `cargo test -p teleop-test-matrix` (126
  tests green).
- Run `run_matrix.py --plan` / `--plan --tier0` / `--dry-run --tier0` with no
  network access at all — confirms the matrix expands correctly and shows
  wall-time estimates before you commit real session time.
- Run a live session against LiveKit Cloud via `.livekit-demo` credentials:
  session lifecycle, codec negotiation (including AV1, with no fallback —
  a failed AV1 publish is a hard error by design), encoder-tier auto-selection,
  glass-to-glass timing via in-band frame metadata, zero-jitter buffering,
  the `data_track_buf1`/`dc_reliable`/`dc_lossy` control-transport paths, and
  the full stats pipeline.
- Run `parse_runs.py` against real output and get a scored, four-verdict
  (PASS/FAIL/INVALID/OBSERVE) report with breakpoints and per-codec/per-tier
  tables, never pooling across codec/encoder_tier/buffering_mode/
  control_transport/ran_profile/path.
- Regenerate and score the full known-answer fixture suite (83 tests) with no
  live session at all, for testing report or `matrix.yaml` changes safely.

## What's blocked, and on whom

**On you, next:** the live Tier 0 smoke test itself. Nobody on this build
has run `teleop-harness` against a real SFU — per this project's scope, that
was deliberately left to you. `SETUP-AND-TESTS.md` §4 is written to make that
first run as cheap to diagnose as possible (`--validate-args` as a
zero-network pre-flight, explicit field-location notes for the two most
likely misreadings). If it fails, that's a finding worth reporting back, not
a sign the runbook is wrong — see "known boundary" in `SETUP-AND-TESTS.md`'s
closing section.

**On a Linux host with root (Tier 1):** the loss/jitter/uplink sweeps (T-2's
non-required cells, all of T-3, T-4's concurrency beyond 1, most of T-5's
fault injection), and clock sync approaching NTP-grade. This last one matters
more than it looks: **T-1, T-2, T-3, T-4, and Q-7 all score every run INVALID
without a valid clock offset** — only T-5 and V-0 survive on RTT alone. This
was a late, mechanically-derived correction during Phase 4 (not the original
"T-3 and Q-7 only" guess), and it means clock sync is close to a
precondition for the whole matrix rather than a nicety for one or two
suites. Budget for `chrony`/PTP setup before a full Tier 1 run, not after a
disappointing one.

**On external NVENC/Jetson hardware (Tier 2):** every AV1 result from Tier
0/1 is provisional until re-run here. Apple Silicon has no AV1 hardware
encoder, so software AV1 numbers on a MacBook describe bitrate efficiency
correctly but not encode latency, CPU load, or achievable fps — the numbers
that actually matter for whether AV1 is viable on a robot. `encoder_tier` is
recorded in every run and the analysis layer refuses to pool across tiers,
so this is a re-run, not a re-analysis, once the hardware exists.

**On the T-Mobile network team (Nishant Patel; capacity questions to Numan
Suri/Scott Jacka):** the RLC-AM-vs-UM hypothesis from the master prompt's §5
— whether aligning RAN discard/reassembly timers with the application's
zero-jitter-buffer behavior actually helps. This harness records
`ran_profile` but does not control it; testing the hypothesis needs a lab RAN
where those five parameters can be varied. Until then, every run's
`ran_profile` defaults to `unknown` and is `n/a` on loopback/lan paths, and
the report will not compare across profiles without saying so.

**Not this harness's job, ever:** manipulation sufficiency (Figure's call,
using the profile→bitrate→fps table T-1 produces), cell uplink capacity (a
real radio test), operator takeover timing.

## Recommended execution order

1. **Live Tier 0 smoke test** (`SETUP-AND-TESTS.md` §4) — one cell, ~60s,
   confirms the whole pipeline works before anything else.
2. **Full Tier 0 shaping-free subset** — `T1_video_floor`,
   `T2_loss_collapse`'s required AV1 cell, `Q7_latency_definition`
   (69 runs total per the current `--plan --tier0`, ~2.8h wall time). This is
   the largest amount of real signal available before any netem host exists.
3. **Stand up Tier 1** — Linux host, root, clock sync, then proceed straight
   to the full matrix (1017 runs, ~43.9h).
4. **Tier 2 re-runs** for every AV1 cell, once NVENC/Jetson hardware is
   available — these upgrade "provisional" findings to load-bearing ones.

## Known findings worth carrying forward, not re-discovering

- **Clock sync gates 5 of 7 suites**, not the 2 originally assumed. See
  above.
- **`enable_zero_playout_delay` is process-global** — `run_matrix.py`
  already handles this by batching runs per `(suite, buffering_mode)`, never
  spanning a `zero_jitter` boundary within one process. If you ever hand-roll
  an invocation outside `run_matrix.py`, respect this or a run labeled
  `zero_jitter` may silently execute with the default buffer.
- **`RemoteVideoTrack::subscribe_timing_events()` must be called before
  `NativeVideoStream` construction**, or G2G frame metadata arrives empty
  while everything else looks healthy. The harness gets this right and also
  has a backstop metric (`g2g_metadata_coverage_pct`, <95% ⇒ INVALID) in case
  this regresses under future refactoring — but if you ever reimplement any
  part of the subscribe path, this ordering is not obvious from the SDK's
  public surface and is easy to get wrong silently.
- **AV1 has no fallback path.** A failed AV1 publish is a hard error, not a
  silent substitution — unlike H265, which does fall back to H264
  automatically and is therefore excluded from this matrix entirely (an H265
  cell would otherwise silently become an H264 cell).
- **Software AV1 on Apple Silicon is not representative of NVENC/Jetson.**
  Bitrate efficiency carries over; encode latency, CPU load, and fps ceiling
  do not. Every report must say so, and the analysis layer refuses to pool
  across `encoder_tier` values so this can't be silently averaged away.
- **`buffering_mode` is locked to `zero_jitter`; the room-level playout-delay
  hint modes are retired** (2026-08-24). Measured Tier 0 runs put both hint
  modes at ~6ms regardless of a 100-400x difference in requested buffer, while
  `zero_jitter` measured ~2.9ms average and ~25.7ms target against the floor
  cell's ~43.2ms. The LiveKit robotics docs confirm why: a true 0/0 "isn't
  supported when using playout delay hints", so there was never a zero to
  reach through the room API. The matrix now aligns with LiveKit's robotics
  low-latency guidance. Full reasoning, measurements and consequences are in
  the amendment at the top of `MEASUREMENT-DESIGN.md`.
- **There is no validation gate.** `V0_playout_units` existed only to settle
  whether the hint API took milliseconds or 10ms units. With the hint modes
  retired the harness never calls that API, so the question is moot by
  construction rather than unresolved, and the suite is removed from
  `matrix.yaml`, `run_schema.json` and `parse_runs.py`. **Re-adding any hint
  mode must re-add the suite and its gate** — a hint value with no gate is
  exactly the unverified-unit failure the gate existed to prevent.

## Deferred, not dropped

- Several LOW-severity findings from the Phase 4 review were deferred as
  genuinely unreachable in practice rather than fixed defensively (documented
  inline in `parse_runs.py` near the relevant guards): `join_to_*_ms` when
  `run_origin_unix_us == 0` (a harness that never stamped its own start
  should fail loudly, not be guarded around), and frame dimensions of 0
  (already caught upstream by the video gates). Revisit only if a real run
  ever produces one — that would be evidence of a harness bug worth
  surfacing, not a case to code defensively for now.
- Five path-ish fields in `run_schema.json`
  (`control_samples_path`, `frame_timing_path`, `video_sample_path`,
  `stdout_path`, `stderr_path`) are neither written by `run_matrix.py` nor
  read by `parse_runs.py` — dead schema surface, harmless, worth pruning
  eventually but not urgent.

## Definition-of-done status

All nine items from the master prompt's §15 are met: the crate builds clean
under fmt/clippy(own code)/test with a changeset
(`.changeset/add_teleop_test_matrix_harness.md`); `MEASUREMENT-DESIGN.md`
maps every metric to a verified symbol or a resolved gap, states the
playout-delay resolution, states the audio decision (measured, scored
OBSERVE, not deferred), and names `reference_config`; `matrix.yaml` has all
axes including AV1/encoder_tier/buffering_mode/control_transport/ran_profile,
all thresholds traced to PRD clauses with a `source` column, and all six
suites; the mandatory AV1 cell is present in T-2 and Q-7; `--plan`/`--dry-run` work on macOS
with a correctly-filtered Tier 0 subset; `parse_runs.py` reproduces
hand-computed fixture values and scores all required failure fixtures
INVALID with correct reasons; this document and `SETUP-AND-TESTS.md` are
written and independently reviewed; every review pass's findings were fixed
or are listed above as deliberate deferrals with reasons.

Every phase gate (design, matrix definition, harness crate, analysis,
runbook) passed an independent review cycle — several took two or three
rounds to get there, and every round surfaced something real: a
self-contradictory baseline choice, silent counter-differencing bugs, a
process-global scheduling hazard, a CLI default that turned every real run
INVALID, and two broken paths in this very handoff's sibling document. That
pattern is the point of the review structure, not a sign anything was built
carelessly — treat future changes to this harness with the same discipline.
