# Run discipline — what the 4–5 Sep 2026 session cost, and the rules that came out of it

Two hosts, one night, a 5G teleoperation video rig. The session set out to find
why WebRTC's bandwidth estimator "settled at 1.2 Mbps on a 10 Mbps link". It
ended by establishing that the estimator was reporting the truth, that the
uplink had fallen roughly 70× partway through, and that nothing in the rig could
have detected the difference.

This document is the part worth keeping. It is not a summary of findings — those
live in `MEASUREMENT-DESIGN.md` and the run reports. It is the set of rules that
would have made the night shorter, written where the next person will hit them.

Each rule below cost something specific. The cost is stated, because a rule
without its incident gets optimised away by whoever reads it next.

---

## 1. The withdrawal ledger

Eleven claims were made and withdrawn in one session, between two hosts. They
are listed together, rather than distributed politely through the text, because
a reader who sees only the surviving conclusions will trust them more than the
evidence supports. The withdrawal rate is the honest answer to "how confident
are you".

The third column records *how* each was caught, because it is the evidence for
the second pattern below.

| Claim | Withdrawn because | Caught by |
|---|---|---|
| BWE decay over a run | Probes had no subscriber, so GCC had no receiver feedback at all | Other host's review |
| SFU was dropping frames | Never separated from encoder-side drops | Attributed from one host's memory; the other has no record of it — see below |
| Keyframe tail explained the p99 | Two independent refutations, from opposite ends of the pipeline. At the receiver, the worst frames group into one to three contiguous episodes per run rather than a cadence — A3's entire worst 1% is one episode, frames 62–179, spanning 0.0–3.9 s. At the encoder, `gopLength` and `idrPeriod` are both `NVENC_INFINITE_GOPLENGTH`, so there is no periodic keyframe to produce a periodic tail: the hypothesis was not merely unsupported but impossible | Re-derived from the data; the encoder half found afterwards, in seconds |
| The grant was being lost between components | Two refutations, of two versions of the claim. `available_outgoing` and `target_bitrate` agree to 5% in the realistic arm; and grant loss predicts stalls follow *idle* periods, where measured episodes follow *elevated* demand (A1 median demand ratio 1.21 into an episode) | One host each |
| A standing 2× `available`/`target` gap | Confined to arm 2b, a configuration already rejected; ratio pinned at 2.04 while both absolutes swung, so structural | Other host proposed it; own data settled it |
| "The estimator settles at 1.2 Mbps" | It never settles. It starts at 3.3 Mbps, collapses by t+20 s, and is still climbing at t+120 s | **Neither — see below** |
| "The estimator is wrong" | The uplink was 0.15 Mbps. It was measuring reality | **Neither — two curl commands, available all night** |
| "The scaler never ratchets back up" | It does, in three of six runs — but only between the bottom two rungs, never toward the request | Other host's step table |
| The `--log-end-frame-id` window was unreachable at 1 fps | Frame IDs advance at capture rate. The window closed on schedule; a dropped boundary frame hung the exit | Other host's review |
| Arm 3-alt: publish over the PTP cable as a good-link control | The video goes publisher → SFU → subscriber. The SFU is unreachable from a /30 with no route off it | Other host's review |
| arm 1's QP mechanism explains the A-series staircase | Opposite bitrate signatures. arm 1's bitrate falls 6.76 → 0.82 Mbps at flat bpp; A3 holds 9–10 Mbps while bpp climbs 0.30 → 2.00. Different links, hours apart | Other host's review |

Two patterns run through that list.

**Eight of the eleven were withdrawn because a quantity was assumed rather than
measured** — receiver feedback, link capacity, drop attribution, the routing
topology. In every case the measurement was cheap and available at the time.
The uplink that invalidated a night's conclusions was two `curl` commands away
from the first hour.

A specific form of that assumption appeared three times and deserves its own
name: **a mechanism established on one run was carried onto another run where it
had only been assumed.** arm 1's QP evidence was applied to the A-series, which
ran hours earlier, sustained 9–10 Mbps of delivered video where arm 1's delivery
fell to 0.82 Mbps, and shows the opposite bitrate signature. A capacity figure
taken once became a standing property of the link.
A subscriber's presence in one probe was assumed in the next. Runs are not
interchangeable, and the rig gave no way to tell — which is what §4 exists to
fix.

