"""metrics.build(): schema completeness on a synthetic cell, and the two legacy integration checks.

Run from the repo root:  python3 -m unittest teleop.grid.tests.test_metrics -v

The integration checks use cells imported by tools/import_legacy.py under
$TELEOP_LEGACY_ROOT (default ~/teleop-runs/legacy); when a cell is not imported yet but the old
results directory exists, it is imported there first. Both are skipped when neither exists.
Reference values come from the legacy paired reports; their "owd" was capture -> receive
(latency.capture_to_receive here), see CONTRACT.md "added by reduce".
"""
from __future__ import annotations

import csv
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from teleop.grid import metrics
from teleop.grid.reduce import reduce_cell
from teleop.grid.stats import FIELDS

REPO = Path(__file__).resolve().parents[3]
LEGACY_ROOT = Path(os.environ.get("TELEOP_LEGACY_ROOT", str(Path.home() / "teleop-runs" / "legacy")))
OLD_RESULTS = Path(os.environ.get("TELEOP_OLD_RESULTS", str(REPO / "results")))

SCHEMA = {
    "config": ("variables", "requested", "negotiated", "epoch", "commit", "clock_offsets", "ptp", "band",
               "gates_passed", "encoder_is_nvenc"),
    "frame": ("size_kb", "packets_per_frame", "fps_delivered", "frames_captured", "frames_encoded", "frames_sent",
              "frames_received", "frames_rendered", "dropped_pre_encode", "dropped_post_encode", "keyframes",
              "size_spread_pct"),
    "rate": ("bytes_per_s_a", "packets_per_s_a", "bytes_per_s_b", "packets_per_s_b", "kbps_target", "kbps_achieved",
             "padding_bytes_share"),
    "encoder": ("qp", "encode_ms", "quality_limitation_s"),
    "latency": ("app_to_wire_a", "emission_a", "in_flight", "arrival_b", "wire_to_app_b", "decode", "render", "e2e", "owd"),
    "jitter": ("owd_sd_ms", "interarrival_rfc3550_ms", "frame_interval_b_ms"),
    "tail": ("owd_over_100", "owd_over_150", "e2e_over_100", "e2e_over_150", "episodes"),
    "network": ("packets_lost", "loss_events", "nacks_sfu_to_a", "nacks_b_to_sfu", "duplicates_a", "in_flight_packets",
                "path_mtu"),
    "modem": ("a", "b"),
    "integrity": ("captures_complete", "mirror_verified", "reduce_resyncs", "ptp_locked", "excluded", "reasons"),
}
SUMMARIES = {
    "frame": ("size_kb", "packets_per_frame"),
    "rate": ("bytes_per_s_a", "packets_per_s_a", "bytes_per_s_b", "packets_per_s_b"),
    "encoder": ("qp", "encode_ms"),
    "latency": SCHEMA["latency"],
    "jitter": ("interarrival_rfc3550_ms", "frame_interval_b_ms"),
    "network": ("in_flight_packets",),
}
MODEM_KEYS = ("activity_index", "x19ef_records", "kernel_queue_max_bytes", "rsrp_dbm", "snr_db")


def check_schema(tc: unittest.TestCase, m: dict) -> None:
    for g, keys in SCHEMA.items():
        tc.assertIn(g, m)
        for k in keys:
            tc.assertIn(k, m[g], f"{g}.{k}")
    for g, keys in SUMMARIES.items():
        for k in keys:
            tc.assertEqual(set(m[g][k]), set(FIELDS), f"{g}.{k} is not a Summary")
    for h in ("a", "b"):
        for k in MODEM_KEYS:
            tc.assertIn(k, m["modem"][h])
        for k in ("rsrp_dbm", "snr_db"):
            tc.assertEqual(set(m["modem"][h][k]), set(FIELDS))
    tc.assertEqual(set(m["encoder"]["quality_limitation_s"]), {"none", "bandwidth", "cpu", "other"})
    for k in ("owd_over_100", "owd_over_150", "e2e_over_100", "e2e_over_150"):
        tc.assertEqual(set(m["tail"][k]), {"count", "share"})


