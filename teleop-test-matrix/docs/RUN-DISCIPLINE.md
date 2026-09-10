# Run discipline — what the 4–5 Sep 2026 session cost, and the rules that came out of it

Two hosts, a 5G teleoperation video rig, 4–6 Sep 2026. The programme set out to
find why WebRTC's bandwidth estimator "settled at 1.2 Mbps on a 10 Mbps link",
and why the picture collapsed from 1080p to 320×180.

Neither was a defect. The estimator was reporting a real uplink collapse the rig
could not see, and the resolution staircase was ordinary rate control on a test
source deliberately chosen to be incompressible — a fact misidentified for two
days by both hosts. Both answers were available on day one, in a `curl` command
and a log line respectively.

This document is the part worth keeping. It is not a summary of findings — those
live in `MEASUREMENT-DESIGN.md` and the run reports. It is the set of rules that
would have made the night shorter, written where the next person will hit them.

Each rule below cost something specific. The cost is stated, because a rule
without its incident gets optimised away by whoever reads it next.

---

## 1. The withdrawal ledger

Twelve claims were made and withdrawn across this programme, between two
hosts. They are listed together, rather than distributed politely through the text, because
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
| **The A-series published animated colour bars** | It published near-incompressible **noise**. Three "independent" proofs all reasoned about git — commit timestamps, the tree at a commit, a file in the repo — while the runs used a binary built from an uncommitted tree, which git cannot see. The run's own log said `pseudo-random noise` on line one | Other host asking what the proof was *about*; settled by the log |
| arm 1's QP mechanism explains the A-series staircase | Opposite bitrate signatures. arm 1's bitrate falls 6.76 → 0.82 Mbps at flat bpp; A3 holds 9–10 Mbps while bpp climbs 0.30 → 2.00. Different links, hours apart | Other host's review |

Two patterns run through that list.

**Nine of the twelve were withdrawn because a quantity was assumed rather than
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

### A negative from a search is not evidence of absence

Four times, a search returned nothing and the nothing was believed. The search
was wrong every time, and in one case it was *incapable* of reporting what it
was read as reporting.

| The search | Why the negative was false |
|---|---|
| `grep` for `--max-bitrate` in `publisher.rs` | Flags are derived from field names by clap, so the literal string appears nowhere. Returned "absent" for four flags, two of which exist |
| `grep -rn scale_resolution_down_by livekit/src/` | The field is declared in `libwebrtc/src` and marshalled in `webrtc-sys/src`. The scope was chosen by the same assumption that produced the question |
| `grep … \| head -3 \|\| echo "ABSENT"` | **The fallback can never fire.** A pipeline exits with `head`'s status, which is 0. This command cannot report absence — its silence means nothing at all |
| Parsing `ptp4l` lines for `rms` | The answer to a different question sat in the `delay` field on every line read |

The third is the one worth internalising, because it is not a judgement error.
Verified:

```
$ grep -rn 'nothing_here' /etc/hostname | head -3 || echo "FALLBACK FIRED"
  (silence — exit 0, from head)
$ grep -rn 'nothing_here' /etc/hostname      || echo "FALLBACK FIRED"
  FALLBACK FIRED
```

A blank result was read as a confirmed negative by a command structurally unable
to produce one.

> **A negative result needs at least as much verification as a positive one.**
> A positive announces itself — here is the line, here is the file. A negative
> is silence, and silence is also what a mis-scoped search, a wrong pattern, and
> a broken pipeline produce. Before believing "it does not exist", establish
> that the search *could* have found it.

What caught two of the four was **implausibility**: "`--max-bitrate` does not
exist" was unbelievable to someone who had been passing it all evening, which
prompted a second look. That is the practical trigger — a negative that would be
surprising if true has earned one more check, by a different method. The cost of
the one that survived: a finding that sent a reader to add a field, write FFI
conversions both ways and rebuild `webrtc-sys`, when all of that already existed
and worked, and the actual blockage was one private method.

### Arguments from one kind of evidence are one argument

The costliest error of the programme was defended by three proofs that looked
independent and were not. The claim was that the A-series published animated
colour bars. The proofs:

1. the noise mode "did not exist until commit 712526d at 18:56", and the runs
   were at 16:42–17:08
2. `--test-pattern 2` "would have been rejected" — the test at `712526d^` asserts
   it fails to parse
3. the run script "hardcodes `--test-pattern 1`"

Each is sound about the object it addresses. All three address **git**: a commit
timestamp, a tree at a commit, a file in the repo. The runs were direct binary
invocations from a working tree with uncommitted changes, which git cannot see —
and (3) is about a script those runs never used.

So it was one argument stated three times. Presented as three-way corroboration,
it bought two days of confidence, and a **fourth** git argument would have made
it more confident and no more correct.

