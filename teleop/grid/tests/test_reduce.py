"""Unit tests for teleop.grid.reduce on small synthetic inputs, plus an A-only integration check.

Run from the repo root:  python3 -m unittest teleop.grid.tests.test_reduce -v
(pytest also collects these). Everything is written to temporary directories.
"""
from __future__ import annotations

import csv
import json
import os
import shutil
import struct
import tempfile
import unittest
from pathlib import Path

from teleop.grid.reduce import dlf_rates, dlf_records, frames as F, ml1, pcap_extract, segjoin

GPS_EPOCH = dlf_records.GPS_EPOCH
LEGACY_ROOT = Path(os.environ.get("TELEOP_LEGACY_ROOT", str(Path.home() / "teleop-runs" / "legacy")))


def dlf_ts(unix: float) -> int:
    return int((unix - GPS_EPOCH) / 1.25e-3) << 16


def dlf_rec(code: int, unix: float, payload: bytes = b"") -> bytes:
    body = struct.pack("<Q", dlf_ts(unix)) + payload
    return struct.pack("<HH", 4 + len(body), code) + body


def ml1_rec(unix: float, blocks: list[tuple[int, int, int, float]], length: int) -> bytes:
    """0xB97F record of `length` bytes with carrier blocks at the given record offsets."""
    b = bytearray(length)
    struct.pack_into("<HH", b, 0, length, ml1.ML1_CODE)
    struct.pack_into("<Q", b, 4, dlf_ts(unix))
    for off, pci, arfcn, brsrp in blocks:
        struct.pack_into("<I", b, off + 32, arfcn)
        struct.pack_into("<H", b, off + 38, pci)
        struct.pack_into("<H", b, off + 64, pci)
        struct.pack_into("<i", b, off + 72, int(brsrp * 128))
    return bytes(b)


class DlfRecordsTest(unittest.TestCase):
    def test_resync_over_garbage(self):
        data = dlf_rec(0xB0C0, 1.79e9, b"x" * 8) + b"\xff\xff\x00" + dlf_rec(0xB0C1, 1.79e9 + 1, b"y" * 4)
        st = {}
        recs = list(dlf_records.records(data, stats=st))
        self.assertEqual([r[0] for r in recs], [0xB0C0, 0xB0C1])
        self.assertEqual(st["resyncs"], 1)
        self.assertEqual(st["skipped_bytes"], 3)

    def test_iter_file_matches_records(self):
        data = b"".join(dlf_rec(0xB000 + i % 5, 1.79e9 + i / 10) for i in range(200)) + b"\x00" * 5
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(data)
        try:
            a = list(dlf_records.records(data))
            b = list(dlf_records.iter_file(f.name, chunk=100))
            self.assertEqual(a, b)
        finally:
            os.unlink(f.name)


class DlfRatesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_window_drops_garbage_timestamps_and_offset_applies(self):
        E = 1_790_000_000.0
        off = -15.8                      # host = modem + off
        recs = []
        for s in range(0, 30):
            for _ in range(10):          # 10 ELEV records per second, host time s .. s+1
                recs.append(dlf_rec(0xB8C4, E + s + 0.5 - off))
        recs.append(dlf_rec(0xB8C4, 368_000_000.0))       # 1981
        recs.append(dlf_rec(0xB8C4, 2.3e11))               # year ~9000
        recs.append(dlf_rec(0x19EF, E + 3.2 - off))
        p = self.tmp / "x.dlf"
        p.write_bytes(b"".join(recs))
        s = dlf_rates.scan(p, off, E, duration_s=30, margin_s=5, want_ml1=False)
        self.assertEqual(s["records_total"], 303)
        self.assertEqual(s["records_outside_window"], 2)
        secs = sorted({k[0] for k in s["counts"]})
        self.assertEqual(secs[0], 0)
        self.assertEqual(secs[-1], 29)
        out = self.tmp / "r.csv"
        dlf_rates.write_csv(out, s["counts"], off, E, 30, s["records_total"], s["stats"])
        head = out.read_text().splitlines()[0]
        self.assertIn("host_minus_utc_s=-15.800", head)
        self.assertIn(f"probe_start={E:.3f}", head)
        self.assertEqual(dlf_rates.read_header_offset(out), -15.8)
        per = dlf_rates.read_csv(out, E)
        self.assertEqual(per["0xB8C4"][7], 10)
        self.assertEqual(dlf_rates.code_count(per, "0x19EF", 0, 30), 1)
        act = dlf_rates.activity_index(per, 30)
        self.assertAlmostEqual(act["steady_per_s"], 10.0)
        self.assertAlmostEqual(act["index"][3], 1.0)

    def test_read_csv_rebases_other_probe_start(self):
        p = self.tmp / "b.csv"
        p.write_text("# host_minus_utc_s=-15.802 probe_start_ms=1790706733000 probe_end_ms=1790706793000\n"
                     "second_rel_probe,code,count\n0,0xb8c4,4\n")
        per = dlf_rates.read_csv(p, 1790706732)
        self.assertEqual(per["0xB8C4"][1], 4)


