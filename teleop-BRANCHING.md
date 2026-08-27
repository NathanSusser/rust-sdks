# Two projects, one repo

Both projects live in this fork. They share a base, they never touch the same files,
and either can be tested against the other with a merge that cannot conflict.

```
upstream/main
  └── main
       └── teleop-test-matrix          shared base — harness, reference docs
            ├── enable-dscp             SDK only:  webrtc-sys/  libwebrtc/
            └── teleop-slice-clients    app only:  teleop-test-matrix/ config/ net/
```

## The rule that makes this work

**`enable-dscp` touches only `webrtc-sys/` and `libwebrtc/`.**
**`teleop-slice-clients` touches only `teleop-test-matrix/`, `config/`, `net/`, `docs/`.**

If either branch needs to change a file outside its set — the workspace `Cargo.toml`,
a reference doc, anything in the harness both projects use — stop, put it on
`teleop-test-matrix`, and merge down. Hold that line and merges between these
branches stay trivial forever, because git never sees two edits to one file.

## Setup

Commit the untracked work first, or it will not exist in either worktree:

```bash
git switch teleop-test-matrix
git add 5g-slice-steering-device-reference.md \
        5g-teleop-qos-device-reference.md \
        teleop-matrix-pipeline/ \
        teleop-slice-clients-PROMPT.md \
        enable-dscp-PROMPT.md \
        teleop-BRANCHING.md
git commit -m "Reference docs and agent prompts for slice work"
```

Then two worktrees, both based on `teleop-test-matrix`:

```bash
git worktree add ../rust-sdks-dscp  -b enable-dscp           teleop-test-matrix
git worktree add ../rust-sdks-slice -b teleop-slice-clients  teleop-test-matrix

for d in ../rust-sdks-dscp ../rust-sdks-slice; do
  (cd "$d" && git submodule update --init --recursive)
done
```

Both branch off `teleop-test-matrix` rather than `main` because the DSCP work needs a
client to test against, and `teleop-harness` is that client.

**Submodules do not follow a worktree.** Skip that loop and `livekit-protocol` and
`yuv-sys` come up empty with a confusing build error.

**Do not share `CARGO_TARGET_DIR` between the two.** `enable-dscp` rebuilds
`webrtc-sys`; a shared target thrashes libwebrtc on every switch. Separate targets,
eat the disk.

## Day to day

Shared change — a harness fix, a reference doc, a workspace dependency:

```bash
git switch teleop-test-matrix
# edit, commit
git -C ../rust-sdks-dscp  merge teleop-test-matrix
git -C ../rust-sdks-slice merge teleop-test-matrix
```

Test the DSCP patch against the slice clients:

```bash
git -C ../rust-sdks-slice merge enable-dscp
```

File-disjoint, so this cannot conflict. Merge it as often as you like; it is a
one-way convenience and does not commit you to shipping them together.

## Keeping the DSCP patch upstreamable

`enable-dscp` changes only files that belong to `livekit/rust-sdks`, which is what
makes it a viable PR to upstream. Keep the commits clean and single-purpose — no
"and also fixed a typo in the harness" — so extracting it later is one command:

```bash
git switch -c dscp-upstream main
git cherry-pick <sha>..<sha>
```

If a T-Mobile-specific detail ever lands in those commits, that door closes.

## Merging back

Both feature branches merge into `teleop-test-matrix` when they are done. Nothing
merges into `main` — `main` tracks upstream, and `upstream` has push disabled.
