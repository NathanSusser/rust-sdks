"""Screenshots and per-frame QP: grid derivation, B's subscriber argv, reduce/screens.py,
metrics and the two report pages. Synthetic inputs in temporary directories.

    python3 -m unittest discover -s teleop/grid/tests -t .
"""
from __future__ import annotations

import csv
import json
import shutil
import tempfile
import unittest
from pathlib import Path

import numpy as np

from teleop.grid import agent as A
from teleop.grid import grid as G
from teleop.grid import metrics as M
from teleop.grid.reduce import screens as SC
from teleop.grid.report import cell as report_cell
from teleop.grid.tests import test_report as TR

EPOCH = 1_790_384_410
QP_HEAD = "rtp_timestamp,frame_id,capture_timestamp_us,qp,width,height,decode_ms,codec,implementation"
INDEX_HEAD = "frame_id,capture_timestamp_us,width,height,stride_y,stride_u,stride_v,bytes_written"

# BT.601 limited-range YUV of pure colours -> the RGB they decode to
COLOURS = {
    "red": ((81, 90, 240), (255, 0, 0)),
    "green": ((145, 54, 34), (0, 255, 0)),
    "blue": ((41, 240, 110), (0, 0, 255)),
    "white": ((235, 128, 128), (255, 255, 255)),
    "black": ((16, 128, 128), (0, 0, 0)),
    "grey": ((126, 128, 128), (128, 128, 128)),
}