class Ml1Test(unittest.TestCase):
    def test_band_from_arfcn(self):
        self.assertEqual(ml1.band(501390), "n41")          # 2506.95 MHz
        self.assertEqual(ml1.band(126900), "n71")          # 634.5 MHz
        self.assertEqual(ml1.band(396000), "n25")          # 1980 MHz: n2/n25 overlap -> n25
        self.assertEqual(ml1.band_raw(396000), "n2/n25")
        self.assertEqual(ml1.band(398500), "n25")          # 1992.5 MHz: n25 only
        self.assertEqual(ml1.band(520000), "n41")          # 2600 MHz: n38/n41 overlap -> n41

    def test_block_discovery_variable_stride(self):
        # PCell at +0 with SCell at +212 (396-byte record) and at +272 (516-byte record)
        r1 = ml1_rec(1.79e9, [(0, 85, 501390, -88.5), (212, 301, 396000, -95.0)], 396)
        r2 = ml1_rec(1.79e9, [(0, 85, 501390, -88.0), (272, 301, 396000, -96.0)], 516)
        b1, b2 = ml1.parse_record(r1), ml1.parse_record(r2)
        self.assertEqual([(o, p, a) for o, p, a, _ in b1], [(0, 85, 501390), (212, 301, 396000)])
        self.assertEqual([(o, p, a) for o, p, a, _ in b2], [(0, 85, 501390), (272, 301, 396000)])
        self.assertAlmostEqual(b1[0][3], -88.5)

    def test_pci_copies_must_agree(self):
        r = bytearray(ml1_rec(1.79e9, [(0, 85, 501390, -88.0)], 200))
        struct.pack_into("<H", r, 64, 86)
        self.assertEqual(ml1.parse_record(bytes(r)), [])

    def test_collector_majority_and_blank_accounting(self):
        c = ml1.Collector()
        for i in range(8):
            c.add(1.79e9 + i, i, ml1_rec(1.79e9, [(0, 85, 501390, -88.0), (212, 301, 396000, -95.0)], 396))
        c.add(1.79e9 + 9, 9, ml1_rec(1.79e9, [(0, 7, 126900, -100.0)], 396))
        c.add(1.79e9 + 10, 10, ml1_rec(1.79e9, [], 396))
        s = c.summary()
        self.assertEqual((s["band"], s["arfcn"], s["pci"]), ("n41", 501390, 85))
        self.assertAlmostEqual(s["share"], 0.8)
        self.assertEqual(s["scell"]["band"], "n25")
        self.assertEqual(s["records_without_block"], 1)

    def test_scan_collects_ml1_in_one_pass(self):
        E = 1_790_000_000.0
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "m.dlf"
            p.write_bytes(dlf_rec(0xB8C4, E + 1) + ml1_rec(E + 2, [(0, 85, 501390, -88.0)], 304)
                          + ml1_rec(9.9e10, [(0, 1, 501390, -88.0)], 304))
            s = dlf_rates.scan(p, 0.0, E, 10, 5)
            self.assertEqual(s["ml1"].records, 1)            # the garbage-timestamp one is excluded
            s["ml1"].write_csv(Path(d) / "band.csv")
            with open(Path(d) / "band.csv") as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(rows[0]["band"], "n41")
            self.assertEqual(rows[0]["carrier"], "pcell")


