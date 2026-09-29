# teleop-matrix-pipeline

Prompt + agent definitions that stand up a five-role engineering team to build the
teleoperation test matrix for the LiveKit Rust SDK in this repo.

## Use

```bash
# 1. Install the subagents
mkdir -p .claude/agents && cp teleop-matrix-pipeline/agents/*.md .claude/agents/

# 2. Open Claude Code at the repo root, then paste everything below the horizontal
#    rule in MASTER-PROMPT.md
```

## Contents

| File | What it is |
|---|---|
| `MASTER-PROMPT.md` | The prompt. Sections 1–10: prior art, requirements source, required LiveKit reading, the Rust-specific design problems, team, phases with gates, standards, scope, communication, done criteria. |
| `agents/teleop-architect.md` | Measurement design, metric→API mapping, gap resolution |
| `agents/teleop-rust-engineer.md` | The `teleop-test-matrix` crate |
| `agents/teleop-test-engineer.md` | `matrix.yaml`, run schema, runner, fixtures |
| `agents/teleop-data-analyst.md` | Extraction, scoring, breakpoints, report |
| `agents/teleop-reviewer.md` | Independent per-gate review |

## What the prompt produces

`teleop-test-matrix/` — a workspace member crate plus `matrix.yaml`,
`run_schema.json`, `run_matrix.py`, `parse_runs.py`, fixtures,
`docs/MEASUREMENT-DESIGN.md`, `SETUP-AND-TESTS.md`, and `HANDOFF.md`. Proven on
synthetic data; stops short of live-SFU execution.

## Prior art it reimplements

`~/code/Livekit Native Nathan/test_matrix/` — the same six-suite matrix built
against a C++ / libwebrtc client. The prompt points the team at it as reference,
not as something to mechanically port.
