#!/usr/bin/env python3
"""Tests for run_vmaf.py that need neither a webrtc-vmaf clone nor a camera.

What is covered here is the glue that is wrong silently: parsing the tool's stdout (its
only output), the missing-clone error path (the most likely first failure), and the
saturation warning (which is what stops a table of 99.9s being read as a codec result).

Run:  python3 -m pytest vmaf/test_run_vmaf.py
"""

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import run_vmaf  # noqa: E402


# --------------------------------------------------------------------------
# Parsing webrtc-vmaf's stdout.
#
# The tool prints its score and nothing machine-readable, so this parser is the only
# thing standing between a real number and a plausible wrong one.
# --------------------------------------------------------------------------

SINGLE_INPUT_STDOUT = """computing VMAF for av1 at 2000
  pattern.y4m: 92.254380 \tfps: 281\t bitrate: 1987.4kb/s
VMAF: 92.254380 \tfps: 281\t bitrate: 1987.4kb/s
"""

MULTI_INPUT_STDOUT = """computing VMAF for vp9 at 1500
  a.y4m: 90.0 \tfps: 100\t bitrate: 1400.0kb/s
  b.y4m: 94.0 \tfps: 120\t bitrate: 1500.0kb/s
average VMAF: 92.0 \tfps: 110\t bitrate: 1450000.0
"""


def test_parses_the_single_input_summary_line():
    score, actual = run_vmaf.parse_score(SINGLE_INPUT_STDOUT)
    assert score == pytest.approx(92.25438)
    # The actual achieved bitrate, which is how a codec that missed its target is caught.
    assert actual == pytest.approx(1987.4)


def test_parses_the_multi_input_average_line():
    score, _ = run_vmaf.parse_score(MULTI_INPUT_STDOUT)
    assert score == pytest.approx(92.0)


def test_a_perfect_score_still_parses():
    """A lossless encode prints an integer-looking score; the regex must still match."""
    score, _ = run_vmaf.parse_score("VMAF: 100.000000 \tfps: 9\t bitrate: 1.0kb/s\n")
    assert score == pytest.approx(100.0)


def test_missing_score_is_an_error_not_a_silent_zero():
    """A zero here would enter the table as a real, catastrophically bad result."""
    with pytest.raises(run_vmaf.VmafError) as excinfo:
        run_vmaf.parse_score("computing VMAF for av1 at 2000\nffmpeg: no such filter\n")
    assert "no VMAF score" in str(excinfo.value)
    # The tool's own output has to reach the operator; it is the only diagnosis available.
    assert "no such filter" in str(excinfo.value)


# --------------------------------------------------------------------------
# Locating the user's clone.
# --------------------------------------------------------------------------


def test_a_missing_clone_explains_itself(tmp_path):
    with pytest.raises(run_vmaf.VmafError) as excinfo:
        run_vmaf.resolve_tool(tmp_path / "not-here")
    message = str(excinfo.value)
    assert "no webrtc-vmaf clone" in message
    # Actionable: the operator must not have to go and find the URL.
    assert "git clone https://github.com/livekit/webrtc-vmaf.git" in message


def test_a_directory_without_the_script_says_so(tmp_path):
    """Pointing at the clone's parent is the obvious mistake and must not read as absent."""
    (tmp_path / "webrtc-vmaf").mkdir()
    with pytest.raises(run_vmaf.VmafError) as excinfo:
        run_vmaf.resolve_tool(tmp_path)
    assert "contains no webrtc-vmaf.py" in str(excinfo.value)


def test_a_file_is_rejected_as_a_repo(tmp_path):
    target = tmp_path / "webrtc-vmaf"
    target.write_text("not a directory")
    with pytest.raises(run_vmaf.VmafError) as excinfo:
        run_vmaf.resolve_tool(tmp_path / "webrtc-vmaf")
    assert "must be a directory" in str(excinfo.value)


def test_a_real_clone_resolves(tmp_path):
    repo = tmp_path / "webrtc-vmaf"
    repo.mkdir()
    script = repo / "webrtc-vmaf.py"
    script.write_text("#!/usr/bin/env python3\n")
    assert run_vmaf.resolve_tool(repo) == script


# --------------------------------------------------------------------------
# Table rendering and the saturation warning.
# --------------------------------------------------------------------------


def test_the_table_is_a_bitrate_by_codec_grid():
    rows = [
        {"codec": "av1", "target_kbps": 1000, "actual_kbps": 990.0, "vmaf": 88.5},
        {"codec": "h264", "target_kbps": 1000, "actual_kbps": 995.0, "vmaf": 71.25},
    ]
    table = run_vmaf.format_table(rows, ["av1", "h264"], [1000])
    assert "av1" in table and "h264" in table
    assert "88.50" in table and "71.25" in table