What refuted it was one line of a different *kind* of evidence — the run's own
log, written by the process at the moment it ran:

```
Test pattern enabled: pseudo-random noise (near-incompressible) at 1920x1080
```

That line was on disk the whole time, in files already being quoted from for
resolution and frame rate.

> **Independence is a property of the *class* of evidence, not the number of
> arguments.** Before counting proofs, ask what kind each one is. Three
> arguments from the repository, three from one log, three from one stats
> column: each set is one argument. Reach for a different class — the artifact
> instead of the source, the process instead of the plan, the wire instead of
> the counter.

The corollary that cost the most here: **a claim proved about a script says
nothing about runs that bypassed it.** The A-runs were direct binary
invocations, recorded as such in `EXPERIMENT-PLAN` §0.2, in a document both
hosts had edited.

### Repetition fixes variance, and only variance

A comparison was proposed for a confirming repeat: AV1 delivered the same 1080p30
content on 0.137 Mbps where H.264 needed 0.443–0.521, a 3.2× difference that
looked like a coding gain. One run per codec felt like the weakness, so a second
of each was suggested to make it quotable.

It would not have. The two encoders were handed the same CBR target, both
undershot enormously, and each one's rate control then chose its own operating
point — H.264 settling near 0.24 of its quantiser scale, AV1 near 0.68. A large
part of that ratio is AV1 spending fewer bits to produce a worse picture.

> **A confound is invariant under repetition.** Repeating an uncontrolled
> comparison yields a tighter estimate of a quantity that does not mean what it
> appears to mean — and the tighter number is *more* tempting to quote, not less.
> Sample size answers "how precisely do we know this"; it never answers "is this
> the thing we think it is".

What would have made it a real comparison is holding quality constant — fixed QP
or CRF on both sides, or a quality metric on the decoded output so bits can be
compared at matched VMAF. That is a different experiment, not more of the same
one.

The proposal came within minutes of both hosts agreeing the rig cannot measure
codec efficiency, on the exact question just closed. Worth noting as the
reinstatement reflex reaching for a *number* rather than a mechanism.

### "The guard did not fire" and "the guard is broken" look identical from outside

A newly added inactivity timeout appeared not to work. It was very nearly filed
as a bug in the fix that had just been written. The guard was correct: it arms
only after frames have arrived, and the verification had killed the publisher
25 s in — while the run script's uplink probe still had ~15 s to go, so no frame
had ever arrived and the guard was right not to arm.

Nothing distinguishes those two states from outside the process. A guard that
stays silent because its precondition was never met and a guard that is broken
produce the same observable: silence.

> **When a guard does not fire, verify its precondition before doubting the
> guard.** Design the test around the arming condition, not around the wall
> clock — and be aware that anything which delays the first real event (a
> capacity probe, a connection handshake) shifts that condition later than the
> obvious moment.

The near-miss is the point: a correct piece of code was one step from being
"fixed" on the strength of a mistimed test.

#### The rule did not protect the person who wrote it

The same guard was later verified by killing a publisher with `SIGKILL`. It fired
correctly, and the fix was reported as working. But `SIGKILL` stops frames
*without unpublishing*, and a harness that ends a run properly does unpublish —
which breaks the stats loop on its track-sid check before the inactivity branch is
ever evaluated. The common ending was the one case the guard did not cover. It
cost 7:51 of a subscriber sitting on an empty room with 2,489 rows and no frames.

> **A timeout verified against a simulated failure has been verified against your
> model of the failure, not the failure.** Any test where you construct the fault
> is testing your imagination of it. Prefer the ending the system actually
> produces; where you cannot reproduce it locally, say the path is unverified
> rather than treating "my simulation passed" as coverage.

Then, fixing it, the same author armed the replacement test on a 20 s publisher
window that expired during a ~30 s subscriber pre-flight — so zero rows arrived,
neither guard armed, and a working fix looked broken. That is the arming-condition
rule above, hit twice more within the hour by the person whose bug produced it.

Three rules in this document have now been walked into by the person who wrote
them: `pkill -f` by its author, the arming condition twice by its subject.

> **A rule does not protect its author.** The interval between writing one and
> breaking it can be under an hour. Rules are worth writing anyway — but a written
> rule is a thing to check against before acting, not a hazard you are now immune
> to.

### `pkill -f` matches the shell that invoked it

Hit independently by both hosts in the same programme, costing two cycles each
time. A pattern-matching kill sees its own invocation, because the shell's
command line contains the pattern:

```
pkill -f 'room-name my-test'      # kills the publisher AND this shell
pgrep -f 'release/publisher'      # counts wrapper shells as matches
```