def write_synthetic_cell(cell: Path, n: int = 90, lost_at: int | None = 60) -> None:
    E = 1_790_000_000
    (cell / "hosta").mkdir(parents=True)
    (cell / "hostb").mkdir()
    label = cell.name
    with open(cell / "hosta" / f"{label}.pub.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["frame_id", "capture_timestamp_us", "webrtc_packetize_timestamp_us", "encode_ms", "frame_id_gap"])
        fid = 0
        for i in range(n):
            fid += 2 if i == 30 else 1               # one frame dropped before the encoder
            cap = int((E + 1 + i / 30) * 1e6)
            w.writerow([fid, cap, cap + 1500, 1.25, "" if i == 0 else (2 if i == 30 else 1)])
    with open(cell / "hostb" / "subscriber.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["frame_id", "capture_timestamp_us", "webrtc_receive_timestamp_us", "frame_gpu_complete_timestamp_us",
                    "decode_ms", "render_ms", "e2e_to_gpu_complete_ms", "packets_lost"])
        for i in range(n):
            if i == 45:
                continue                             # never arrived
            cap = int((E + 1 + i / 30) * 1e6)
            owd = 150 if i == 70 else 30              # one spike
            lost = 0 if lost_at is None or i < lost_at else (3 if i < 75 else 2)   # non-monotonic
            w.writerow([i + 1, cap, cap + 1500 + owd * 1000, cap + 60_000, 5.0, 3.0, 45.0 + (owd - 30), lost])
    with open(cell / "hosta" / f"{label}.jsonl", "w") as f:
        for k in range(4):
            f.write(json.dumps({"poll_index": k, "t_unix_us": int((E + 0.5 + k) * 1e6), "video_out": {
                "frames_encoded": 30 * k, "frames_sent": 30 * k, "key_frames_encoded": 1, "qp_sum": 900 * k,
                "encoder_implementation": "NVIDIA H264 Encoder", "target_bitrate_bps": 2_500_000,
                "quality_limitation_none_s": float(k), "quality_limitation_bandwidth_s": 0.0,
                "quality_limitation_cpu_s": 0.0, "quality_limitation_other_s": 0.0}}) + "\n")
    manifest = {"label": label, "grid_id": "t", "epoch": E, "status": "OK",
                "variables": {"codec": "h264", "kbps": 2500, "duration_s": 4, "padding": True},
                "clock": {"a": {"host_minus_utc_s": -15.8}, "b": {"host_minus_utc_s": -15.8}},
                "ptp": {"a": {"state": "MASTER"}, "b": {"state": "SLAVE", "servo_lines_30s": 30, "offset_ns": 900}},
                "gates": {"a": [{"name": "link MTU", "pass": True, "detail": "path MTU 1400"}], "b": []}}
    (cell / "manifest.json").write_text(json.dumps(manifest))


class SyntheticCellTest(unittest.TestCase):
    def test_schema_and_values(self):
        with tempfile.TemporaryDirectory() as d:
            cell = Path(d) / "t-c01"
            write_synthetic_cell(cell)
            reduce_cell(cell)
            m = metrics.build(cell)
            check_schema(self, m)
            on_disk = json.loads((cell / "metrics.json").read_text())
            self.assertEqual(on_disk["frame"]["frames_received"], 89)
            self.assertEqual(m["frame"]["frames_captured"], 91)
            self.assertEqual(m["frame"]["dropped_pre_encode"], 1)
            self.assertEqual(m["frame"]["dropped_post_encode"], 1)
            self.assertEqual(m["latency"]["owd"]["p50"], 30.0)
            self.assertEqual(m["latency"]["owd"]["max"], 150.0)
            self.assertEqual(m["tail"]["owd_over_100"]["count"], 1)
            self.assertEqual(len(m["tail"]["episodes"]), 1)
            self.assertEqual(m["tail"]["episodes"][0]["dominant_segment"], None)   # no wire: unjoined
            self.assertEqual(m["network"]["packets_lost"], 3)
            self.assertEqual(m["network"]["loss_events"], 1)
            self.assertEqual(m["network"]["path_mtu"], 1400)
            self.assertAlmostEqual(m["encoder"]["qp"]["p50"], 30.0)
            self.assertTrue(m["config"]["encoder_is_nvenc"])
            self.assertTrue(m["config"]["gates_passed"])
            self.assertTrue(m["integrity"]["ptp_locked"])
            self.assertFalse(m["integrity"]["captures_complete"])       # no pcaps, DLFs, hops
            self.assertEqual(m["latency"]["in_flight"]["n"], 0)
            self.assertIsNone(m["latency"]["in_flight"]["p50"])
            self.assertIsNone(m["rate"]["padding_bytes_share"])
            self.assertIsNone(m["modem"]["a"]["x19ef_records"])
            self.assertIsNone(m["config"]["band"]["a"])
            self.assertEqual(m["modem"]["a"]["activity_index"]["n"], 0)

    def test_empty_reduced_dir_still_full_schema(self):
        with tempfile.TemporaryDirectory() as d:
            cell = Path(d) / "empty"
            cell.mkdir()
            (cell / "manifest.json").write_text(json.dumps({"label": "empty", "epoch": 1}))
            m = metrics.build(cell)
            check_schema(self, m)
            self.assertFalse(m["integrity"]["captures_complete"])