def test_a_missing_cell_renders_as_a_dash_not_a_zero():
    rows = [{"codec": "av1", "target_kbps": 1000, "actual_kbps": None, "vmaf": 88.5}]
    table = run_vmaf.format_table(rows, ["av1", "h264"], [1000, 2000])
    assert "-" in table
    assert "0.00" not in table


def test_a_saturated_sweep_is_flagged():
    """Every codec at ~100 is not a codec result; it is a source that is too easy.

    This is the condition actually observed on the synthetic pattern at 1080p above
    2 Mbps, so the warning is guarding a real case rather than a hypothetical one.
    """
    rows = [
        {"codec": c, "target_kbps": b, "actual_kbps": None, "vmaf": 99.9}
        for c in ("av1", "h264")
        for b in (2000, 5000)
    ]
    warning = run_vmaf.saturation_warning(rows)
    assert warning is not None
    assert "not a codec-efficiency result" in warning


def test_a_discriminating_sweep_is_not_flagged():
    rows = [
        {"codec": "av1", "target_kbps": 500, "actual_kbps": None, "vmaf": 78.0},
        {"codec": "h264", "target_kbps": 500, "actual_kbps": None, "vmaf": 61.0},
        {"codec": "av1", "target_kbps": 1000, "actual_kbps": None, "vmaf": 89.0},
        {"codec": "h264", "target_kbps": 1000, "actual_kbps": None, "vmaf": 74.0},
    ]
    assert run_vmaf.saturation_warning(rows) is None


def test_no_rows_produces_no_warning():
    assert run_vmaf.saturation_warning([]) is None


# --------------------------------------------------------------------------
# Argument handling, through the real CLI.
# --------------------------------------------------------------------------

SCRIPT = Path(__file__).parent / "run_vmaf.py"


def run_cli(*args):
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args], capture_output=True, text=True
    )


def test_dry_run_enumerates_cells_without_a_clone(tmp_path):
    """--dry-run must work before anything is installed, like run_matrix.py --plan."""
    result = run_cli(
        "--source", str(tmp_path / "nope.y4m"),
        "--vmaf-repo", str(tmp_path / "absent"),
        "--dry-run",
    )
    assert result.returncode == 0, result.stderr
    # Four default codecs x five default bitrates.
    assert "20 cells" in result.stdout
    assert "av1" in result.stdout


def test_the_default_codec_set_matches_the_matrix():
    assert run_vmaf.DEFAULT_CODECS == ("av1", "h264", "vp8", "vp9")
    # H.265 is excluded by default but must remain reachable for a pure codec comparison.
    assert "h265" in run_vmaf.SUPPORTED_CODECS


def test_the_bitrate_sweep_reaches_the_prd_ceiling():
    """PRD 8.0b puts the uplink ceiling at 5 Mbps; the sweep must include it."""
    assert max(run_vmaf.DEFAULT_BITRATES_KBPS) == 5000


def test_an_unsupported_codec_fails_before_encoding(tmp_path):
    result = run_cli(
        "--source", str(tmp_path / "s.y4m"),
        "--vmaf-repo", str(tmp_path),
        "--codec", "mpeg2",
    )
    assert result.returncode != 0
    assert "does not support" in result.stderr
    assert "Traceback" not in result.stderr


def test_a_missing_clone_exits_cleanly_without_a_traceback(tmp_path):
    """The most likely first failure. A traceback would bury the one actionable line."""
    source = tmp_path / "s.y4m"
    source.write_text("")
    result = run_cli(
        "--source", str(source), "--vmaf-repo", str(tmp_path / "absent"),
    )
    assert result.returncode != 0
    assert "Traceback" not in result.stderr
    assert "no webrtc-vmaf clone" in result.stderr


def test_a_missing_source_names_the_exporter(tmp_path):
    """The fix is to run export-source, so the error says so rather than just 'not found'."""
    repo = tmp_path / "webrtc-vmaf"
    repo.mkdir()
    (repo / "webrtc-vmaf.py").write_text("#!/usr/bin/env python3\n")
    result = run_cli(
        "--source", str(tmp_path / "absent.y4m"), "--vmaf-repo", str(repo),
    )
    assert result.returncode != 0
    assert "export-source" in result.stderr


def test_source_and_repo_are_both_required():
    assert run_cli("--dry-run").returncode != 0
