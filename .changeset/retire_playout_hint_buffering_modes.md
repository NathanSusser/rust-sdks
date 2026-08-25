---
---

<!--
No crates need bumping. `teleop-test-matrix` is `publish = false` and is not a
knope-tracked package (like the `examples/*` members), and this change is confined to
that crate: no other crate's public API, dependencies, or behavior are modified.
-->

# Retire the playout-hint buffering modes from `teleop-test-matrix`

`--buffering-mode` no longer accepts `playout_hint_floor` or `playout_hint_smooth`; the
remaining values are `default` and `zero_jitter`. The room-level playout-delay hint path
is removed with them: the `--playout-delay-min-ms` / `--playout-delay-max-ms` flags, the
`create_room_with_playout_delay` call site, and the `PlayoutDelayApplied` enum are gone.

Measured runs showed the two hint modes indistinguishable from each other despite a
100-400x difference in requested buffer, while `zero_jitter` measured lower on two
independent metrics; LiveKit's robotics documentation confirms a true 0/0 is not reachable
through the hint API. `matrix.yaml` already locks `buffering_mode` to `zero_jitter`, so
the harness can no longer accept a mode the matrix does not model.

The `playout_delay_applied` field remains in the run-metadata record, pinned to
`not_requested`, so pre-existing and new run records share one schema.

The pre-run `delete_room` that `ensure_room` performed is retained as `session::reset_room`
and now runs for every buffering mode rather than only for the hint modes. The matrix
reuses room names across repeats, so a repeat could otherwise inherit participants from a
predecessor that failed to close cleanly. A delete failure stays non-fatal: the room not
existing is the expected case.
