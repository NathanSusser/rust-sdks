#!/usr/bin/env python3
"""Sweep bitrate x codec through `webrtc-vmaf` and tabulate the resulting quality.

This answers the question T-1 cannot: **at a fixed bitrate, which codec produces the
better picture.** It is an OFFLINE ENCODE measurement on a file. It says nothing about
transport, latency, jitter, packet loss or the SFU, and its numbers must never be pooled
with harness results. See README.md.

Two repos, deliberately:

  teleop-test-matrix/vmaf/   this wrapper and the exporter  (here)
  webrtc-vmaf/               LiveKit's tool, cloned separately, NOT vendored

`--vmaf-repo` must point at your clone. Nothing here assumes where it lives.

Usage:
    # 1. export the same content the harness transports
    cargo build -p teleop-test-matrix --bin export-source
    ./target/debug/export-source --output vmaf/sources/pattern.y4m \\
        --width 1920 --height 1080 --fps 30 --duration-s 10

    # 2. sweep
    python3 vmaf/run_vmaf.py --source vmaf/sources/pattern.y4m \\
        --vmaf-repo ~/code/webrtc-vmaf --width 1920 --height 1080

    python3 vmaf/run_vmaf.py --source s.y4m --vmaf-repo ~/code/webrtc-vmaf \\
        --codec av1 --codec h264 --bitrate 2000 --bitrate 5000 --json out.json

Stdlib only, matching run_matrix.py and parse_runs.py.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

# Codecs swept by default.
#
# Matches the matrix's set exactly (matrix.yaml `codec`), so a VMAF row and a harness row
# name the same codec. H.265 is EXCLUDED by default for the same reason the matrix excludes
# it -- see cli.rs `Codec`: it is the one codec with an automatic publish-time fallback to
# H.264, so an H.265 harness cell cannot be trusted to have been H.265. That reason is a
# transport-path reason and does not apply to an offline encode, where ffmpeg either runs
# libx265 or fails; `--codec h265` is therefore accepted and will work. It is off by default
# only so the default sweep lines up 1:1 with the matrix rather than implying the harness
# can measure a codec it deliberately does not publish.
DEFAULT_CODECS = ("av1", "h264", "vp8", "vp9")

# Every codec webrtc-vmaf's encode_file() accepts. Anything else raises inside the tool
# after minutes of encoding, so it is rejected up front here instead.
SUPPORTED_CODECS = ("av1", "h264", "h264_zerolatency", "h265", "vp8", "vp9")

# Bitrate rungs in kbps.
#
# Bounded above by the PRD 8.0b 5 Mbps uplink ceiling, which is the number the whole
# video budget is written against. The low rungs matter more than the high ones: the
# codecs separate where the bitrate is scarce and converge where it is plentiful.
DEFAULT_BITRATES_KBPS = (500, 1000, 2000, 3000, 5000)

# Above this, the encode is visually indistinguishable from the source and the codec
# comparison has no headroom left to measure. Reported as a warning, never as a verdict --
# scoring lives outside this script, as in the rest of the harness.
SATURATION_VMAF = 98.0

# webrtc-vmaf prints "VMAF: <score> \tfps: <n>\t bitrate: <k>kb/s" for a single input and
# "average VMAF: ..." for several. Both are matched; the per-file line is not, since it is
# a subset of the same run.
SCORE_RE = re.compile(r"^\s*(?:average )?VMAF:\s*([0-9.]+)", re.MULTILINE)
ACTUAL_BITRATE_RE = re.compile(r"bitrate:\s*([0-9.]+)")


class VmafError(RuntimeError):
    """A sweep could not be completed. Carries a message meant to be read, not a traceback."""


def resolve_tool(repo: Path) -> Path:
    """Locate webrtc-vmaf.py inside the user's clone.

    Fails with an actionable message rather than a traceback: a missing clone is the single
    most likely first failure, and 'FileNotFoundError' would not say what to do about it.
    """
    repo = repo.expanduser()
    if not repo.exists():
        raise VmafError(
            f"no webrtc-vmaf clone at {repo}\n"
            "  This wrapper does not vendor the tool; LiveKit maintains it separately.\n"
            "  Clone it and point --vmaf-repo at it:\n"
            "    git clone https://github.com/livekit/webrtc-vmaf.git\n"
            f"    python3 run_vmaf.py --vmaf-repo /path/to/webrtc-vmaf ..."
        )
    if not repo.is_dir():
        raise VmafError(f"--vmaf-repo must be a directory, but {repo} is a file")

    tool = repo / "webrtc-vmaf.py"
    if not tool.is_file():
        raise VmafError(
            f"{repo} exists but contains no webrtc-vmaf.py\n"
            "  --vmaf-repo must point at the root of a webrtc-vmaf clone, not at its parent."
        )
    return tool


def check_prerequisites() -> None:
    """Verify ffmpeg/ffprobe are present, since webrtc-vmaf shells out to both."""
    missing = [b for b in ("ffmpeg", "ffprobe") if shutil.which(b) is None]
    if missing:
        raise VmafError(
            f"{', '.join(missing)} not on PATH; webrtc-vmaf invokes them for every encode.\n"
            "  Install ffmpeg with libvmaf support (macOS: brew install ffmpeg)."
        )


def parse_score(stdout: str) -> tuple[float, float | None]:
    """Extract (vmaf, actual_kbps) from webrtc-vmaf's stdout.

    The tool reports quality on stdout only; there is no machine-readable output to ask
    for. Parsing is therefore deliberate and narrow, and a miss is an error rather than a
    silently absent row.
    """
    match = SCORE_RE.search(stdout)
    if not match:
        raise VmafError(
            "no VMAF score in webrtc-vmaf's output. Its last lines were:\n"
            + "\n".join(stdout.strip().splitlines()[-15:])
        )
    actual = ACTUAL_BITRATE_RE.search(stdout)
    return float(match.group(1)), (float(actual.group(1)) if actual else None)


def run_one(
    tool: Path,
    source: Path,
    codec: str,
    bitrate_kbps: int,
    framerate: int,
    width: int | None,
    height: int | None,
    verbose: bool,
) -> dict:
    """Run one codec x bitrate cell and return its row."""
    command = [
        sys.executable,
        str(tool),
        "--codec", codec,
        "--bitrate", str(bitrate_kbps),
        "--framerate", str(framerate),
    ]
    if width:
        command += ["--width", str(width)]
    if height:
        command += ["--height", str(height)]
    command.append(str(source))

    if verbose:
        print(f"  $ {' '.join(command)}", file=sys.stderr)

    # cwd is the tool's own directory: webrtc-vmaf writes its intermediates to a relative
    # 'tmp_vmaf/', so running from elsewhere would scatter them through the caller's tree.
    completed = subprocess.run(
        command, capture_output=True, text=True, cwd=str(tool.parent)
    )
    if completed.returncode != 0:
        raise VmafError(
            f"webrtc-vmaf failed for {codec} at {bitrate_kbps} kbps "
            f"(exit {completed.returncode}):\n{(completed.stderr or completed.stdout).strip()}"
        )

    score, actual_kbps = parse_score(completed.stdout)
    return {
        "codec": codec,
        "target_kbps": bitrate_kbps,
        "actual_kbps": actual_kbps,
        "vmaf": score,
    }


def format_table(rows: list[dict], codecs: list[str], bitrates: list[int]) -> str:
    """Render the sweep as a bitrate x codec grid of VMAF scores."""
    by_cell = {(r["codec"], r["target_kbps"]): r for r in rows}
    width = max(9, max((len(c) for c in codecs), default=9) + 2)

    lines = []
    header = f"{'kbps':>8} " + "".join(f"{c:>{width}}" for c in codecs)
    lines.append(header)
    lines.append("-" * len(header))
    for bitrate in bitrates:
        cells = []
        for codec in codecs:
            row = by_cell.get((codec, bitrate))
            cells.append(f"{row['vmaf']:>{width}.2f}" if row else f"{'-':>{width}}")
        lines.append(f"{bitrate:>8} " + "".join(cells))
    return "\n".join(lines)


def saturation_warning(rows: list[dict]) -> str | None:
    """Warn when most cells sit at the top of the scale.

    A sweep where every codec scores ~100 has not found a codec difference; it has found
    that the source is too easy for the bitrates swept. That reads exactly like a real
    result in a table, so it is called out explicitly. This is a warning about measurement
    validity, not a threshold on the measurement -- no verdict is derived from it.
    """
    if not rows:
        return None
    saturated = [r for r in rows if r["vmaf"] >= SATURATION_VMAF]
    if len(saturated) < max(1, len(rows) // 2):
        return None
    return (
        f"WARNING: {len(saturated)} of {len(rows)} cells scored >= {SATURATION_VMAF} VMAF.\n"
        "  At these bitrates the source is near-losslessly encoded by every codec, so the\n"
        "  differences in this table are not a codec-efficiency result. Sweep lower\n"
        "  bitrates, or use a more demanding source -- the synthetic pattern is\n"
        "  deliberately simple and saturates early (see README.md)."
    )


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Sweep bitrate x codec through webrtc-vmaf on a harness-exported source.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--source", type=Path, required=True,
                    help="input clip, normally a .y4m from `export-source`")
    ap.add_argument("--vmaf-repo", type=Path, required=True,
                    help="path to your webrtc-vmaf clone (not vendored here)")
    ap.add_argument("--codec", action="append", dest="codecs", metavar="CODEC",
                    help=f"repeatable; default {' '.join(DEFAULT_CODECS)}")
    ap.add_argument("--bitrate", action="append", dest="bitrates", type=int, metavar="KBPS",
                    help="repeatable, in kbps; default "
                         f"{' '.join(str(b) for b in DEFAULT_BITRATES_KBPS)}")
    ap.add_argument("--framerate", type=int, default=30)
    ap.add_argument("--width", type=int, default=None,
                    help="defaults to the source's own width (y4m is self-describing)")
    ap.add_argument("--height", type=int, default=None)
    ap.add_argument("--json", type=Path, default=None, help="also write rows as JSON")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the cells that would run and exit")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args()

    codecs = args.codecs or list(DEFAULT_CODECS)
    bitrates = sorted(args.bitrates or list(DEFAULT_BITRATES_KBPS))

    unsupported = [c for c in codecs if c not in SUPPORTED_CODECS]
    if unsupported:
        raise VmafError(
            f"webrtc-vmaf does not support: {', '.join(unsupported)}\n"
            f"  Supported: {', '.join(SUPPORTED_CODECS)}"
        )

    if args.dry_run:
        print(f"source: {args.source}")
        print(f"tool:   {args.vmaf_repo}/webrtc-vmaf.py")
        print(f"{len(codecs) * len(bitrates)} cells: "
              f"{len(codecs)} codecs x {len(bitrates)} bitrates")
        for codec in codecs:
            for bitrate in bitrates:
                print(f"  {codec:>18} @ {bitrate:>5} kbps")
        return 0

    tool = resolve_tool(args.vmaf_repo)
    check_prerequisites()
    if not args.source.is_file():
        raise VmafError(
            f"no source clip at {args.source}\n"
            "  Generate one with:\n"
            "    cargo build -p teleop-test-matrix --bin export-source\n"
            "    ./target/debug/export-source --output "
            f"{args.source} --width 1920 --height 1080 --fps 30 --duration-s 10"
        )

    rows: list[dict] = []
    total = len(codecs) * len(bitrates)
    for index, codec in enumerate(codecs):
        for jndex, bitrate in enumerate(bitrates):
            cell = index * len(bitrates) + jndex + 1
            print(f"[{cell}/{total}] {codec} @ {bitrate} kbps ...", file=sys.stderr, flush=True)
            row = run_one(tool, args.source, codec, bitrate,
                          args.framerate, args.width, args.height, args.verbose)
            rows.append(row)
            print(f"          VMAF {row['vmaf']:.2f}", file=sys.stderr, flush=True)

    print()
    print(f"source: {args.source}   framerate: {args.framerate}")
    print("VMAF by bitrate (kbps) x codec -- OFFLINE ENCODE QUALITY ONLY,")
    print("not transport, not latency, and never pooled with harness results.")
    print()
    print(format_table(rows, codecs, bitrates))

    warning = saturation_warning(rows)
    if warning:
        print()
        print(warning)

    if args.json:
        payload = {
            "source": str(args.source),
            "framerate": args.framerate,
            "width": args.width,
            "height": args.height,
            "codecs": codecs,
            "bitrates_kbps": bitrates,
            "rows": rows,
        }
        args.json.write_text(json.dumps(payload, indent=2) + "\n")
        print(f"\nwrote {args.json}")

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except VmafError as exc:
        # A message, not a traceback: every VmafError is a condition the operator can fix.
        sys.exit(f"error: {exc}")
    except KeyboardInterrupt:
        sys.exit(130)
