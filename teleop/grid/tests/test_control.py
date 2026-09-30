"""The control path over the data track: reduce/control.py, metrics.json `control` and the cell
report's "Control path (data track)" page, on synthetic logs in temporary directories.

    python3 -m unittest discover -s teleop/grid/tests -t .

Inputs are the formats CONTRACT.md "Layout v2 ... Control path" fixes: hosta/control-pub.jsonl
({"seq","t_send_unix_us","t_send_monotonic_us","probe"}), hostb/control.csv
(seq,t_send_unix_us,t_recv_unix_us,owd_us,probe_token,transport) and the probe round trips in
A's stats jsonl (probe.rtt_us_interval).
"""
from __future__ import annotations

import csv
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from teleop.grid import metrics as M
from teleop.grid.reduce import control as CTL
from teleop.grid.reduce import frames as F
from teleop.grid.reduce import reduce_cell
from teleop.grid.report import cell as report_cell
from teleop.grid.tests import test_metrics as TM

E = 1_790_000_000
HZ = 200


def t_us(s: int, t0: float) -> int:
    """Send time of seq s in integer microseconds (exact, so window edges are deterministic)."""
    return E * 1_000_000 + int(round(t0 * 1e6)) + s * (1_000_000 // HZ)


def write_pub(cell: Path, seqs, t0=0.5, extra_lines=()):
    with open(cell / "hosta" / CTL.PUB_LOG, "w") as f:
        for s in seqs:
            t = t_us(s, t0)
            f.write(json.dumps({"seq": s, "t_send_unix_us": t, "t_send_monotonic_us": s * 5000,
                                "probe": s % 100 == 0}) + "\n")
        for ln in extra_lines:
            f.write(ln + "\n")


def write_recv(cell: Path, seqs, owd_ms=lambda s: 30.0, t0=0.5, dup=(), shift_send=(), transport="data_track_buf1",
               extra_rows=()):
    rows = []
    for s in seqs:
        ts = t_us(s, t0) + (5000 if s in shift_send else 0)
        tr = ts + int(owd_ms(s) * 1000)
        rows.append((tr, f"{s},{ts},{tr},{tr - ts},{s + 7 if s % 100 == 0 else 0},{transport}"))
        if s in dup:
            rows.append((tr + 3000, f"{s},{ts},{tr + 3000},{tr + 3000 - ts},0,{transport}"))
    rows.sort()
    with open(cell / "hostb" / CTL.RECV_LOG, "w") as f:
        f.write("seq,t_send_unix_us,t_recv_unix_us,owd_us,probe_token,transport\n")
        for _, r in rows:
            f.write(r + "\n")
        for r in extra_rows:
            f.write(r + "\n")


def read_control_csv(cell: Path) -> list[dict]:
    with (cell / "reduced" / "control.csv").open() as f:
        return list(csv.DictReader(f))


class Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="teleop-control-test-"))
        self.cell = self.tmp / "t-c01"
        for d in ("hosta", "hostb", "reduced"):
            (self.cell / d).mkdir(parents=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def reduce(self, transport=None):
        notes: list[str] = []
        res = CTL.reduce_control(self.cell / "hosta", self.cell / "hostb", E, self.cell / "reduced", notes, transport)
        return res, notes

    def metrics(self, res, probes=None, probe_section=True, dur=6.0):
        res = dict(res, probe_section=probe_section)
        if probes is not None:
            CTL.write_probes(self.cell / "reduced" / "probes.csv", probes)
        return M.control(self.cell / "reduced", {"control": res}, dur, {"control_transport": "data_track_buf1"})


class ReduceControl(Tmp):
    N = 1200          # 6 s at 200 Hz: window [0.5 + 2, 0.5 + 5.995 - 2]

    def test_window_delivery_gaps(self):
        lost = set(range(600, 605)) | {700, 900, 901}
        write_pub(self.cell, range(self.N))
        write_recv(self.cell, [s for s in range(self.N) if s not in lost], owd_ms=lambda s: 30.0 + (s % 4))
        res, notes = self.reduce()
        self.assertEqual(res["window_s"], [2.5, round(0.5 + (self.N - 1) / HZ - 2, 6)])
        self.assertEqual((res["published_total"], res["received_total"], res["duplicates"]), (self.N, self.N - 8, 0))
        # window by send time: seq 400 .. 799 (400 samples), of which 600-604 and 700 are lost
        self.assertEqual((res["published"], res["received"]), (400, 394))
        self.assertIsNone(res["reason"])
        self.assertEqual(res["transport"], "data_track_buf1")
        rows = read_control_csv(self.cell)
        self.assertEqual(list(rows[0]), list(CTL.CONTROL_COLS))
        self.assertEqual(len(rows), self.N)
        r600 = rows[600]
        self.assertEqual((r600["sent"], r600["received"], r600["owd_ms"], r600["in_window"]), ("1", "0", "", "1"))
        self.assertEqual((rows[0]["probe"], rows[1]["probe"]), ("1", "0"))
        m = self.metrics(res)
        self.assertAlmostEqual(m["delivered_pct"], 100 * 394 / 400)
        self.assertEqual(m["gaps"], {"count": 2, "max_consecutive_lost": 5})
        self.assertEqual(m["owd"]["n"], 394)
        self.assertEqual((m["owd"]["min"], m["owd"]["max"]), (30.0, 33.0))
        self.assertIsNotNone(m["jitter_sd_ms"])
        # interarrival between consecutive first arrivals: 5 ms +/- the owd pattern (+1,+1,+1,-3 ms), and
        # 6 x 5 ms - 2 ms across the 5-sample gap (seq 599 arrives 33 ms after sending, seq 605 31 ms)
        ia = m["interarrival"]
        self.assertEqual(ia["n"], 393)
        self.assertEqual(ia["p50"], 6.0)
        self.assertAlmostEqual(ia["max"], 28.0, places=3)

    def test_duplicates_mismatch_and_strangers(self):
        write_pub(self.cell, range(self.N))
        write_recv(self.cell, range(self.N), dup={500, 501}, shift_send={650},
                   extra_rows=["99999,1790000003000000,1790000003030000,30000,0,data_track_buf1"])
        res, notes = self.reduce()
        self.assertEqual(res["duplicates"], 2)
        self.assertEqual(res["t_send_mismatch"], 1)
        self.assertEqual(res["recv_not_published"], 1)
        self.assertTrue(any("another publisher" in n for n in notes))
        self.assertTrue(any("not in A's publisher log" in n for n in notes))
        rows = {int(r["seq"]): r for r in read_control_csv(self.cell)}
        self.assertEqual(rows[500]["dups"], "1")
        self.assertEqual((rows[99999]["sent"], rows[99999]["received"]), ("0", "1"))
        m = self.metrics(res)
        self.assertEqual(m["delivered_pct"], 100.0)              # strangers are not in the denominator
        self.assertEqual(m["gaps"], {"count": 0, "max_consecutive_lost": 0})
        self.assertEqual(m["duplicates"], 2)

    def test_unparsable_lines(self):
        write_pub(self.cell, range(self.N), extra_lines=["{not json", '{"seq": "x"}'])
        write_recv(self.cell, range(self.N), extra_rows=["garbage,,,", ",1,2,3,0,x"])
        res, notes = self.reduce()
        self.assertEqual(res["bad_lines"], {"pub": 2, "recv": 2})
        self.assertTrue(any("unparsable" in n for n in notes))

    def test_missing_publisher_log(self):
        write_recv(self.cell, range(self.N))
        res, _ = self.reduce()
        self.assertIn("control-pub.jsonl absent", res["reason"])
        self.assertIsNone(res["published"])
        self.assertEqual(res["received"], 400)
        rows = read_control_csv(self.cell)
        self.assertEqual(rows[0]["sent"], "")
        m = self.metrics(res)
        self.assertIsNone(m["delivered_pct"])
        self.assertEqual(m["gaps"], {"count": None, "max_consecutive_lost": None})
        self.assertEqual(m["owd"]["n"], 400)                     # B's log alone still gives one-way
        self.assertIn("control-pub.jsonl absent", m["reason"])

    def test_missing_receive_log(self):
        write_pub(self.cell, range(self.N))
        res, _ = self.reduce("dc_reliable")
        self.assertIn("hostb/control.csv absent", res["reason"])
        self.assertEqual(res["transport"], "dc_reliable")      # from the manifest variable
        m = self.metrics(res)
        self.assertIsNone(m["delivered_pct"])
        self.assertEqual(m["owd"]["n"], 0)
        self.assertIsNone(m["owd"]["p50"])
        self.assertIsNone(m["jitter_sd_ms"])
        self.assertEqual(m["published"], 400)
        self.assertIsNone(m["received"])

    def test_both_missing(self):
        (self.cell / "reduced" / "control.csv").write_text("seq,t_s\n1,1.0\n")    # from an earlier reduction
        res, _ = self.reduce()
        self.assertFalse((self.cell / "reduced" / "control.csv").exists())
        self.assertIn("control-pub.jsonl absent", res["reason"])
        self.assertIn("control.csv absent", res["reason"])
        m = self.metrics(res, probe_section=False)
        for k in ("delivered_pct", "jitter_sd_ms", "published", "received"):
            self.assertIsNone(m[k], k)
        for k in ("owd", "interarrival", "rtt"):
            self.assertEqual(m[k]["n"], 0, k)
        self.assertIn("no probe section", m["reason"])

    def test_nothing_received(self):
        write_pub(self.cell, range(self.N))
        write_recv(self.cell, [])
        res, notes = self.reduce()
        self.assertEqual((res["published"], res["received"]), (400, 0))
        self.assertTrue(any("received none" in n for n in notes))
        m = self.metrics(res)
        self.assertEqual(m["delivered_pct"], 0.0)
        self.assertEqual(m["gaps"], {"count": 1, "max_consecutive_lost": 400})

    def test_short_log_has_no_window(self):
        write_pub(self.cell, range(3 * HZ))                        # 3 s < 2 x 2 s
        write_recv(self.cell, range(3 * HZ))
        res, _ = self.reduce()
        self.assertIsNone(res["window_s"])
        self.assertIn("no window", res["reason"])
        m = self.metrics(res)
        self.assertIsNone(m["delivered_pct"])

    def test_probe_rtts_inside_the_window(self):
        write_pub(self.cell, range(self.N))
        write_recv(self.cell, range(self.N))
        res, _ = self.reduce()
        probes = [(0.5, 900.0), (3.0, 60.0), (3.5, 64.0), (4.0, 62.0), (6.2, 900.0)]
        m = self.metrics(res, probes)
        self.assertEqual(m["rtt"]["n"], 3)                        # the start-up and tail samples are outside
        self.assertEqual((m["rtt"]["min"], m["rtt"]["max"]), (60.0, 64.0))
        m = self.metrics(res, [])
        self.assertEqual(m["rtt"]["n"], 0)
        self.assertIn("no probe round trip", m["reason"])


class StatsJsonl(Tmp):
    def test_probes_collected_in_the_one_pass(self):
        p = self.cell / "hosta" / "x.jsonl"
        with open(p, "w") as f:
            f.write(json.dumps({"record": "run_metadata", "encoder_implementation": "NVENC"}) + "\n")
            for k in range(3):
                f.write(json.dumps({"t_unix_us": int((E + 1 + k) * 1e6), "video_out": {"frames_encoded": 30 * k},
                                    "probe": {"probes_sent": 2 * k, "rtt_us_interval": [60000 + k, 61000]}}) + "\n")
            f.write(json.dumps({"t_unix_us": int((E + 5) * 1e6), "probe": {"rtt_us_interval": []}}) + "\n")
            f.write("not json\n")
        s = F.read_webrtc_stats(p, E)
        self.assertEqual(len(s["polls"]), 3)
        self.assertTrue(s["probe_section"])
        self.assertEqual(len(s["probes"]), 6)
        self.assertEqual(s["probes"][0], (1.0, 60.0))
        old = self.cell / "hosta" / "old.jsonl"
        old.write_text(json.dumps({"t_unix_us": int(E * 1e6), "video_out": {"frames_encoded": 1}}) + "\n")
        s = F.read_webrtc_stats(old, E)
        self.assertEqual((s["probes"], s["probe_section"]), ([], False))


class EndToEnd(unittest.TestCase):
    """reduce_cell + metrics.build on test_metrics' synthetic cell with control logs and probes."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="teleop-control-e2e-"))
        self.cell = self.tmp / "t-c01"
        TM.write_synthetic_cell(self.cell)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_pipeline_and_page(self):
        n = 5 * HZ
        write_pub(self.cell, range(n), t0=0.0)
        write_recv(self.cell, [s for s in range(n) if s not in (500, 501, 502)], t0=0.0,
                   owd_ms=lambda s: 25.0 if s % 2 else 35.0)
        with open(self.cell / "hosta" / f"{self.cell.name}.jsonl", "a") as f:
            for k in range(5):
                f.write(json.dumps({"t_unix_us": int((E + k + 0.5) * 1e6),
                                    "probe": {"rtt_us_interval": [70000 + 1000 * k]}}) + "\n")
        reduce_cell(self.cell)
        red = json.loads((self.cell / "reduced" / "reduce.json").read_text())
        self.assertEqual(red["control"]["probe_rtts"], 5)
        self.assertTrue((self.cell / "reduced" / "probes.csv").is_file())
        m = M.build(self.cell)["control"]
        # window: send times 2.0 .. 2.995 s (5 s log minus 2 s each end) = seq 400..599
        self.assertEqual((m["published"], m["received"]), (200, 197))
        self.assertAlmostEqual(m["delivered_pct"], 98.5)
        self.assertEqual(m["gaps"], {"count": 1, "max_consecutive_lost": 3})
        # 99 odd seq at 25 ms, 98 even at 35 ms: the nearest-rank median (index 98 of 197) is 25
        self.assertEqual((m["owd"]["min"], m["owd"]["max"], m["owd"]["p50"]), (25.0, 35.0, 25.0))
        self.assertAlmostEqual(m["jitter_sd_ms"], 5.0, delta=0.05)
        self.assertEqual(m["rtt"]["n"], 1)                       # only the poll at 2.5 s is inside
        self.assertEqual(m["rtt"]["p50"], 72.0)
        self.assertEqual(m["transport"], "data_track_buf1")
        self.assertIsNone(m["reason"])
        on_disk = json.loads((self.cell / "metrics.json").read_text())
        self.assertLessEqual({"delivered_pct", "gaps", "owd", "jitter_sd_ms", "interarrival", "rtt", "transport"},
                             set(on_disk["control"]))
        report_cell.render(self.cell)
        html = (self.cell / "report.html").read_text()
        self.assertIn("Control path (data track)", html)
        self.assertIn("delivered 98.500%", html)
        self.assertIn("gap length (consecutive lost)", html)
        self.assertNotIn("Control metrics incomplete", html)

    def test_page_without_logs(self):
        reduce_cell(self.cell)
        m = M.build(self.cell)["control"]
        self.assertIn("control-pub.jsonl absent", m["reason"])
        report_cell.render(self.cell)
        html = (self.cell / "report.html").read_text()
        self.assertIn("Control metrics incomplete", html)


if __name__ == "__main__":
    unittest.main()