def pkt_line(t, src, dst, udplen, pt="", seq="", ts="", marker="", rtcp="", fmt=""):
    return "\t".join(map(str, [t, src, dst, 5000, 6000, udplen, pt, seq, ts, marker, "0x1" if pt != "" else "", rtcp, fmt]))


LOCAL_A, LOCAL_B, SFU = "10.0.0.2", "10.0.0.3", "192.0.2.10"


def synth_wire(E, n_frames, probe_frames=0, spurious_at=None, pkts_per=3, role="a", start=1.0):
    """Lines for a wire: `probe_frames` single-packet RTP 'frames' before media, one spurious one mid-run."""
    lines = []
    src, dst = (LOCAL_A, SFU) if role == "a" else (SFU, LOCAL_B)
    seq = 100
    ts0 = 4_294_900_000                       # near the wrap on purpose
    for k in range(probe_frames):
        lines.append(pkt_line(E + start - 0.1 + k * 0.01, src, dst, 100, 96, seq, (ts0 - 9000 + k * 90) % 2**32, 0))
        seq += 1
    for i in range(n_frames):
        cap = E + start + i / 30
        ts = (ts0 + round((cap - E - start) * 1000) * 90) % 2**32
        base = cap + 0.002 + (0.030 if role == "b" else 0)
        for j in range(pkts_per):
            lines.append(pkt_line(base + j * 0.0005, src, dst, 1200, 96, seq, ts, 1 if j == pkts_per - 1 else 0))
            seq += 1
        if spurious_at is not None and i == spurious_at:
            lines.append(pkt_line(base + 0.01, src, dst, 60, 96, seq, (ts + 45 * 90) % 2**32, 0))
            seq += 1
    return lines


def synth_pub(E, n_frames, start=1.0):
    return [{"frame_id": i + 1, "capture_us": int((E + start + i / 30) * 1e6),
             "packetize_us": int((E + start + i / 30 + 0.0015) * 1e6), "encode_ms": 1.2, "frame_id_gap": 1}
            for i in range(n_frames)]