The failure is loud but illegible — the shell dies, exit 144, no output — and it
looks like the command produced nothing rather than like it killed the wrong
process. It also silently inflates every `pgrep -c` used as a "stray process"
check, which reported 2 stray publishers on a host that had none.

> Match on the binary path and exclude interpreters (`pgrep -af … | grep -v
> '/bin/bash'`), or capture the PID at launch and signal that.

### A binary can be stale without any of its own sources changing

The third form of the staleness trap, after "never rebuilt" and "rebase moved
the mtimes". A subscriber binary was newer than every file in its own crate, and
was still stale: the change was three files under `webrtc-sys/src/nvidia`, a
dependency it links. `git checkout` only bumps mtimes on files it actually
changes, so a comparison scoped to the crate's own directory passes every time.

> **Check mtimes against the dependency tree, not the crate.** Better, don't
> check at all — rebuild, and verify the artefact instead: does the binary
> contain the new symbol, does `--help` list the new flag, does the CSV header
> carry the new column.

All three forms produce the same outcome — a run that measures code nobody
believes is running — and none is visible in the run's own output.

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

#### And reading the right file is not the same as reading it

The `scaling_settings = kOff` line sits a few lines below the `qp_ = -1`
assignment. Both hosts had that exact function open, in that exact file, while
diagnosing the QP defect — and neither looked down.

So this is not a failure to consult the configuration. The configuration was
open on screen. It is a failure of *scope within* the thing being read: having
decided the question was "does this report QP", the adjacent line answering
"would anything act on it" was invisible.

> **A search shaped by a conclusion stops at the conclusion.** When a
> configuration line explains a defect, read what surrounds it before believing
> the explanation — particularly the lines that would say whether the thing you
> think is broken was ever switched on.

The cost: a causal claim published twice, in a commit message and to the
operator, that one adjacent line refuted.

#### The limit of that rule: the source is only the configuration you wrote

Applying the rule produced a confident wrong answer within hours of its being
written, and the correction is a genuine boundary on it rather than a caveat.

The claim was that NVENC filler-data insertion is off, on the grounds that
`enableFillerDataInsertion` is assigned nowhere in the tree. The search was
sound — re-run unscoped, all file types, with a positive control first to prove
it could find anything at all. The literal result holds: the field is assigned
nowhere outside the two vendor-header lines that declare it.

The inference does not follow, because the encoder does not build its
configuration from zero:

```
NvEncoder.cpp:169   memset(encodeConfig, 0, sizeof(NV_ENC_CONFIG))
NvEncoder.cpp:201   nvEncGetEncodePresetConfig(...)          <- asks the DRIVER
NvEncoder.cpp:203   memcpy(encodeConfig, &presetConfig.presetCfg, ...)
h264_encoder_impl.cpp:207   presetGuid = NV_ENC_PRESET_P4_GUID
```

The struct is zeroed, the driver's preset is copied over it wholesale, and only
selected fields are overridden afterwards. `enableFillerDataInsertion` is not
one of them, so its value is whatever NVIDIA's P4 preset sets — and this source
tree cannot show that.

> **"Never assigned in our sources" and "off" are different claims.** Where a
> preset or a default is fetched at runtime from a component outside the
> repository, the source is not the configuration; it is only the part of the
> configuration we wrote. Reading it gives a confident answer about the wrong
> object.

This is the rule above eating itself one step along: the search was right, the
reading of the search was right, and the inference from source to system
behaviour was wrong, because here the configuration *is* the behaviour of a
driver. What would settle it needs a machine, not a repository — read the live
config back after initialisation, or count filler NAL units in a captured
bitstream.

What survives the retraction, stated separately so it is not carried off with
it: the bytes reached the decoder and cost time proportional to their size
(r² = 0.81 across sixteen cells), which rules out RTP padding, since padding is
dropped at depacketisation and never reaches a decoder. The open question
shrinks to whether a *minority* of those bytes were filler rather than
coefficients — a smaller claim than the one withdrawn, which is what the
reinstatement rule predicts. And the CBR explanation is untouched either way:
CBR obliges the encoder to reach its target, and whether it gets there with
coefficients or partly with filler, it is still spending ten megabits on colour
bars because it was told to.

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

**What made the working half work is worth naming, because it was accidental.**
Each host read the other's claims adversarially and its own confirmatorily, and
the difference was not care or competence. One host found the other's
`scale_resolution_down_by` error inside a minute of reading it, while missing a
faulty inference of its own for hours *in the document it was writing at the
time*. The same asymmetry appears in both directions all night.

Two consequences, one practical and one to be wary of:

- The only mechanism either host would now trust is the one they ran by
  accident: **each checks the other's work, and neither checks their own.**
  Self-review was not weaker here. It was close to absent, while feeling
  identical from the inside.
