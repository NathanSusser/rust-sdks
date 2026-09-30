"""Per-frame QP from B's decoder log (hostb/frames-qp.csv): reduce/qp.py, the frames.csv qp
column, encoder.qp_per_frame and the "QP per frame" report page. Screenshots are gone
(CONTRACT.md "Layout v2 ... Screenshots removed"); nothing here may bring them back.

    python3 -m unittest discover -s teleop/grid/tests -t .
"""
from __future__ import annotations

import csv
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from teleop.grid import metrics as M
from teleop.grid.reduce import qp as QP
from teleop.grid.reduce import reduce_cell
from teleop.grid.report import cell as report_cell
from teleop.grid.tests import test_metrics as TM
from teleop.grid.tests import test_report as TR

EPOCH = 1_790_384_410
QP_HEAD = "rtp_timestamp,frame_id,capture_timestamp_us,qp,width,height,decode_ms,codec,implementation"


def write_qp_log(cell: Path, rows: list[tuple]):
    p = cell / "hostb" / QP.QP_LOG
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(QP_HEAD + "\n" + "\n".join(",".join("" if v is None else str(v) for v in r) for r in rows) + "\n")


class Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="teleop-qp-test-"))
        self.cell = self.tmp / "cell"
        (self.cell / "hostb").mkdir(parents=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class QpLogTest(Tmp):
    def cap(self, fid):
        return int((EPOCH + fid / 25) * 1_000_000)

    def test_absent_and_header_only(self):
        self.assertIsNone(QP.read_qp_log(self.cell / "hostb" / "nope.csv"))
        (self.cell / "hostb" / QP.QP_LOG).write_text("")
        self.assertIsNone(QP.read_qp_log(self.cell / "hostb" / QP.QP_LOG))
        (self.cell / "hostb" / QP.QP_LOG).write_text(QP_HEAD + "\n")
        log = QP.read_qp_log(self.cell / "hostb" / QP.QP_LOG)
        self.assertEqual(len(log), 0)
        self.assertEqual(log.summary()["qp"]["n"], 0)

    def test_three_joins(self):
        write_qp_log(self.cell, [
            (90 * 1250, 1250, self.cap(1250), 31, 32, 32, 2.1, "h264", "ffmpeg"),
            (90 * 2500, None, self.cap(2500) + 4000, 35, 32, 32, 2.2, "h264", "ffmpeg"),   # capture, 4 ms off
            (90 * 3750, None, None, 40, 32, 32, 2.3, "h264", "ffmpeg"),                   # rtp only
            (90 * 5000, 5000, self.cap(5000), None, 32, 32, 2.3, "h264", "ffmpeg"),       # no qp: never answers
        ])
        log = QP.read_qp_log(self.cell / "hostb" / QP.QP_LOG)
        self.assertEqual(log.lookup(1250), (31, "frame_id"))
        self.assertEqual(log.lookup(None, self.cap(2500)), (35, "capture"))
        self.assertEqual(log.lookup(None, self.cap(2500) + 4000 + 15_001), (None, ""))  # beyond 15 ms
        self.assertEqual(log.lookup(None, None, 90 * 3750), (40, "rtp"))
        self.assertEqual(log.lookup(5000, self.cap(5000), 90 * 5000), (None, ""))
        s = log.summary()
        self.assertEqual((s["rows"], s["with_qp"], s["codec"], s["implementation"]), (4, 3, "h264", "ffmpeg"))
        self.assertEqual((s["qp"]["n"], s["qp"]["p50"], s["qp"]["max"]), (3, 35.0, 40.0))

    def test_attach_qp(self):
        write_qp_log(self.cell, [(90 * 1250, 1250, self.cap(1250), 31, 32, 32, 2.0, "h264", "x"),
                                 (90 * 2500, 2500, self.cap(2500), None, 32, 32, 2.0, "h265", "x")])
        rows = [{"frame_id": 1250, "capture_us": self.cap(1250), "rtp_ts": None},
                {"frame_id": 2500, "capture_us": self.cap(2500), "rtp_ts": None},
                {"frame_id": 9999, "capture_us": 1, "rtp_ts": None}]
        log = QP.read_qp_log(self.cell / "hostb" / QP.QP_LOG)
        self.assertEqual(QP.attach_qp(rows, log), 1)
        self.assertEqual([r["qp"] for r in rows], [31, None, None])
        self.assertEqual(QP.attach_qp(rows, None), 0)


class ReduceAndMetrics(unittest.TestCase):
    """The frames.csv qp join and encoder.qp_per_frame through the real reduce_cell + metrics.build."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="teleop-qp-reduce-"))
        self.cell = self.tmp / "t-c01"
        TM.write_synthetic_cell(self.cell)
        self.E = 1_790_000_000

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def qp_rows(self, qps):
        return [(90 * i, i + 1 + (1 if i >= 30 else 0), int((self.E + 1 + i / 30) * 1e6), q, 1600, 1300, 5.0,
                 "h265", "ffmpeg") for i, q in enumerate(qps)]

    def test_join_and_summary(self):
        qps = [30 + (i % 5) for i in range(90)]
        qps[10] = None
        write_qp_log(self.cell, self.qp_rows(qps))
        reduce_cell(self.cell)
        with (self.cell / "reduced" / "frames.csv").open() as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(list(rows[0])[-1], "qp")
        self.assertEqual(rows[0]["qp"], "30")
        self.assertEqual(rows[10]["qp"], "")                       # empty qp in the log stays empty
        red = json.loads((self.cell / "reduced" / "reduce.json").read_text())
        self.assertEqual((red["qp_log"]["rows"], red["qp_log"]["with_qp"], red["qp_log"]["joined"]), (90, 89, 89))
        m = M.build(self.cell)
        s = m["encoder"]["qp_per_frame"]
        self.assertEqual((s["n"], s["min"], s["max"]), (89, 30.0, 34.0))
        self.assertNotIn("screenshots", m["config"])
        # metrics reads reduce.json, not the raw log: removing the log changes nothing now
        (self.cell / "hostb" / QP.QP_LOG).unlink()
        self.assertEqual(M.build(self.cell)["encoder"]["qp_per_frame"], s)

    def test_empty_qp_everywhere(self):
        write_qp_log(self.cell, self.qp_rows([None] * 90))
        reduce_cell(self.cell)
        red = json.loads((self.cell / "reduced" / "reduce.json").read_text())
        self.assertTrue(any("qp empty in all" in n for n in red["notes"]))
        self.assertEqual(M.build(self.cell)["encoder"]["qp_per_frame"]["n"], 0)

    def test_absent_log(self):
        reduce_cell(self.cell)
        with (self.cell / "reduced" / "frames.csv").open() as f:
            self.assertNotIn("qp", next(csv.reader(f)))
        red = json.loads((self.cell / "reduced" / "reduce.json").read_text())
        self.assertIsNone(red["qp_log"])
        self.assertTrue(any("frames-qp.csv absent" in n for n in red["notes"]))
        m = M.build(self.cell)
        self.assertEqual(m["encoder"]["qp_per_frame"]["n"], 0)
        self.assertIsNone(m["encoder"]["qp_per_frame"]["p50"])

    def test_older_reduction_falls_back_to_the_log(self):
        """A reduce.json from before the qp_log key: metrics reads hostb/frames-qp.csv itself."""
        (self.cell / "manifest.json").write_text(json.dumps({"label": "t-c01", "epoch": self.E,
                                                             "variables": {"duration_s": 60}}))
        write_qp_log(self.cell, [(i, i, self.E * 1_000_000 + i, q, 32, 32, 1.0, "h264", "x")
                                 for i, q in enumerate([30, 31, 32, None, 40])])
        s = M.build(self.cell)["encoder"]["qp_per_frame"]
        self.assertEqual((s["n"], s["min"], s["max"], s["p50"]), (4, 30.0, 40.0, 32.0))   # nearest rank


class Report(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="teleop-qp-report-"))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_page_present_without_screenshot_markers(self):
        cd = TR.make_cell(self.tmp, 1, "h265", 2500)
        pages = report_cell.build_pages(report_cell.Cell.load(cd))
        names = [p.name for p in pages]
        for p in pages:
            report_cell.plt.close(p.fig)
        self.assertIn("QP per frame", names)
        self.assertNotIn("Screenshots", names)
        self.assertEqual(names.index("QP per frame"), names.index("Quality and codec timing") + 1)
        report_cell.render(cd)
        html = (cd / "report.html").read_text()
        self.assertIn("H.265 QP 0–51", html)
        self.assertNotIn("screenshot", html.lower())
        self.assertNotIn("<figure", html)

    def test_qp_scale_av1_and_no_qp(self):
        cd = TR.make_cell(self.tmp, 2, "av1", 2500)
        c = report_cell.Cell.load(cd)
        self.assertIn("0–255", report_cell.qp_scale(c))
        # frames.csv whose qp column is empty everywhere: no QP page, and no raw-log read either
        with (cd / "reduced" / "frames.csv").open() as f:
            rows = list(csv.DictReader(f))
        for r in rows:
            r["qp"] = ""
        with (cd / "reduced" / "frames.csv").open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
        self.assertIsNone(report_cell.Cell.load(cd).qp_frames)


if __name__ == "__main__":
    unittest.main()