One row deserves note for what *could not* be established. The SFU-frame-drop
claim is recorded as withdrawn, but neither host can now evidence who made it or
on what basis: one has it in memory, the other has no record in logs or repo.
The correct response was to leave it marked as unverifiable rather than
reconstruct a plausible history for it — reconstructing it would have been the
same move the rest of this document is about, performed on the document itself.

### Withdrawing a number is not finished when the sentences containing it are

A retracted figure keeps working through everything computed **from** it, and
those derived figures do not contain the retracted phrase — so a text search
will not find them.

The instance: a report retracted its 10 Mbps uplink figure in one section while,
three sections earlier, describing a control run as "roughly 3.5% link
utilisation". That percentage was 0.36 Mbps divided by the very number being
retracted. The sweep had been for the phrase *"capacity to spare"*; nothing
containing that phrase was left, and the retracted number was still setting up
the whole argument.

It was fixed by restating the point as a ratio between two *measured delivered*
bitrates — Run B is a twenty-seventh of what A3 delivered — which makes the same
comparison while assuming nothing about what the link could have carried.

> **Audit by category, not by string.** Sort every remaining figure into: a
> distribution, a configured value, or a single instant quoted as a property.
> The third category is where retractions hide, and grep cannot see it because
> the derived figure shares no text with its source.

Applied to this document, that sweep found one: a claim that the A-series ran
"on a link four times faster" than arm 1's — a ratio with no stated provenance,
derived from exactly the kind of spot capacity reading being withdrawn. It now
cites the two measured delivered bitrates instead.

### The reinstatement reflex: a withdrawal is not a prompt for a replacement

Every claim withdrawn this session was immediately followed by reaching for a
substitute. Three times the substitute was offered as the *replacement evidence*
for the claim being retracted — and twice it was weaker than saying nothing:

| Withdrawn | Substituted | Outcome |
|---|---|---|
| "capacity to spare" | "9.6 Mbps delivered into a 10.0 Mbps measured uplink" | The substitute rests on the same spot capacity reading, withdrawn for the same reason |
| the same, in a second section | the same figure, plus "needs no correction" | Same |
| The A-series staircase mechanism | *nothing* — "the collapse proceeded while every signal the scaler is documented to respond to was healthy" | The only one that has held |

The survivor is the one that named what had become **unknown** instead of
substituting a new mechanism. It is also the more useful statement, because it
distinguishes an unidentified trigger from a mismeasurement — and only the
second would be our own fault.

> **When a claim falls, the honest replacement is usually a smaller claim or an
> explicit unknown, not a different claim of the same size.** A retraction that
> arrives with a ready substitute should be suspected: the substitute was
> generally chosen to preserve the conclusion rather than derived from what
> survived.

The A-series case shows what survives when the capacity term is dropped
entirely: A3 sustained 9.1–10.1 Mbps of real coded picture with zero packet loss
and collapsed anyway. A link that carried 9.6 Mbps carried 9.6 Mbps. No ceiling
needs to be known for that to be damning.

### Read the configuration for configuration questions

Twice, a question about **how the system was set up** was attacked with
measurements of **how it behaved**, when the setting itself was one command away
and decisive:

| Question | What was used | What would have settled it |
|---|---|---|
| Does keyframe cadence explain the latency tail? | An episode-grouping query over four runs' arrival data | `gopLength` and `idrPeriod` are both `NVENC_INFINITE_GOPLENGTH` — there is no periodic keyframe, so the hypothesis was impossible, not merely unsupported |
| Which timestamping tier is each host on? | 27,851 samples of PTP path delay, read nightly for a different field | `ethtool -T` on each end |

> **When a claim is about how the system was *configured*, read the
> configuration. Measurement is for how it *behaved*.** Using behaviour to infer
> configuration is slower, weaker, and in at least one case gave the wrong
> answer outright.

The sharper sub-case is the second row, and it is the one worth guarding
against: **data already being collected for one purpose can answer a different
question, and you will not notice, because you are parsing it through a filter
shaped by the first question.** The path-delay field sat beside the `rms` field
on every line that was read all night. Two hardware-timestamped ends on a
back-to-back cable sit in single-digit microseconds; the measured p50 was
74.6 µs with a 64 µs spread, which says plainly that the far end timestamps in
software. It was discarded on every line.

That mattered beyond the rig. PTP corrects for path delay by *assuming the two
directions are symmetric*, so systematic asymmetry survives the correction as a
constant offset the servo cannot see — the servo measures its own convergence,
not its accuracy. A masthead reading of "PTP synced, −246 ns" was `pmc`'s
`offsetFromMaster`: precision presented as accuracy, and on a mixed-tier link
wrong by two orders. The honest bound is tens of microseconds. Nothing downstream
changes — that is still three orders below a 19 ms floor — but the number was
the wrong kind of number.