- Auditing primes you to believe absence. A confident negative feels like a
  *result* when you are looking for defects, rather than the surprise it should
  be — which is how the `scale_resolution_down_by` error survived a pass whose
  entire purpose was catching that class of thing.

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

- **RESOLVED 6 Sep — the A-series picture shrank because the content was
  near-incompressible noise, and nobody knew it.** The runs were published with
  `--test-pattern 2`, not the animated bars every analysis assumed. Once that is
  known the mechanism is ordinary: 1080p noise costs ~1.4 bpp, a 10 Mbps grant at
  1080p30 offers 0.16, so the encoder quantises to the H.264 ceiling, the scaler
  sheds pixels on a 5.00 s clock until bits-per-pixel is affordable, QP relaxes,
  and it stops.

  Replicated as R4 on a link with 2.5x the headroom, and the replication is what
  makes it settled rather than plausible:

  | | A3 (4 Sep, ~10 Mbps up) | R4 (6 Sep, ~25 Mbps up) |
  |---|---|---|
  | staircase | 1080 → 720 → 540 → 360 | identical |
  | terminal rung | 640×360 | 640×360 |
  | delivered | 9.59 Mbps | 10.02 Mbps |
  | bits per pixel | 1.39 | 1.45 |
  | decode p50 | 3.65 ms | 3.63 ms |
  | packets lost | 0 | 0 |

  QP — the column A3 never had — reads **51, the ceiling, before every
  down-step**, relaxing to 38 only on reaching a rung it can afford.
  `quality_limitation_reason` is `Bandwidth` for 3406 of 3414 samples.

  > **Correction, 7 Sep — the component was misattributed.** This entry
  > previously said WebRTC's *quality scaler* sheds the pixels. It does not, and
  > cannot. QP-based quality scaling is disabled across **every hardware encoder
  > in the SDK** — eight assignments, no exceptions, none of them `kOn`:
  >
  > ```
  > nvidia/h264_encoder_impl.cpp:411    nvidia/h265_encoder_impl.cpp:333
  > nvidia/av1_encoder_impl.cpp:432     jetson/h264_encoder_impl.cpp:374
  > jetson/h265_encoder_impl.cpp:274    jetson/av1_encoder_impl.cpp:393
  > vaapi/h264_encoder_impl.cpp:268     passthrough_video_encoder.cpp:333
  > ```
  >
  > Every codec, every vendor, including the Jetson paths that production
  > hardware would use. Neither NVENC path adapts geometry itself either —
  > checked, they never write `encodeWidth`/`encodeHeight` after init, only
  > reconfigure rates — so the adaptation happens above them in libwebrtc,
  > driven by something other than QP.
  >
  > **Every measurement above stands**; only the name of the component is wrong.
  > What replaces it is deliberately left blank rather than filled with a second
  > guess: three claims have already been withdrawn to the reflex of answering a
  > retraction with a same-sized substitute. Establishing the real driver means
  > reading libwebrtc's adaptation path, not inferring it from our own logs.

  **Bandwidth is excluded by direct measurement, not by argument.** The encoder
  was granted its full 10 Mbps cap into a link with 24–30 Mbps available, took
  9.8 of it, and collapsed anyway. Two and a half times the headroom changed
  neither the rungs nor the destination. The scaler responds to content cost
  against its grant, and the link never entered into it.

  The sentence this replaces — *"the collapse proceeded while every signal the
  scaler is documented to respond to was healthy"* — was false. It was written
  about a run whose content had been misidentified, and it survived because both
  hosts believed the misidentification.

---

## For whoever is next at a machine

The section above is organised by open question. This one is organised by what a
person physically present can do, in the order worth doing it. Everything here
needs someone at a keyboard; nothing here can be done remotely, and the first
item is the only one that moves the actual deliverable.

| # | Action | Needs | Why this order |
|---|---|---|---|
| 1 | **WAN ethernet control run.** Publish over wired ethernet instead of 5G, same everything else. | A cable. No uplink. | Decides whether the staircase happens at all with the radio out of the path. If it still does, the 5G link was never the story and two days of link diagnosis were beside the point. Cheapest result that can redirect the whole programme. |
| 2 | **Read the live NVENC config back after `nvEncInitializeEncoder`**, and log `enableFillerDataInsertion` and the preset's rate-control fields. | Host A, a build. No uplink. | Settles whether ~10 Mbps of colour bars was coefficients or partly filler — the one open question about the byte counts, and it is a logging change rather than an experiment. |
| 3 | **Check the SIM or slice for a volume cap.** | Carrier portal. | Host A's uplink fell ~70× and stayed there for over an hour. Nothing in the rig can see this and no run is comparable across it. |
| 4 | **Repeat A3 with encoder columns** — QP, `quality_limitation_reason`, encoded resolution. | A link comparable to the A-series — check `uplink_mbps_start` before trusting the run, not after. | The central open question: why the picture stepped down while every signal the scaler responds to was healthy. Gated on the uplink recovering, which is **not** the same as 3 being resolved: the link may recover on its own, and 3 may come back "no cap" while the link is still bad. Do not wait on 3 if the uplink is healthy. |
| 5 | **Persist EPP on both hosts.** | Root on each. | Governor survives reboot on both; EPP survives on neither. The obvious check passes and the subtle one does not, which is the failure mode hardest to notice. |