def quadrants_i420(w: int, h: int, names=("red", "green", "blue", "white"), stride_pad: int = 0) -> bytes:
    """Four solid quadrants (w, h multiples of 4), planes back to back, luma stride w + stride_pad."""
    sy, sc = w + stride_pad, (w + stride_pad) // 2
    y = np.zeros((h, sy), np.uint8)
    u = np.zeros((h // 2, sc), np.uint8)
    v = np.zeros((h // 2, sc), np.uint8)
    for k, name in enumerate(names):
        (yy, uu, vv), _ = COLOURS[name]
        r0, c0 = (k // 2) * h // 2, (k % 2) * w // 2
        y[r0:r0 + h // 2, c0:c0 + w // 2] = yy
        u[r0 // 2:(r0 + h // 2) // 2, c0 // 2:(c0 + w // 2) // 2] = uu
        v[r0 // 2:(r0 + h // 2) // 2, c0 // 2:(c0 + w // 2) // 2] = vv
    return y.tobytes() + u.tobytes() + v.tobytes()


def scene_i420(w: int, h: int, k: int) -> bytes:
    """A frame that looks like something: a diagonal gradient, a moving bar, a colour patch."""
    yy, xx = np.mgrid[0:h, 0:w]
    y = (40 + 150 * (xx + yy) / (w + h)).astype(np.uint8)
    x0 = int((k * 0.17 % 1) * (w - w // 8))
    y[:, x0:x0 + w // 8] = 220
    u = np.full((h // 2, w // 2), 128, np.uint8)
    v = np.full((h // 2, w // 2), 128, np.uint8)
    (py, pu, pv), _ = COLOURS[("red", "green", "blue")[k % 3]]
    y[h // 8:h // 3, w // 16:w // 4] = py
    u[h // 16:h // 6, w // 32:w // 8] = pu
    v[h // 16:h // 6, w // 32:w // 8] = pv
    return y.tobytes() + u.tobytes() + v.tobytes()


def write_frames(cell: Path, frames: list[tuple[int, int, bytes]], w: int, h: int, *, index: bool = True,
                 index_strides=None):
    """hostb/frames/ as the subscriber writes it: <id:08>.i420 plus index.csv."""
    d = cell / "hostb" / "frames"
    d.mkdir(parents=True, exist_ok=True)
    rows = []
    for fid, cap, buf in frames:
        (d / f"{fid:08d}.i420").write_bytes(buf)
        sy, su, sv = index_strides or (w, w // 2, w // 2)
        rows.append(f"{fid},{cap},{w},{h},{sy},{su},{sv},{len(buf)}")
    if index:
        (d / "index.csv").write_text(INDEX_HEAD + "\n" + "\n".join(rows) + "\n")


def write_qp_log(cell: Path, rows: list[tuple]):
    p = cell / "hostb" / "frames-qp.csv"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(QP_HEAD + "\n" + "\n".join(",".join("" if v is None else str(v) for v in r) for r in rows) + "\n")


def write_frames_csv(cell: Path, rows: list[dict]):
    p = cell / "reduced" / "frames.csv"
    p.parent.mkdir(parents=True, exist_ok=True)
    cols = ("t_s", "frame_id", "capture_us", "rtp_ts", "bytes", "owd", "e2e")
    with p.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for r in rows:
            w.writerow(["" if r.get(c) is None else r.get(c) for c in cols])


def read_screens(cell: Path) -> list[dict]:
    with (cell / "reduced" / "screens.csv").open() as f:
        return list(csv.DictReader(f))


class Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="teleop-screens-test-"))
        self.cell = self.tmp / "cell"
        (self.cell / "hostb").mkdir(parents=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


# ---------------------------------------------------------------- grid + agent
class SampleEvery(unittest.TestCase):
    def raw(self, **d):
        return {"id": "g0930a", "defaults": {"clip": "/nonexistent/clip.mp4", "repeats": 1, "duration_s": 300,
                                             "fps": 25, "resolution": "1600x1300", "bpp": 0.08, **d},
                "axes": {"codec": ["h264"]}, "order": "sequential"}

    def test_derivation(self):
        self.assertEqual(G.sample_every(25, 300, 6), 1250)
        self.assertEqual(G.sample_every(30, 60, 20), 90)
        self.assertEqual(G.sample_every(10, 30, 20), 15)
        self.assertEqual(G.sample_every(1, 1, 20), 1)       # never 0
        self.assertIsNone(G.sample_every(25, 300, 0))

    def test_cell_values_and_label(self):
        on = G.parse(self.raw(screenshots=6)).expand()[0]
        off = G.parse(self.raw()).expand()[0]
        self.assertEqual(on.values["screenshots"], 6)
        self.assertEqual(on.values["sample_every"], 1250)
        self.assertEqual(off.values["screenshots"], 0)
        self.assertNotIn("sample_every", off.values)
        self.assertEqual(on.label, off.label)               # not part of the label
        self.assertEqual(on.manifest_variables()["sample_every"], 1250)
        self.assertNotIn("sample_every", off.manifest_variables())
        self.assertEqual(on.subscriber_args(), {"duration_s": 300, "screenshots": 6, "sample_every": 1250})
        self.assertNotIn("--sample-every", on.harness_args())

    def test_range(self):
        with self.assertRaises(G.GridError):
            G.parse(self.raw(screenshots=21))
        with self.assertRaises(G.GridError):
            G.parse(self.raw(screenshots=-1))

    def test_bpp_sweep_defaults(self):
        g = G.load(G.TELEOP_DIR / "config" / "grids" / "bpp-sweep.yaml", seed=1)
        for c in g.expand():
            self.assertEqual(c.values["screenshots"], 6)
            self.assertEqual(c.values["sample_every"], 1250)


class SubscriberArgs(unittest.TestCase):
    HOST = Path("/results/g/cells/L/hostb")

    def cmd(self, args):
        return A.subscriber_command(Path("/repo/target/release/subscriber"), "wss://sfu.invalid", "L", "b",
                                    self.HOST, ":0", args)

    def test_off(self):
        for args in ({"duration_s": 300}, {"duration_s": 300, "screenshots": 0, "sample_every": None}):
            env, argv = self.cmd(args)
            self.assertNotIn("--sample-frames-dir", argv)
            self.assertNotIn("--sample-every", argv)
            self.assertEqual(env["LK_DECODER_FRAME_LOG"], str(self.HOST / "frames-qp.csv"))
            self.assertEqual(argv[argv.index("--log-csv") + 1], str(self.HOST / "subscriber.csv"))

    def test_on(self):
        env, argv = self.cmd({"duration_s": 300, "screenshots": 6, "sample_every": 1250})
        self.assertEqual(argv[argv.index("--sample-frames-dir") + 1], str(self.HOST / "frames"))
        self.assertEqual(argv[argv.index("--sample-every") + 1], "1250")
        self.assertEqual(env["LK_DECODER_FRAME_LOG"], str(self.HOST / "frames-qp.csv"))

    def test_on_without_sample_every_derives(self):
        _, argv = self.cmd({"duration_s": 300, "fps": 25, "screenshots": 6})
        self.assertEqual(argv[argv.index("--sample-every") + 1], "1250")
        with self.assertRaises(A.AgentError):
            self.cmd({"screenshots": 6})


# ---------------------------------------------------------------- reduce
class I420(Tmp):
    def assert_quadrants(self, rgb, w, h, names, tol=2):
        for k, name in enumerate(names):
            r0, c0 = (k // 2) * h // 2, (k % 2) * w // 2
            # centre of the quadrant, away from the chroma edge
            px = rgb[r0 + h // 4, c0 + w // 4].astype(int)
            self.assertTrue(np.all(np.abs(px - np.array(COLOURS[name][1])) <= tol), f"{name}: {px}")

    def test_known_colours(self):
        w, h = 64, 48
        rgb = SC.i420_to_rgb(quadrants_i420(w, h), w, h, w, w // 2, w // 2)
        self.assertEqual(rgb.shape, (h, w, 3))
        self.assertEqual(rgb.dtype, np.uint8)
        self.assert_quadrants(rgb, w, h, ("red", "green", "blue", "white"))
        rgb = SC.i420_to_rgb(quadrants_i420(w, h, ("black", "grey", "white", "red")), w, h)
        self.assert_quadrants(rgb, w, h, ("black", "grey", "white", "red"))

    def test_png_round_trip(self):
        import matplotlib.image as mimg
        w, h = 64, 48
        write_frames(self.cell, [(1250, (EPOCH + 50) * 1_000_000, quadrants_i420(w, h))], w, h)
        n = SC.reduce_screens(self.cell, EPOCH)
        self.assertEqual(n, 1)
        png = self.cell / "reduced" / "screens" / "1250.png"
        self.assertTrue(png.is_file())
        img = mimg.imread(png)
        self.assertEqual(img.shape[:2], (h, w))
        self.assert_quadrants(np.round(img[..., :3] * 255).astype(np.uint8), w, h, ("red", "green", "blue", "white"))

    def test_padded_stride_inferred(self):
        # the writer records stride w, but the decoder's planes carried 32 bytes of padding per row
        w, h = 64, 48
        buf = quadrants_i420(w, h, stride_pad=32)
        self.assertEqual(SC.plane_layout(w, h, w, w // 2, w // 2, len(buf)), (96, 48, 48))
        rgb = SC.i420_to_rgb(buf, w, h, w, w // 2, w // 2)
        self.assert_quadrants(rgb, w, h, ("red", "green", "blue", "white"))
        with self.assertRaises(ValueError):
            SC.plane_layout(w, h, w, w // 2, w // 2, len(buf) + 7)

    def test_no_frames_dir(self):
        self.assertEqual(SC.reduce_screens(self.cell, EPOCH), 0)
        self.assertFalse((self.cell / "reduced" / "screens.csv").exists())

    def test_bad_frame_skipped_not_fatal(self):
        w, h = 32, 32
        write_frames(self.cell, [(10, EPOCH * 1_000_000, quadrants_i420(w, h)),
                                 (20, EPOCH * 1_000_000, b"\x00" * 17)], w, h)
        notes = []
        self.assertEqual(SC.reduce_screens(self.cell, EPOCH, notes=notes), 1)
        self.assertTrue(any("00000020" in n for n in notes))


class ScreensJoin(Tmp):
    W, H = 32, 32

    def setUp(self):
        super().setUp()
        cap = lambda fid: (EPOCH + fid / 25) * 1_000_000      # noqa: E731
        self.cap = cap
        write_frames(self.cell, [(fid, int(cap(fid)), quadrants_i420(self.W, self.H)) for fid in (1250, 2500, 3750)],
                     self.W, self.H)
        write_frames_csv(self.cell, [
            {"t_s": fid / 25, "frame_id": fid, "capture_us": int(cap(fid)), "rtp_ts": 90 * fid, "bytes": 20_000 + fid,
             "owd": 40.5 + fid / 1000, "e2e": 80.25} for fid in (1250, 2500, 3750)])

    def test_with_qp_log(self):
        write_qp_log(self.cell, [
            (90 * 1250, 1250, int(self.cap(1250)), 31, self.W, self.H, 2.1, "h264", "ffmpeg"),
            # no frame_id: matched by capture timestamp (4 ms off)
            (90 * 2500, None, int(self.cap(2500)) + 4000, 35, self.W, self.H, 2.2, "h264", "ffmpeg"),
            # neither frame_id nor capture: matched by RTP timestamp
            (90 * 3750, None, None, 40, self.W, self.H, 2.3, "h264", "ffmpeg"),
        ])
        self.assertEqual(SC.reduce_screens(self.cell, EPOCH), 3)
        rows = {int(r["frame_id"]): r for r in read_screens(self.cell)}
        self.assertEqual(list(read_screens(self.cell)[0]), list(SC.SCREEN_COLS))
        self.assertEqual((rows[1250]["qp"], rows[1250]["qp_join"]), ("31", "frame_id"))
        self.assertEqual((rows[2500]["qp"], rows[2500]["qp_join"]), ("35", "capture"))
        self.assertEqual((rows[3750]["qp"], rows[3750]["qp_join"]), ("40", "rtp"))
        self.assertAlmostEqual(float(rows[2500]["t_s"]), 100.0, places=3)
        self.assertEqual(rows[1250]["bytes"], "21250")
        self.assertAlmostEqual(float(rows[1250]["owd"]), 41.75, places=3)
        self.assertEqual(rows[1250]["png"], "screens/1250.png")

    def test_without_qp_log(self):
        self.assertEqual(SC.reduce_screens(self.cell, EPOCH), 3)
        for r in read_screens(self.cell):
            self.assertEqual(r["qp"], "")
            self.assertEqual(r["qp_join"], "")
            self.assertNotEqual(r["bytes"], "")

    def test_empty_qp_h265(self):
        write_qp_log(self.cell, [(90 * f, f, int(self.cap(f)), None, self.W, self.H, 2.0, "h265", "x")
                                 for f in (1250, 2500, 3750)])
        SC.reduce_screens(self.cell, EPOCH)
        self.assertTrue(all(r["qp"] == "" for r in read_screens(self.cell)))

    def test_no_index_uses_qp_log_size(self):
        shutil.rmtree(self.cell / "hostb" / "frames")
        write_frames(self.cell, [(1250, int(self.cap(1250)), quadrants_i420(self.W, self.H))], self.W, self.H,
                     index=False)
        write_qp_log(self.cell, [(90 * 1250, 1250, int(self.cap(1250)), 30, self.W, self.H, 2.0, "h264", "x")])
        self.assertEqual(SC.reduce_screens(self.cell, EPOCH), 1)
        r = read_screens(self.cell)[0]
        self.assertEqual((r["width"], r["qp"]), (str(self.W), "30"))
        self.assertAlmostEqual(float(r["t_s"]), 50.0, places=3)     # capture time from the QP log

    def test_attach_qp_to_frame_rows(self):
        write_qp_log(self.cell, [(90 * 1250, 1250, int(self.cap(1250)), 31, 32, 32, 2.0, "h264", "x"),
                                 (90 * 2500, 2500, int(self.cap(2500)), None, 32, 32, 2.0, "h264", "x")])
        rows = [{"frame_id": 1250, "capture_us": int(self.cap(1250)), "rtp_ts": None},
                {"frame_id": 2500, "capture_us": int(self.cap(2500)), "rtp_ts": None},
                {"frame_id": 9999, "capture_us": 1, "rtp_ts": None}]
        log = SC.read_qp_log(self.cell / "hostb" / "frames-qp.csv")
        self.assertEqual(SC.attach_qp(rows, log), 1)
        self.assertEqual([r["qp"] for r in rows], [31, None, None])
        self.assertEqual(SC.attach_qp(rows, None), 0)
        self.assertIsNone(SC.read_qp_log(self.cell / "hostb" / "nope.csv"))


# ---------------------------------------------------------------- metrics
class Metrics(Tmp):
    def build(self):
        (self.cell / "manifest.json").write_text(json.dumps({"label": "cell", "epoch": EPOCH,
                                                             "variables": {"duration_s": 60, "screenshots": 6}}))
        return M.build(self.cell)

    def test_qp_per_frame(self):
        write_qp_log(self.cell, [(i, i, EPOCH * 1_000_000 + i, q, 32, 32, 1.0, "h264", "x")
                                 for i, q in enumerate([30, 31, 32, None, 40])])
        m = self.build()
        s = m["encoder"]["qp_per_frame"]
        self.assertEqual((s["n"], s["min"], s["max"], s["p50"]), (4, 30.0, 40.0, 32.0))   # nearest rank
        self.assertEqual(m["encoder"]["qp"]["n"], 0)        # the per-second series is untouched
        self.assertEqual(m["config"]["screenshots"], 0)
        on_disk = json.loads((self.cell / "metrics.json").read_text())
        self.assertIn("qp_per_frame", on_disk["encoder"])

    def test_absent_log_and_screens_count(self):
        (self.cell / "reduced").mkdir()
        (self.cell / "reduced" / "screens.csv").write_text(",".join(SC.SCREEN_COLS) + "\n1,1,screens/1.png,,,,,,,\n"
                                                           "2,2,screens/2.png,,,,,,,\n")
        m = self.build()
        self.assertEqual(m["encoder"]["qp_per_frame"]["n"], 0)
        self.assertIsNone(m["encoder"]["qp_per_frame"]["p50"])
        self.assertEqual(m["config"]["screenshots"], 2)


# ---------------------------------------------------------------- report
class Report(unittest.TestCase):
    W, H = 320, 260

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="teleop-screens-report-"))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def make(self, index: int, *, qp_log: bool, shots: int = 6) -> Path:
        cd = TR.make_cell(self.tmp, index, "h264", 2500, extra_vars={"screenshots": shots, "sample_every": 300})
        with (cd / "reduced" / "frames.csv").open() as f:
            fr = list(csv.DictReader(f))
        ids = [int(r["frame_id"]) for r in fr]
        picks = ids[::len(ids) // shots][:shots]
        cap = {int(r["frame_id"]): int((TR.EPOCH + float(r["t_s"])) * 1e6) for r in fr}
        write_frames(cd, [(fid, cap[fid], scene_i420(self.W, self.H, k)) for k, fid in enumerate(picks)],
                     self.W, self.H)
        if qp_log:
            rng = np.random.default_rng(index)
            write_qp_log(cd, [(90 * i, fid, cap[fid], int(np.clip(rng.normal(30, 3), 10, 51)), self.W, self.H, 2.0,
                               "h264", "ffmpeg") for i, fid in enumerate(ids)])
        SC.reduce_screens(cd, TR.EPOCH, json.loads((cd / "manifest.json").read_text()))
        M_json = json.loads((cd / "metrics.json").read_text())
        if qp_log:
            M_json["encoder"]["qp_per_frame"] = M.summ(SC.read_qp_log(cd / "hostb" / "frames-qp.csv").qps())
        (cd / "metrics.json").write_text(json.dumps(M_json))
        return cd

    def test_two_new_pages(self):
        cd = self.make(1, qp_log=True)
        self.assertEqual(len(read_screens(cd)), 6)
        pages = report_cell.build_pages(report_cell.Cell.load(cd))
        names = [p.name for p in pages]
        self.assertIn("Screenshots", names)
        self.assertIn("QP per frame", names)
        self.assertEqual(names.index("QP per frame"), names.index("Screenshots") + 1)
        for p in pages:
            report_cell.plt.close(p.fig)
        pdf = report_cell.render(cd)
        self.assertEqual(TR.pdf_pages(pdf), TR.expected_cell_pages(cd) + 1)
        html = (cd / "report.html").read_text()
        self.assertIn("Screenshots", html)
        self.assertIn("QP per frame", html)
        self.assertIn("H.264 QP 0–51", html)
        # six embedded screenshots, each captioned
        self.assertEqual(html.count("<figure"), 6)
        self.assertEqual(html.count("<figcaption"), 6)
        self.assertRegex(html, r"#\d+ +t [\d.]+ s +QP \d+ +[\d.]+ kB +one-way \d+ ms")

    def test_screens_without_qp_and_paging(self):
        cd = self.make(2, qp_log=False, shots=8)
        pages = report_cell.build_pages(report_cell.Cell.load(cd))
        names = [p.name for p in pages]
        self.assertIn("Screenshots (1)", names)
        self.assertIn("Screenshots (2)", names)
        # the fixture's frames.csv carries a qp column, so the QP page still appears (from frames.csv)
        self.assertIn("QP per frame", names)
        for p in pages:
            report_cell.plt.close(p.fig)

    def test_qp_scale_av1(self):
        cd = TR.make_cell(self.tmp, 3, "av1", 2500)
        c = report_cell.Cell.load(cd)
        self.assertIn("0–255", report_cell.qp_scale(c))
        self.assertEqual(report_cell.pages_screens(c), [])      # no screens.csv, no page


if __name__ == "__main__":
    unittest.main()