**The A-series QP error is not an instance of this rule**, though it looks like
one. That was a *measurement* carried from the run where it was taken to a run
where it was assumed, which is the "mechanism travelled between runs" failure
above. Keeping the two separate matters: one is solved by reading a config file,
the other by refusing to reuse a finding across runs whose conditions were never
compared.

**The two that survived longest were the two both hosts agreed on** — and the
mechanism is worse than a shared prior. On those claims the hosts were not two
independent analysts at all. One host sent "the estimator settles at 1.2 Mbps";
the other reasoned forward from it and sent conclusions back; the first read its
own claim returning as corroboration. The check both believed they were running
was never run. It went through three messages and into a commit message before
anyone plotted the series against time.

So the rule is not "mutual agreement is a weak signal". It is narrower and
actionable:

> **Agreement counts only when the second analyst derived the claim from the
> data rather than from the first analyst's message.** Nothing in the two-host
> protocol distinguished those cases, even to the participants.

Cross-checking worked everywhere else that night — a broken test caught before
merge, a missing enum variant, a wrong window rule, an unrunnable experiment
design. Every one of those was a claim one host made alone and the other checked
against the artifact. The failure was confined to the beliefs both already held,
which is exactly where a review protocol feels most reliable and is least.

---

## 2. A run must record how it ended

**Rule.** `exit_reason` answers one question — how the process ended — and must
be derived from something observable at the moment it ended. It must never be a
constant, must never be inferred from a side effect, and must never be reused to
report the failure of a different question.

Three separate defects produced the identical outcome on the same day, reached
by three different routes:

| Mechanism | Effect |
|---|---|
| Hardcoded (`finish_from_csv(path, "completed")`) | Always reachable, always a lie. A run terminated by hand after overrunning its window by 19 minutes recorded a clean completion |
| Unreachable (`set -e` aborted the script when the binary exited non-zero) | The close never ran. Killed runs left `outcome: null` |
| Overwritten (a missing CSV replaced the status-derived value with `outcome_read_failed`) | A read failure destroyed the answer to an unrelated question |

The invariant they all break: **the run that died is the run with no record of
how it died** — and that is the run whose provenance gets argued about later.

The general form is worth stating on its own, because a fourth mechanism is
more likely than a repeat of one of these: *a field that answers one question
must not be reused to report the failure of a different question, because the
case where the second question fails is usually the case where the first answer
matters most.* A read error belongs in its own field; the exit status is known
from the process regardless of whether any CSV exists.

Two corollaries, both learned the same night:

- **Handle SIGTERM, not just SIGINT.** The rig stops runs with `kill`. Default
  disposition killed the process outright and closed no manifest at all, losing
  provenance for exactly the runs that had to be stopped by hand.
- **Flush before counting.** `finish_from_csv` measures the file on disk, and
  rows are flushed at most once a second. A manifest could report fewer rows
  than the CSV it describes.

> **Both hosts shipped a manifest bug that passed its own unit tests.** One
> wrote `finish()` and never called it; the other made it unreachable behind
> `set -e`. Test the wiring, not just the unit — the unit was correct in both
> cases.

---

## 3. The window rule

**Rule.** Publisher frame IDs advance at **capture** rate, independent of what
the encoder delivers. A `--log-end-frame-id` of 3600 comes due 120 s into a
30 fps run whether the encoder is producing 30 fps or 1 fps.

This corrects an earlier version of this rule which claimed the window becomes
unreachable when the frame rate collapses. It does not. Measured on arm 2b:
frame IDs 8 → 3569 over 119.2 s is 29.86 ids/sec while delivery ran at 1.12
rows/sec.

The real defect was narrower and worse. `FrameLogRange::reaches_end` tested
`end == frame_id` — equality against **one specific frame ID** — and
`record()` returned before consulting it for any frame outside the window. So
shutdown depended on that exact frame surviving the whole capture → packetize
pipeline.

At 30 fps with no drops, frame 3600 nearly always survives, which is why this
held up for weeks. Arm 2b encoded roughly one frame in twenty-six, giving the
end frame about a 4% chance. It lost. The publisher ran 19 minutes past its
window, still publishing into the room.

Fixed in `6f25679`: `>=`, evaluated before the containment gate.