class PcapAndJoinTest(unittest.TestCase):
    E = 1_790_000_000.0

    def test_parse_and_flow_and_nack(self):
        lines = synth_wire(self.E, 10) + [
            pkt_line(self.E + 2, SFU, LOCAL_A, 40, rtcp="205", fmt="1"),
            pkt_line(self.E + 2.1, SFU, LOCAL_A, 40, rtcp="205", fmt="15"),
            pkt_line(self.E + 2.2, SFU, LOCAL_A, 60, rtcp="200;205", fmt="1"),
            pkt_line(self.E + 2.3, LOCAL_A, "198.51.100.1", 50),           # unrelated non-RTP
        ]
        pk = pcap_extract.parse_lines(lines)
        fl = pcap_extract.find_flow(pk, "a")
        self.assertEqual((fl.local, fl.sfu, fl.video_pt), (LOCAL_A, SFU, 96))
        w = segjoin.build_wire(pk, fl, "a")
        self.assertEqual(len(w.frames), 10)
        self.assertEqual(len(w.nack_rx), 2)                   # fmt 15 (TWCC) is not a NACK
        self.assertEqual(w.rtcp_rx, 3)
        self.assertEqual(w.order[0], 4_294_900_000)           # wrap handled: first frame sorts first

    def test_tshark_cmd_field_list(self):
        cmd = pcap_extract.tshark_cmd("x.pcap")
        self.assertIn("rtp.heuristic_rtp:TRUE", cmd)
        fields = [cmd[i + 1] for i, c in enumerate(cmd) if c == "-e"]
        self.assertEqual(tuple(fields), pcap_extract.FIELDS)
        self.assertEqual(len(fields), 13)

    def test_duplicates(self):
        lines = synth_wire(self.E, 3)
        lines.append(lines[1])                                # same seq again 0 s later
        pk = pcap_extract.parse_lines(lines)
        w = segjoin.build_wire(pk, pcap_extract.find_flow(pk, "a"), "a")
        self.assertEqual(len(w.dup_times), 1)

    def test_shift_skips_probe_frames(self):
        E = self.E
        pk = pcap_extract.parse_lines(synth_wire(E, 1200, probe_frames=2))
        wa = segjoin.build_wire(pk, pcap_extract.find_flow(pk, "a"), "a")
        pub = synth_pub(E, 1200)
        shift, med, frm = segjoin.choose_shift(pub, wa, E)
        self.assertEqual((shift, frm), (2, 30.0))
        self.assertAlmostEqual(med, 0.5, places=1)
        self.assertEqual(segjoin.order_agreement(pub, wa, shift), 1.0)
        self.assertEqual(segjoin.resolve(pub, wa, shift, E, frm)[1], 0)

    def test_shift_and_resync(self):
        """A spurious wire 'frame' mid-run: the steady-state shift is taken after it (3), and the
        rows before it leave the order join once and are re-joined by timestamp."""
        E = self.E
        n = 1200
        pk = pcap_extract.parse_lines(synth_wire(E, n, probe_frames=2, spurious_at=700))
        fl = pcap_extract.find_flow(pk, "a")
        wa = segjoin.build_wire(pk, fl, "a")
        pub = synth_pub(E, n)
        shift, med, frm = segjoin.choose_shift(pub, wa, E)
        self.assertEqual(shift, 3)
        wire_ts, resyncs = segjoin.resolve(pub, wa, shift, E, frm)
        self.assertEqual(resyncs, 1)
        self.assertTrue(all(t is not None for t in wire_ts))
        pkb = pcap_extract.parse_lines(synth_wire(E, n, role="b"))
        wb = segjoin.build_wire(pkb, pcap_extract.find_flow(pkb, "b"), "b")
        sub = {p["capture_us"]: {"webrtc_receive_timestamp_us": p["capture_us"] + 34_000,
                                 "frame_gpu_complete_timestamp_us": 1, "decode_ms": 5.0, "render_ms": 3.0,
                                 "e2e_to_gpu_complete_ms": 45.0, "packets_lost": 0}
               for p in pub}
        rows = segjoin.join(pub, sub, wa, wb, E, wire_ts)
        self.assertAlmostEqual(rows[100]["app_to_wire_a"], 0.5, places=1)
        r = rows[900]
        self.assertAlmostEqual(r["app_to_wire_a"], 0.5, places=1)
        self.assertAlmostEqual(r["emission_a"], 1.0, places=1)
        self.assertAlmostEqual(r["in_flight"], 30.0, places=1)
        self.assertAlmostEqual(r["arrival_b"], 1.0, places=1)
        self.assertAlmostEqual(r["owd"], 32.5, places=1)
        self.assertAlmostEqual(r["capture_to_receive"], 34.0, places=1)
        self.assertAlmostEqual(r["wire_to_app_b"], 34.0 - 33.0, places=1)
        self.assertEqual(r["packets_a"], 3)