def legacy(name: str) -> Path | None:
    cell = LEGACY_ROOT / "cells" / name
    if (cell / "manifest.json").exists():
        return cell
    old = OLD_RESULTS / name
    if (old / "EPOCH").exists():
        from teleop.grid.tools.import_legacy import import_cell
        return import_cell(old, LEGACY_ROOT)
    return None


def run_legacy(name: str) -> dict:
    cell = legacy(name)
    reduce_cell(cell)
    return metrics.build(cell)


@unittest.skipUnless(legacy("vbv-2500kbps"), "legacy cell vbv-2500kbps not available")
class Legacy2500Test(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = run_legacy("vbv-2500kbps")

    def test_schema(self):
        check_schema(self, self.m)

    def test_reference_values(self):
        m = self.m
        c2r = m["latency"]["capture_to_receive"]         # the legacy report's "owd"
        self.assertAlmostEqual(c2r["p50"], 33.1, delta=0.1)
        self.assertAlmostEqual(c2r["p99"], 53.4, delta=0.3)
        self.assertAlmostEqual(c2r["max"], 91.9, delta=0.1)
        self.assertAlmostEqual(m["latency"]["e2e"]["p50"], 44.4, delta=0.1)
        self.assertEqual(m["frame"]["frames_received"], 1698)
        self.assertEqual(m["network"]["packets_lost"], 0)
        self.assertAlmostEqual(m["frame"]["size_kb"]["p50"], 10.6, delta=0.1)
        # owd per the contract (packetize -> receive) is capture -> receive minus ~1.4 ms of encode
        self.assertAlmostEqual(m["latency"]["owd"]["p50"], 31.7, delta=0.2)
        self.assertEqual(m["latency"]["owd"]["n"], 1698)

    def test_join_and_band(self):
        m = self.m
        self.assertGreaterEqual(m["latency"]["in_flight"]["n"], 1698)
        self.assertLess(abs(m["latency"]["app_to_wire_a"]["p50"]), 1.0)
        self.assertEqual(m["integrity"]["reduce_resyncs"], 0)
        self.assertEqual(m["config"]["band"]["a"]["band"], "n41")
        self.assertEqual(m["config"]["band"]["b"]["band"], "n41")
        self.assertTrue(m["modem"]["a"]["dlf_present"])
        self.assertEqual(m["modem"]["a"]["activity_index"]["n"], 60)
        self.assertTrue(m["integrity"]["captures_complete"])


@unittest.skipUnless(legacy("vbv-8000kbps"), "legacy cell vbv-8000kbps not available")
class Legacy8000Test(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = run_legacy("vbv-8000kbps")

    def test_schema(self):
        check_schema(self, self.m)

    def test_reference_values(self):
        m = self.m
        c2r = m["latency"]["capture_to_receive"]
        self.assertAlmostEqual(c2r["p50"], 37.4, delta=0.1)
        self.assertAlmostEqual(c2r["p99"], 86.5, delta=0.5)
        self.assertAlmostEqual(c2r["max"], 203.4, delta=0.1)
        self.assertEqual(m["tail"]["capture_to_receive_over_100"]["count"], 44)
        self.assertEqual(m["network"]["packets_lost"], 0)
        self.assertEqual(m["tail"]["owd_over_100"]["count"], 38)

    def test_missing_a_modem_path(self):
        m = self.m
        self.assertFalse(m["modem"]["a"]["dlf_present"])
        self.assertEqual(m["modem"]["a"]["activity_index"]["n"], 0)
        self.assertIsNone(m["modem"]["a"]["x19ef_records"])
        self.assertIsNone(m["modem"]["a"]["kernel_queue_max_bytes"])
        self.assertIsNone(m["config"]["band"]["a"])
        self.assertEqual(m["config"]["band"]["b"]["band"], "n41")
        self.assertFalse(m["integrity"]["captures_complete"])
        self.assertIn("missing: a.dlf", m["integrity"]["reasons"])
        self.assertTrue(m["modem"]["b"]["dlf_present"])
        self.assertEqual(m["tail"]["spike_kinds"].get("transient", 0), 0)   # no A modem: cannot call a transient


if __name__ == "__main__":
    unittest.main()
