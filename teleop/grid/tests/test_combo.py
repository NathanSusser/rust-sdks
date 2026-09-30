"""report/combo.py: <combo>/summary.pdf + summary.html over a combination's repeats, and the
staleness rule report.grid.render uses to refresh them. Synthetic repeats in temporary dirs.

    python3 -m unittest discover -s teleop/grid/tests -t .
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from teleop.grid import stats
from teleop.grid.report import combo as C
from teleop.grid.report import grid as G
from teleop.grid.tests import synth


def pdf_pages(path: Path) -> int:
    out = subprocess.run(["pdfinfo", str(path)], capture_output=True, text=True, check=True).stdout
    return int(re.search(r"^Pages:\s+(\d+)", out, re.M).group(1))


def variables(codec="h265", fps=30, bpp=0.06, duration_s=12):
    return {"codec": codec, "fps": fps, "bpp": bpp, "kbps": synth.kbps_of(fps, bpp), "resolution": "1600x1300",
            "width": 1600, "height": 1300, "vbv_frames": 1, "padding": True, "duration_s": duration_s,
            "control_transport": "data_track_buf1"}


def write_plan(gd: Path, repeats: int):
    (gd / "grid.yaml").write_text(json.dumps({"id": synth.GRID_ID, "swept": ["codec", "fps", "bpp"],
                                              "axes": {"codec": ["h265"], "fps": [30], "bpp": [0.06]},
                                              "defaults": {"repeats": repeats}}))


class ComboSummary(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="teleop-combo-test-"))
        cls.gd = cls.tmp / synth.GRID_ID
        cls.gd.mkdir()
        write_plan(cls.gd, 4)                                   # r4 planned, never run
        v = variables()
        cls.cd = cls.gd / "h265-30fps-b0060"
        synth.make_repeat(cls.gd, "h265-30fps-b0060/r1", v, index=1, repeat=1, seed=11)
        synth.make_repeat(cls.gd, "h265-30fps-b0060/r2", v, index=2, repeat=2, seed=12, status="INCOMPLETE")
        synth.make_repeat(cls.gd, "h265-30fps-b0060/r3", v, index=3, repeat=3, seed=13, encoder="libx265",
                          ptp_locked=False, negotiated_codec="h264")
        (cls.cd / "r1" / "report.pdf").write_bytes(b"%PDF-1.4\n%synthetic\n")     # a link target
        cls.pdf = C.render(cls.cd)
        cls.html = (cls.cd / "summary.html").read_text()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_outputs(self):
        self.assertEqual(self.pdf, self.cd / "summary.pdf")
        self.assertEqual(pdf_pages(self.pdf), 2)
        for s in ("r1", "r2", "r3", "r4", "med", "Distributions", "Scalars", "Over time", "Repeats and flags"):
            self.assertIn(s, self.html)
        self.assertNotRegex(self.html.replace("http://www.w3.org", ""), r"https?://")

    def test_flags(self):
        for s in ("INCOMPLETE", "not NVENC", "PTP not locked", "codec fallback", "h265 → h264", "not run"):
            self.assertIn(s, self.html, s)
        self.assertIn('<div class="banner">r2: INCOMPLETE', self.html)
        self.assertIn('<div class="banner">r3: not NVENC', self.html)

    def test_median_over_counted_repeats(self):
        reps, _ = C.load(self.cd)
        self.assertEqual([r.n for r in reps], [1, 2, 3, 4])
        self.assertEqual([r.counted for r in reps], [True, False, True, False])     # r2 INCOMPLETE, r4 not run
        srows, vrows = C.kpi_tables(reps)
        e2e = next(r for r in srows if r[0].startswith("Glass-to-glass"))
        p99 = e2e[2]["p99"]
        self.assertEqual(len(p99), 5)                            # r1..r4 + median
        self.assertIsNone(p99[3])                                # r4: not run
        self.assertEqual(p99[4], stats.summ([p99[0], p99[2]])["p50"])
        fps = next(r for r in vrows if r[0].startswith("Delivered frame rate, % of target"))
        self.assertTrue(90 < fps[2][0] < 102)          # a span-based rate: can sit a hair above the target

    def test_links(self):
        self.assertIn('href="r1/report.pdf"', self.html)
        self.assertNotIn('href="r2/report.pdf"', self.html)      # no report rendered for r2
        self.assertIn('href="../comparison/analysis.html"', self.html)


class Awkward(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="teleop-combo-edge-"))
        self.gd = self.tmp / synth.GRID_ID
        self.gd.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_one_repeat(self):
        synth.make_repeat(self.gd, "av1-25fps-b0140/r1", variables("av1", 25, 0.14), index=0, repeat=1, seed=3)
        pdf = C.render(self.gd / "av1-25fps-b0140")
        self.assertEqual(pdf_pages(pdf), 2)
        self.assertIn("AV1", (self.gd / "av1-25fps-b0140" / "summary.html").read_text())

    def test_no_metrics_anywhere(self):
        synth.make_repeat(self.gd, "h265-30fps-b0040/r1", variables(bpp=0.04), index=0, repeat=1, seed=1,
                          status="SKIPPED", metrics_json=False)
        synth.make_repeat(self.gd, "h265-30fps-b0040/r2", variables(bpp=0.04), index=1, repeat=2, seed=2,
                          status="SKIPPED", metrics_json=False)
        pdf = C.render(self.gd / "h265-30fps-b0040")
        self.assertEqual(pdf_pages(pdf), 2)
        html = (self.gd / "h265-30fps-b0040" / "summary.html").read_text()
        self.assertIn("no metrics.json", html)
        self.assertIn("SKIPPED", html)

    def test_no_control_logs_and_no_qp(self):
        synth.make_repeat(self.gd, "h265-25fps-b0080/r1", variables(fps=25, bpp=0.08), index=0, repeat=1, seed=5,
                          control=False, qp_log=False)
        C.render(self.gd / "h265-25fps-b0080")
        html = (self.gd / "h265-25fps-b0080" / "summary.html").read_text()
        self.assertIn("no control-path data", html)

    def test_refresh_only_when_stale(self):
        v = variables()
        synth.make_repeat(self.gd, "h265-30fps-b0060/r1", v, index=0, repeat=1, seed=7)
        cd = self.gd / "h265-30fps-b0060"
        self.assertTrue(G._stale(cd))
        self.assertEqual(G.refresh_summaries(self.gd), [])
        t0 = (cd / "summary.pdf").stat().st_mtime_ns
        self.assertFalse(G._stale(cd))
        G.refresh_summaries(self.gd)
        self.assertEqual((cd / "summary.pdf").stat().st_mtime_ns, t0)             # nothing re-rendered
        # a new repeat's metrics.json is newer than the summary: stale again
        time.sleep(0.02)
        synth.make_repeat(self.gd, "h265-30fps-b0060/r2", v, index=1, repeat=2, seed=8)
        later = time.time() + 5
        os.utime(cd / "r2" / "metrics.json", (later, later))
        self.assertTrue(G._stale(cd))
        G.refresh_summaries(self.gd)
        self.assertGreater((cd / "summary.pdf").stat().st_mtime_ns, t0)
        self.assertIn("r2", (cd / "summary.html").read_text())

    def test_render_error_is_logged_not_raised(self):
        cd = self.gd / "broken"
        (cd / "r1").mkdir(parents=True)
        (cd / "r1" / "manifest.json").write_text("{not json")
        (cd / "r1" / "metrics.json").write_text(json.dumps({"integrity": "not a dict"}))
        errs = G.refresh_summaries(self.gd)
        # either rendered despite the junk, or failed into summary-errors.log -- never raised
        self.assertTrue(not errs or (cd / "summary-errors.log").is_file())


if __name__ == "__main__":
    unittest.main()
