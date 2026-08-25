---
---

<!--
No crates need bumping. `teleop-test-matrix` is `publish = false` and is not a
knope-tracked package (like the `examples/*` members), and this change is purely
additive: no existing crate's public API, dependencies, or behavior are modified. The
only edit outside the new directory is adding the workspace member.
-->

# Add the `teleop-test-matrix` measurement harness

New workspace member `teleop-test-matrix`, producing the `teleop-harness` binary used by
the teleoperation test matrix. It joins a room as both publisher and subscriber, publishes
a deterministic synthetic video track and a fixed-rate control stream over a selectable
transport, samples `RtcStats` on a fixed cadence with its own poll-budget accounting, and
appends one raw JSON snapshot per interval plus a publisher sequence log. Scoring lives in
the Python analysis layer, so the binary emits no thresholds and no verdicts.

Additive only — no existing crate's public API is touched, so no version bumps are needed.