**The transferable form:** any termination condition that tests equality against
a single event assumes that event is never dropped. Where the event travels
through a lossy pipeline, use a threshold.

---

## 4. Measure the link next to the run

**Rule.** Every run records uplink capacity at both ends, on **both hosts**.
A run without it cannot be compared to another run on anything bandwidth-related.

This is the control the session spent a night without. Every cross-run
comparison assumed link capacity was constant between runs separated by tens of
minutes. It was not: Host A's uplink fell from 10.0 Mbps to 0.14 during the
session while its downlink stayed above 26 Mbps and Host B's uplink, same
carrier and same room, still measured 12.8.

An estimator reporting 0.030 Mbps therefore looked like a bug for hours. It was
correct.

**Uplink specifically, and on both hosts.** A downlink figure looked healthy
throughout and would have deepened the confusion. A publisher-only figure would
have shown the collapse but not that it was *one-sided* — and one-sided is what
distinguishes a device or slice problem from the shared network. Neither host
could have reached the diagnosis alone.

### 4a. Check the instrument before believing the link

A surprising capacity figure is a measurement artifact until these three are
ruled out. Run them rather than re-deriving them:

| Check | What it rules out | Observed when sound |
|---|---|---|
| Payload size sweep, 2 / 4 / 8 MB | TCP slow-start truncating a short probe | 13.7 / 15.2 / 12.6 Mbps — no systematic bias with size |
| Pipe vs pre-generated file | `--data-binary @-` being the bottleneck | Pipe reads *higher* (13.0/12.0 vs 11.0/9.8) |
| `/dev/urandom` throughput | The entropy source capping the probe | ~3800 Mbps, three orders of magnitude clear |

### 4b. Know what two samples can and cannot say

`uplink_mbps_start` and `uplink_mbps_end` **bound the link at the endpoints.
They do not characterise it during the run.** Sampling continuously would have
the probe competing with the run for the exact resource under measurement, which
is what the before-and-after placement exists to avoid. The limit is stated
rather than instrumented around.

Two consequences:

- A run whose two readings differ by more than about **2×** should not be
  compared to another run on capacity at all. Host B's uplink swung 2.4× across
  repeated measurements minutes apart with nothing changed; one 30 s
  verification run on Host A read 0.026 then 0.157.
- Never write "the uplink during this run was X". Write "it was between X and Y
  at the endpoints".

### 4c. Noise tells you which differences you may interpret

The variance measurement is not only a caveat. It sets the threshold at which a
difference becomes interpretable. A 2.4× swing within one host means a 2×
difference *between* hosts would have been uninterpretable — and means the
observed 70× gap is far outside the noise and can be read as real.

Measuring your instrument's noise floor tells you which differences you are
entitled to interpret. Do it before interpreting any of them.

---

## 5. Post-mortem: run a3r1

**What was recorded at the time:** the run was invalidated because a previous
publisher overlapped Host B's new subscriber, so the subscriber measured two
streams. The response was a three-step publisher-stopped handshake before any
subscriber comes up.

**What actually happened:** the previous publisher had finished its measurement
window and should have exited. It did not, because of the boundary-frame defect
in §3, and it was still publishing into the room when the next run started.

The handshake is worth keeping as belt-and-braces. But it treats the symptom,
and a reader who takes it as *the fix* will leave the boundary bug in place —
and the boundary bug is what silently corrupted a run. Since `6f25679` the
publisher ends its own run and releases the room, which is the actual repair.

**The general shape:** when a run is corrupted by another process, ask why that
process was still alive before adding a protocol step to work around it.

---

## 6. Defects found in the runbooks themselves

Ten corrections to `PTP-RUNBOOK-HOST-A.md` and the harness docs, all found by
executing them rather than reading them.