**Not on this list, deliberately:** the arms in `EXPERIMENT-PLAN.md`. Two of six
cannot be run as specified, and its replication design assumes a link capacity
that is stationary across forty minutes — which this programme measured moving
2.4× in minutes. Those are design decisions for the operator, not repairs, and
they are recorded in that document's §0.4 rather than fixed.


---

## 7. A capability can vanish from a build without any source changing

`webrtc-sys/build.rs` compiles the NVENC encoders only if `cuda.h` is found under
`$CUDA_HOME/include`, defaulting to `/usr/local/cuda`. On this machine `cuda.h`
lives in `/usr/include`, so a build without `CUDA_HOME=/usr` silently produces a
binary with **no hardware encoder at all**. The only signal is one `cargo:warning`
among hundreds of lines.

Three runs were completed and analysed on OpenH264 before anyone noticed. One of
them showed a four-rung resolution staircase, which is a *software* encoder's
quality scaler — the hardware paths all set `ScalingSettings::kOff` — and would
have been pooled with the NVENC staircase runs.

`CUDA_HOME` is not declared in `cargo:rerun-if-env-changed`, so setting it does
not invalidate the cached build. Correcting the variable appeared to change
nothing until `build.rs` was touched.

> **Read the encoder implementation out of the run's own stats, every run.**
> `video_out.encoder_implementation` is one string and it is the difference
> between measuring the deliverable's encoder and measuring a fallback. This is
> the same rule as reading the source out of the run's own log, and it failed the
> same way.

**Related defect, not yet fixed:** `--encoder nvenc` silently fell back to
OpenH264. The harness deliberately makes an unopenable camera fatal, because "a
run labelled `camera` that actually ran the pattern would be pooled with pattern
runs and could not be detected afterwards." An explicitly requested encoder
deserves exactly the same treatment and does not currently get it.

## 8. On this link, sequential runs are not a comparison

Four runs minutes apart, same room, same content, same cap, differed in their
congestion-control grant by **7.3x** — 0.492, 0.548, 0.732 and 3.600 Mbps. Every
run spent essentially its whole grant (ratios 0.74 to 1.01), and QP tracked the
grant rather than anything under test.

An A/B whose arms run sequentially on this link measures the link. Any comparison
must either interleave its arms and repeat, or match cells on the grant they
actually received and discard those that cannot be matched.

## 9. The startup order was the defect, and it looked obviously correct

The SFU deletes a room once it has been empty for roughly 60 seconds. Our protocol
was "subscriber up first, then publish", which guarantees exactly that condition:
the subscriber joins an empty room and starts a garbage-collection clock against
itself. When the publisher arrived late, the room had been deleted and the
publisher created a *new* room instance with the same name and a different SID.

From inside the subscriber this is indistinguishable from a receive fault. It is
connected, its arguments are right, it has no participant filter, and it receives
nothing — because it is attached to a room that no longer exists.

Four runs died of this. One, `livecam-2500k`, sat in the manifest for a day as
"cause NOT established". Every run that worked, worked because the publisher
happened to start inside the window.

> **Publish first, then subscribe.** The publisher needs no subscriber to start,
> so the room is never empty and the clock never runs. Where the order cannot be
> inverted, the subscriber must join within the empty-room timeout, which is
> fragile and depends on pre-flight duration.

Two things generalise past this SFU:

- **A rig that works most of the time is evidence about your timing, not about
  your protocol.** Both hosts held "subscriber first" from the first run and
  neither questioned it, because it kept appearing to work.
- **The assumption that never gets stated is the one that never gets checked.**
  Startup order was never written down as a decision, so it was never a candidate
  when runs failed. Every failure was investigated as a receive problem.

The subscriber compounded it by not handling `RoomEvent::Disconnected` at all: it
logged one line and waited indefinitely, and both of its liveness guards arm only
once frames have arrived, so neither could fire on a session that never delivered
one. That is the fourth guard in this programme to assume the session was still
alive — after end-of-window, silence, and clean unpublish.