class FramesTest(unittest.TestCase):
    def test_loss_timeline_non_monotonic(self):
        total, ev = F.loss_timeline([(0.1, 0), (1.2, 3), (1.5, 2), (2.2, 2), (3.1, 5), (4.0, 1), (5.5, 4)])
        self.assertEqual(total, 5)
        self.assertEqual(ev, {1: 3, 3: 3, 5: 3})

    def test_classify(self):
        self.assertEqual(F.classify(5, "in_flight", 1.0, 3.0, 0), "transient")
        self.assertEqual(F.classify(5, "in_flight", 1.0, 1.0, 0), "inflight")
        self.assertEqual(F.classify(5, "in_flight", 1.0, None, 1500), "transient")
        self.assertEqual(F.classify(50, "in_flight", 1.0, 3.0, 0), "inflight")
        self.assertEqual(F.classify(50, "emission_a", 2.5, None, None), "bigframe")
        self.assertEqual(F.classify(50, "arrival_b", 1.1, None, None), "other")
        self.assertEqual(F.classify(50, "wire_to_app_b", 1.0, None, None), "bhost")
        self.assertEqual(F.classify(50, None, 1.0, None, None), "unjoined")

    def test_spikes_and_episodes(self):
        def row(t, owd, inf=30.0, size=1.0):
            return {"t_s": t, "owd": owd, "bytes": 1000 * size, "size_ratio": size, "packets_a": 3,
                    "app_to_wire_a": 0.1, "emission_a": 1.0, "in_flight": inf, "arrival_b": 2.0, "wire_to_app_b": 0.1}
        rows = [row(t / 10, 35) for t in range(300)]
        rows[200] = row(20.0, 150, inf=140)
        rows[205] = row(20.5, 120, inf=110)
        rows[280] = row(28.0, 130, inf=120)
        med = F.steady_medians(rows)
        sp, ep = F.spikes_and_episodes(rows, med, {}, {}, [20.1, 25.0])
        self.assertEqual(len(sp), 3)
        self.assertEqual(sp[0]["dominant"], "in_flight")
        self.assertEqual(sp[0]["b_nacks_0_3s"], 1)
        self.assertEqual(len(ep), 2)
        self.assertEqual((ep[0]["n"], ep[0]["max_ms"]), (2, 150))

    def test_inflight_samples(self):
        s = F.inflight_samples([0.05, 0.06, 0.25], [0.08, 0.30], 0.4, hz=10)
        self.assertEqual([v for _, v in s], [0, 1, 1, 1])

    def test_finish_frames_jitter(self):
        rows = [{"t_s": 20 + i, "bytes": 100 + i, "owd": 30 + (i % 2) * 16, "receive_us": i * 33_000} for i in range(4)]
        med = F.finish_frames(rows)
        self.assertEqual(med, 101.5)
        self.assertAlmostEqual(rows[1]["jitter_rfc3550"], 1.0)
        self.assertAlmostEqual(rows[1]["interval_b"], 33.0)


def _legacy_cell(name):
    p = LEGACY_ROOT / "cells" / name
    return p if (p / "manifest.json").exists() else None


@unittest.skipUnless(_legacy_cell("vbv-2500kbps"), "imported legacy cell vbv-2500kbps not present")
class AOnlyCellTest(unittest.TestCase):
    """hostb/ missing: reduce must still produce what it can and mark integrity."""

    def test_a_only(self):
        from teleop.grid import metrics
        from teleop.grid.reduce import reduce_cell
        src = _legacy_cell("vbv-2500kbps")
        tmp = Path(tempfile.mkdtemp(dir=LEGACY_ROOT))   # same filesystem: hard links, no copies
        try:
            cell = tmp / "a-only"
            (cell / "hosta").mkdir(parents=True)
            for f in (src / "hosta").iterdir():
                if f.suffix != ".dlf":                    # keep the test quick; exercises no-DLF too
                    os.link(f, cell / "hosta" / f.name)
            m = json.loads((src / "manifest.json").read_text())
            m["label"] = "vbv-2500kbps"
            (cell / "manifest.json").write_text(json.dumps(m))
            reduce_cell(cell)
            red = json.loads((cell / "reduced" / "reduce.json").read_text())
            self.assertIn("hostb/", red["missing"])
            self.assertIn("a.dlf", red["missing"])
            mt = metrics.build(cell)
            self.assertIsNone(mt["latency"]["owd"]["p50"])
            self.assertEqual(mt["latency"]["owd"]["n"], 0)
            self.assertIsNotNone(mt["latency"]["app_to_wire_a"]["p50"])
            self.assertAlmostEqual(mt["frame"]["size_kb"]["p50"], 10.6, delta=0.2)
            self.assertFalse(mt["integrity"]["captures_complete"])
            self.assertIsNone(mt["network"]["packets_lost"])
            self.assertIsNone(json.loads((cell / "manifest.json").read_text())["band"]["b"])
        finally:
            shutil.rmtree(tmp)


if __name__ == "__main__":
    unittest.main()