| # | Defect | Correction |
|---|---|---|
| 1 | Paths given as `~/rust-sdks/...` | The tree is at `~/code/rust-sdks/...`. Every `cd` in Phases 9 and later is wrong as written |
| 2 | Phase 9: "It exits by itself at the end frame" | Was false — see §3. True only since `6f25679`, and for a different reason than the text implies |
| 3 | Troubleshooting sends a header-only CSV to the permissions entry | The file's own existence rules permissions out — see below. The cause is a frame-ID range that matched nothing |
| 4 | Phase 7 titled "(optional; skip for a one-off test)" | The systemd units are what make the rig survive a reboot. Not optional for a multi-day programme, and the one-off framing invites skipping it |
| 5 | CSV lacked resolution and bitrate columns | Added in `408acf2`. Without them the resolution staircase and the estimator trajectory are both invisible |
| 6 | Phase 4 covers NTP but never the CPU governor | A bursty 30 fps duty cycle never convinces `powersave` to ramp; cost 13 ms of capture→buffer latency with no symptom other than the latency |
| 7 | No instruction to run the full test suite before pushing | A filtered run passed while the shared branch was broken. Shared modules require the full unfiltered package suite |
| 8 | Definition of done requires `phc2sys` with `s2` on Host A | Host A is the grandmaster; it disciplines *from* its own clock. Absent `phc2sys` is correct there, and the checklist marks a correct rig as failing |
| 9 | Nothing warns that a `ptp4l` restart makes the restarted host's own sync check meaningless | The slave detects the gap loudly. The restarted host cannot — see below |
| 10 | The 90-frame startup exclusion discarded evidence differentially across arms | Not merely "it removed the cause" — it removed 85% of the worst frames from the loaded runs and 0% from the control. See below |

### Defect 3 — a header-only CSV has already told you the answer

The symptom is not a missing file. It is a file containing the header and
nothing else, and that distinction is the entire diagnostic.

`frame_log.rs` `create_csv` does `create_dir_all`, `File::create`, writes the
header and flushes — all at logger construction, before a single frame arrives.
So the artifact partitions the causes by itself:

| What is on disk | What it proves |
|---|---|
| No file at all | `File::create` failed — genuinely a path or permissions problem |
| Header, zero rows | `File::create` **succeeded**. Permissions are ruled out by the file's own existence; the frame-ID range matched nothing |

A header-only CSV is *positive evidence against* the permissions hypothesis, and
the runbook sends the reader at precisely the thing the artifact has already
exonerated. The file is telling you the answer and the troubleshooting table
talks you out of reading it.

### Defect 9 — after a restart, the restarted host cannot check its own sync

The original framing of this entry was wrong in three ways, and correcting it
turns an anecdote into a rule.

When Host A's `ptp4l` went down on 4 Sep, Host B's log shows the whole event:

```
19:19:24  port 1 (eno2): SLAVE to MASTER on ANNOUNCE_RECEIPT_TIMEOUT_EXPIRES
19:19:24  selected local clock c4efbb.fffe.32622d as best master
19:19:24  port 1 (eno2): assuming the grand master role
19:19:28  selected best master clock 345a60.fffe.5a6e7b
19:19:29  port 1 (eno2): UNCALIBRATED to SLAVE on MASTER_CLOCK_SELECTED
19:19:29  rms 5831 max 12953 freq +374977984 delay 70362
```

It **self-healed in five seconds** with no operator action. It was **not silent
on the slave**: a grandmaster has no master to measure itself against, so
`ptp4l` emits no `rms` lines at all while holding that role — nine seconds of
silence bracketed by state transitions that name the condition in plain English.
The existing "ptp4l reporting rms over 120 samples" pre-flight *fails* on this
rather than passing. And the residue was one excursion, not a divergence: 13 µs
against transport figures in the tens of milliseconds, three orders down,
decayed within a second.

The real defect is an asymmetry, and it points at the *other* host:

> **After any `ptp4l` restart, the restarted host's sync-quality check is
> uninformative until the window it averages over has fully elapsed since the
> restart.** A freshly started daemon has no `rms` history, so a check that
> counts samples over a window passes on a few seconds of convergence — and the
> restarted host is also the one that believes it is the grandmaster, and
> therefore never doubts itself.

The slave detects this trivially. The host that cannot is the one that just
restarted, which is the host most likely to be asked whether it is healthy.

### Defect 10 — an exclusion that acted differently on each arm

`START_FRAME=60` (later 90) excluded the first frames of every run, to keep
encoder ramp-up out of the statistics. Applied uniformly, defensible in
isolation, and stated in the script's own comment.

It was not uniform in effect. Worst-1% frames falling inside the excluded
window:

| Run | In window | Share |
|---|---|---|
| Run B (control) | 0 / 35 | 0% |
| A2-off | 3 / 33 | 9% |
| A1 | 9 / 34 | 26% |
| A2 | 29 / 34 | **85%** |
| A3 | 29 / 34 | **85%** |

Zero percent from the control; 85% from the two runs whose collapse the
programme was trying to explain. Every comparison drawn across that filter was
biased toward making the loaded runs look calmer than they were.