## 10. A pin that removes a confound can remove the measurement with it

`LK_PIN_BITRATE_TO_MAX` was added so a bitrate sweep's cells measure their cap
rather than whatever congestion control granted. It does that by setting
`x-google-min-bitrate` to the cap, which stops the estimator lowering the
allocation.

On a link with headroom that is correct. On a degraded link it is destructive: the
sender pushes the cap into a path that cannot carry it, a third of the packets
become retransmissions that also fail, the SFU asks for keyframe after keyframe,
and nothing coherent arrives. Measured on a 0.63-1.28 Mbps uplink:

| cap | retransmitted | keyframes | outcome |
|---|---|---|---|
| 0.5 Mbps | 0.1% | 6 | 3712 frames received |
| 5 Mbps | 31.7% | 22 | one keyframe, then silence |
| 10 Mbps | 38.4% | 44 | one keyframe, then silence |

Unpinned, those cells would have stepped down and produced a resolution ladder,
which is data about what the link delivers. Pinned, they produce nothing.

> **A control that forces the independent variable also forces the system past the
> point where it can respond.** Whatever the system would have done instead is the
> measurement you lose.

**And a drowned cell passes the admission gate perfectly.** The gate checked that
the grant held at the cap. It did — the pin held it there. What the gate could not
see was whether any of it arrived. A third test is therefore required: reject any
cell whose retransmission rate exceeds 5%. Tonight's data separates without a
judgement call, 0.1% against 31-38%.

The cheaper fix is upstream of all of it: **probe the uplink immediately before
each cell and skip any cell whose cap exceeds it.** That is a gate rather than an
analysis, it costs seconds, and it would have skipped ten of fourteen cells before
an hour was spent on them.

## 11. Matching the sampling window changed a divergence into an agreement

The first cross-host quantiser comparison this programme has produced, on
`e2r-500k-r1`:

| | n | p5 | p50 | p95 |
|---|---|---|---|---|
| encoder QP, Host A, 1 Hz polls | 163 | 14.1 | 26.6 | 42.6 |
| decoder QP, Host B, per frame | 3659 | 14.7 | **30.0** | 42.4 |
| decoder QP, resampled to 1 Hz | 131 | 17.2 | **28.2** | 41.8 |

Compared naively the medians differ by **+3.4 QP**, which reads as the received
bitstream being materially worse than the sent one. Resampled to the same 1 Hz
window the difference is **+1.6**.

Host B's column is an interval mean repeated across the frames of that interval —
3659 rows carry only 124 distinct values — so the per-frame series weights each
interval by its frame count. Comparing it against a per-interval series compares
the weighting as much as the quantiser.

This is the third instance in one day of the same error: a sampled PSNR against a
full-clip PSNR, a windowed p50 against a full-run mean, and now a per-frame series
against a per-interval one. Every one pointed somewhere interesting and every one
dissolved on matching the populations.

> **Before explaining a discrepancy between two statistics, check that they
> describe the same population.** It is the cheapest hypothesis and it has been
> right three times out of three.

---

## 12. GCC is removed from this rig, permanently, by operator decision

Google Congestion Control's bandwidth estimate was the binding constraint on almost
every run in this programme, and it was not tracking the link. Operator ruling
after seeing the evidence below: **GCC is not good at BWE here; keep it removed.**

### The evidence

On a 10 Mbps cap with a link that measured 9.7 Mbps immediately beforehand, the
grant did this:

| t | grant | resolution | limitation |
|---|---|---|---|
| 0 s | 1.816 Mbps | 1600x1300 | none |
| 48 s | 5.151 | 1600x1300 | none |
| 81 s | 3.421 | 1600x1300 | none |
| **84 s** | **0.070** | 1600x1300 | none |
| 163 s | 1.694 | 800x648 | bandwidth |

**A 50x drop in a single one-second poll**, then recovery at roughly 20 kbps/s —
seven minutes to return to 9 Mbps, in a 165 s run. GCC's delay-based estimator
decreases multiplicatively at ~0.85 per interval; this is not that mechanism
behaving normally. The resolution staircase followed the grant collapse. It did not
cause it.

Across the unpinned adaptive sweep the delivered rate had no relationship to the
cap at all — 5 Mbps sent 0.27 while 2 Mbps sent 0.36 and 10 Mbps sent 1.92, all at
zero retransmission. A link ceiling makes every cap above it converge; these
scatter.

### The three overrides, and what each was doing

1. **A hardcoded start ceiling.** `MAX_START_BITRATE_KBPS = 1000` in
   `peer_transport.rs`: ask for 10 Mbps, start at 1, earn the rest by probing. Now
   `LK_MAX_START_BITRATE_KBPS`; unset, behaviour is unchanged.
2. **The estimator's floor.** `x-google-min-bitrate` pinned at the cap via
   `LK_PIN_BITRATE_TO_MAX`, so the allocation cannot be lowered.
3. **The degradation policy, which nobody chose.** The SDK defaults a *camera*
   source to `MaintainFramerate` — hold frame rate, **sacrifice resolution**. That
   is the resolution staircase, and it sits above any scaler:
   `scaling_settings = kOff` on all eight hardware paths never disabled it, because
   it was never the scaler. `--degradation locked` sets
   `MaintainFramerateAndResolution`; quality is then the only axis left, and it
   shows up in QP where it can be read.

### The result

10 Mbps offered, all three overrides applied: **147.2 MB over 152 s at 6.8-9.4
Mbps, 1600x1300 throughout, 29 fps, QP 15-31, zero resolution changes.** The
comparison run twenty minutes earlier, GCC intact, was at 300x240 and 0.03 Mbps.

### What we now own

> **Congestion control exists to stop a sender congesting a path it shares.**
> Removing it moves that responsibility to us. This is defensible on a dedicated
> link whose capacity is known out of band, and it is what the operator has
> decided. It is not a general default, and a future reader should not carry this
> configuration onto a shared link without knowing what it disables.

The measurement cost is also real: with the pin engaged, a cap above what the link
carries produces a drowned stream rather than an adapted one, so the retransmission
gate in section 10 is mandatory alongside this, not optional.

---

## 13. The paired-run protocol, and why each clause exists

Every clause below was bought by a specific failure earlier the same night.

1. **Agree a wall-clock start epoch at least 30 s in the future**, named as a Unix
   integer in the message that proposes it. Both hosts arm against that number,
   never against message arrival or a verbal "go". Cost of not doing this: a
   publisher moved rooms while the subscriber was mid-join, and four runs died to
   empty-room deletion because the two sides arrived at different times.
2. **Each host's pre-flight runs INSIDE the lead, not after the epoch.** Host B
   starts its runbook at T-45 s so its uplink probe and manifest complete before T
   and the join lands on the epoch. A host that starts *at* T joins at T plus its
   own setup time, which is arming on the epoch plus an unmeasured constant.
3. **Each host runs its side in a subagent.** Main sessions coordinate; subagents
   execute and cannot be distracted mid-cell.
4. **Confirm epoch AND duration as explicit values before either side arms.**
   Tonight the epoch travelled as a number and the duration travelled in prose, so
   a 300 s publisher was paired against an 1800 s subscriber. The inactivity guard
   absorbed it — the subscriber exited 3 at publisher stop plus 10 s, with the full
   300 s captured — but a 30-minute publisher against a 300 s subscriber would have
   cost the whole cell. **Synchronising the start is not synchronising the run.**
5. **A change to either value after arming is an abort and re-agree**, never a
   mid-flight adjustment. Relaunching to change one argument misses the epoch,
   which is the thing the protocol exists to protect.
6. **Both hosts exchange CSVs when the run ends**, and reports are generated from
   the pair. A publisher-side figure without its receive-side counterpart is not a
   measurement of what the operator sees — see §14.

### An artefact this creates, and how to read it

A duration mismatch leaves the shorter side's `--log-end-frame-id` unreachable, so
its CSV stops short of the frame ID the script aimed at. **That gap is bookkeeping,
not loss.** The arrived-against-emitted panel takes `published` from the frame-ID
span actually observed and is unaffected, but a reader comparing the last frame ID
to the script's target would see a fabricated shortfall of tens of thousands of
frames.

## 14. Send-side health is not delivery, and the rig never measured the difference

The publisher reported 147.2 MB sent, 1600x1300 throughout, 29 fps, no resolution
changes. All true. The receiver got this:

| stage | frames | lost above |
|---|---|---|
| published | 2561 | — |
| **arrived** | **575** | **1986 — 77.5%, in the network** |
| decoded | 575 | 0 |
| drawn | 373 | 202 |

**77.5% of frames never arrived.** Transport was 95.8% of end-to-end latency, p50
472 ms rising to 2058 ms — a queue filling as well as overflowing, which is what a
pinned sender does to a bottleneck that cannot signal back-pressure.

This is not a reporting error from one night. **No publisher-side figure in this
programme has ever had a receive-side counterpart**, from the first run: one host
reported bitrate as though it were delivery, the other held the received/decoded
counters and never differenced them against the publisher's frame IDs. Both halves
existed and nobody joined them.

> **Every publisher-side number needs its receive-side pair before it means
> anything to an operator.** "The encoder held 1600x1300" and "the operator saw
> 1600x1300" are different claims, and only one of them is about the deliverable.