> **A filter that discards more evidence from the treatment than from the
> control is removing signal, not noise.** It is not enough for an exclusion to
> be defensible in isolation — it has to be checked for differential effect
> across arms, and this one never was.

### What these have in common

Defects 3, 6, 9 and 10 share a shape with the rest of this document: the rig
reports success while the measurement is wrong, or points at a cause the
evidence already rules out. Those are the expensive ones. A guard that warns
loudly is worth more than a fix that silently works, because the fix will be
reverted by a reboot and the guard will not.

Defect 10, the agreement failure in §1, and the discarded path-delay field are
the same error at three scales, and are the most transferable lines here:

> **A filter can feel sound because it was applied uniformly, when what matters
> is whether it *acted* uniformly.**

| The filter | Applied to | Acted on |
|---|---|---|
| The 90-frame startup exclusion | every run | two — stripping 85% of the worst frames from the loaded arms and 0% from the control |
| The two-host review protocol | every claim | all but the two both hosts already believed |
| A log parser reading `rms` | every `ptp4l` line, all night | one field — while the answer to a different question sat in the field beside it |

The third is the most insidious, because nothing was excluded on purpose. The
parse was simply shaped by the question being asked at the time, and it made a
fact that was present 27,851 times invisible.

Since the operator may be remote and without `sudo`, the publisher and
subscriber scripts now warn on both governor **and** EPP: `cpufrequtils`
persists the governor across a reboot but does not manage EPP, which is an
`intel_pstate` knob outside its scope and reverts to `balance_performance`. A
freshly booted machine can read `performance` while the setting that matters has
silently gone back.

---

## What this programme has not answered

Stated plainly, because a reader of this document and the run reports together
could come away thinking the investigation concluded. It did not.

**The deliverable is whether the teleoperation feed holds 1600×1300 at 30 fps
over this link. That is still unanswered.** Nothing in this session measured it.
What the session produced is the instrumentation to answer it: a rig that
records how each run ended, what the link could carry at both ends of it, which
encoder actually ran, and whether the host's power settings were where they
should have been. None of that is the answer. All of it is what makes the next
attempt at the answer worth trusting.

The withdrawal ledger in §1 is longer than the list of established findings, and
that ratio is the accurate picture of where the work stands.

## Still open

Recorded here rather than dropped, because both are one measurement away from an
answer and neither can be taken until the uplink recovers.

- **Did the *encoder* step up, or did the SFU switch layers?** The delivered-side
  up-steps are now confirmed independently: A2 shows six down-steps and three
  up-steps alternating 320×180 and 480×268, on the same exact 5.00 s clock, and
  A2-off matches. What is no longer in question is that delivered resolution
  rose. What remains is strictly the mechanism — the encoder raising its output,
  or the SFU selecting a different layer — and those runs predate the
  encoder-side resolution column, so the encoder's own sequence is unknown for
  them. On R1, where both sides were instrumented, the two agree on zero
  up-steps: consistent, but a run with no up-steps cannot confirm how an up-step
  happens. Needs a run that oscillates with both sides logged.
- **Does the estimator recover past 1.65 Mbps given longer than 120 s?** Arm 1
  was still climbing at cutoff and never plateaued. R1 was designed to answer
  this and could not, because its link had already collapsed. Needs a repeat on
  a link comparable to arm 1's.

- **Why did the A-series picture shrink while nothing was starved?** The encoder
  runs `NV_ENC_PARAMS_RC_CBR` with `enableFillerDataInsertion` never set
  (`h264_encoder_impl.cpp:225`). Denied padding, NVENC satisfies CBR the only
  remaining way — by lowering QP until the target is consumed — so ~10 Mbps of
  *real* coded picture on trivially compressible colour bars is the rate control
  working as configured. That accounts for the flat bitrate, the bpp climbing
  0.30 → 2.00 as resolution fell, and A3's frames carrying 6.7× the bytes of
  arm 1's at identical 640×360.

  What it does not account for is the staircase. WebRTC's quality scaler steps
  down on *high* QP; CBR at a 10 Mbps target on that content should have driven
  QP low. The picture stepped down four times on an exact 5.00 s clock while
  bitrate held, packet loss was zero, and QP pressure should have been absent.
  The honest statement is not "mechanism unknown" but the more specific **the
  collapse proceeded while every signal the scaler is documented to respond to
  was healthy.** Settling it needs a repeat with the encoder-side QP column,
  which did not exist during the A-series — reasoning about QP from an encoder
  config instead of from QP is the substitution this document exists to prevent.