---

## 15. The link was rate-limited at the subscription the whole time

Two modems, same room, same tower, same night:

| | Host A | Host B |
|---|---|---|
| APN | `fast.t-mobile.com` | `fast.t-mobile.com` |
| operator | 310260 T-Mobile | 310260 T-Mobile |
| access | 5gnr, connected | 5gnr, connected |
| RSRP | −89.00 dBm | −88.00 dBm |
| RSRQ | −11.00 dB | −11.00 dB |
| SNR | 28.50 dB | 26.50 dB (**worse**) |
| **uplink** | **1.1 Mbps** | **15.3 / 15.9 / 16.9 Mbps** |

Identical APN, identical registration, Host B on marginally worse SNR, and **15x
the uplink**. Neither the air interface nor the APN can produce that. It is a
subscription-level limit on Host A's line.

### It explains every open thread at once

    uplink capped near 1.1 Mbps
      -> a 1.62 Mbps stream overshoots by ~47%
      -> ~40% of packets dropped before reaching the SFU
      -> the SFU NACKs; retransmits add load to the SAME saturated uplink
      -> repairs arrive outside the 300-packet reorder window, discarded "too old"
      -> the SFU never completes those frames and cannot forward them
      -> the receiver sees 78% of missing frames with no sequence gap, 47% frame loss

The 47% overshoot and the 47% frame loss are the same number. Retransmission was
not merely failing, it was **adding load to a saturated link** — the repair
mechanism was part of the collapse.

### What this invalidates

- **The cap sweep found double-digit retransmission at every rate including
  2 Mbps and no clean rate anywhere.** Of course it did: the ceiling was 1.1.
- **The "7.3x capacity swing" recorded as radio variability** was measured on a
  throttled line.
- **The 0.14 Mbps hour on 4 Sep** was read as a radio collapse.
- **Every conclusion about congestion control, pacing, keyframe storms and the
  SFU** was drawn from a sender that was over capacity in nearly every run.

> **Establish the subscription's rate limit before characterising anything above
> it.** We spent a programme measuring an encoder and a congestion controller
> against a ceiling nobody had checked, and read the ceiling's effects as
> properties of the encoder, the estimator, the SFU and the radio in turn.

The diagnostic is two commands and neither needs the rig:

    mmcli -m 0 --signal-setup=5 && mmcli -m 0 --signal-get   # RSRP/RSRQ/SNR
    mmcli -m 0 -b 0 | grep -i apn                            # APN

A second line on the same tower is the control. Without one, good signal and poor
throughput look like a radio problem and there is nothing to compare against.

---

## 16. An older LiveKit rejects data TRACKS, and the symptom names nothing

Pointing this harness at an older LiveKit deployment fails like this:

    signal connection failed on v1 path: Handshake { status: 404 }
    v1 path not found (404), falling back to v0 path
    run failed: track publish failed: control data track: Publish data track timed-out

The harness publishes a **control data track** by default
(`--control-transport data_track_buf1`). A server with no v1 signal path does not
support data tracks, the publish times out, and **the publish dies before any video
leaves the host — so the room is never created at all.**

From the subscriber's side that is invisible in the worst way: it queries the server,
finds no room with the expected name, and has no reason to suspect a *data track* is
the cause. The first attempt at the overnight sweep lost its first cell to this and
would have lost all sixteen.

> **`--control-transport dc_reliable` is the fix.** The legacy data channel works on
> deployments that predate data tracks. Check it first whenever a publish times out on
> a server that answers 404 on the v1 signal path.

**And a run whose publish fails must stop the campaign, not continue.** Sixteen cells
into a server that rejects every publish produces sixteen empty results and one
morning wasted. A cell refused for being *late* is different and the sweep should
carry on past it.

## 17. `pkill -f` is worse than recorded, and the usual workaround does not fix it

§7 already says a pattern-matching kill sees its own invocation. That is not sharp
enough, and both hosts have now been caught after reading it.

The bracket trick — `grep '[o]vernight.sh'` — stops **grep** matching itself. It does
**not** stop the pattern matching **the parent shell**, whose command line contains
the pattern because the pattern is written in the command being run. So
`ps | grep | xargs kill` kills the shell doing the killing: exit 144, no output, and
whatever edit was queued behind it never runs. Host B lost an epoch update to exactly
this and nearly re-armed against a stale schedule.

> **Record the PID at launch and kill that PID.** Never derive a kill list from `ps`
> or `pkill` pattern matching inside a shell whose own command line contains the
> pattern.

Three occurrences across two hosts, all after the rule was written down. The rule was
right and too weak to act on, which is its own lesson: **a rule that names the failure
without naming the tempting workaround will be re-learned.**
