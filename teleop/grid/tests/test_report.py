"""Report tests: synthetic cells and grids built in a temp dir, rendered, checked.

    python3 -m unittest teleop.grid.tests.test_report -v      (from the repo root, Host A)

No data file is committed; everything is generated here. Nothing is written outside
the temporary directory, except the optional legacy-cell check (see the last test), which
renders into that cell's own directory as the contract prescribes.
"""
from __future__ import annotations

import csv
import json
import math
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import numpy as np

from teleop.grid import stats
from teleop.grid.report import cell as report_cell
from teleop.grid.report import grid as report_grid

GRID_ID = "gtest01"
EPOCH = 1_790_384_410
SEGS = ["app_to_wire_a", "emission_a", "in_flight", "arrival_b", "wire_to_app_b", "decode", "render"]
LEGACY_CELL = Path("/home/nsusser/teleop-runs/legacy/cells/vbv-2500kbps")


def _geometry(kbps: int, fps: int = 30, bpp: float = 0.10) -> tuple[int, int]:
    px = kbps * 1000 / (fps * bpp)
    h = math.sqrt(px * 1300 / 1600)
    w, h = int(h * 1600 / 1300) // 16 * 16, int(h) // 16 * 16
    return max(160, min(1600, w)), max(128, min(1300, h))


def _write_csv(path: Path, head: list[str], rows: list[list]) -> None:
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(head)
        for r in rows:
            w.writerow(["" if (v is None or (isinstance(v, float) and not math.isfinite(v))) else v for v in r])


def make_cell(grid_dir: Path, index: int, codec: str, kbps: int, repeat: int = 1, *, duration_s: int = 60,
              status: str = "OK", encoder: str = "NVIDIA NVENC", ptp_locked: bool = True,
              reduced: bool = True, spikes_csv: bool = True, seed: int | None = None, alt_names: bool = False,
              extra_vars: dict | None = None) -> Path:
    """One realistic cell: manifest.json, metrics.json and (optionally) reduced/*.csv.

    Column names are the canonical ones reduce/ writes (CONTRACT.md "Reduced tables");
    alt_names=True writes the `_ms`-suffixed spellings the report also accepts.
    """
    rng = np.random.default_rng(seed if seed is not None else index * 7919 + kbps)
    fps = 30
    w, h = _geometry(kbps)
    variables = {"codec": codec, "kbps": kbps, "fps": fps, "resolution": "auto", "width": w, "height": h,
                 "bpp": 0.10, "vbv_frames": 1,
                 "padding": "on", "target_quality": "off", "intra_refresh": 0, "pin_bitrate": "on",
                 "duration_s": duration_s, "clip": "/media/clip-src-30s.mp4", "lead_s": 60}
    variables.update(extra_vars or {})
    vtag = "".join(f"-{k[:1]}{v}" for k, v in (extra_vars or {}).items())
    label = f"{GRID_ID}-c{index:02d}-{codec}-{kbps}k-{w}x{h}-v1-p1{vtag}-r{repeat}"
    cd = grid_dir / "cells" / label
    (cd / "reduced").mkdir(parents=True, exist_ok=True)

    # ---- frames ---------------------------------------------------------------------
    n = duration_s * fps
    t = np.arange(n) / fps + rng.uniform(0, 0.002, n)
    budget = kbps * 1000 / 8 / fps
    key = (np.arange(n) % 300) == 0
    size = budget * rng.lognormal(0, 0.28, n)
    size[key] *= 4.0
    size = np.round(size)
    pk_a = np.ceil(size / 1150).astype(int)
    app = rng.gamma(2.0, 0.6, n)
    emis = pk_a * 0.35 + rng.gamma(1.5, 0.3, n)
    infl = 28 + rng.gamma(2.0, 3.0, n)
    # two path-side stall bursts (~400 ms arrival stalls, as seen on the rig)
    for b0 in (rng.uniform(10, 25), rng.uniform(35, 50)):
        m = (t >= b0) & (t < b0 + 0.4)
        infl[m] += np.linspace(380, 20, m.sum())
    arr = rng.gamma(1.5, 0.8, n) + pk_a * 0.05
    hold = rng.random(n) < 0.05
    w2a = rng.gamma(2.0, 1.0, n) + np.where(hold, 50.0, 0.0)
    dec = rng.gamma(3.0, 1.2 if codec == "h264" else 2.1, n)
    ren = rng.gamma(4.0, 1.1, n)
    enc = rng.gamma(4.0, 0.9 if codec == "h264" else 1.4, n)
    c2p = enc + rng.gamma(2.0, 0.5, n)
    owd = app + emis + infl + arr + w2a
    e2e = c2p + owd + dec + ren
    received = (rng.random(n) > 0.001).astype(int)
    rendered = received * (rng.random(n) > 0.002).astype(int)
    for arr_ in (arr, w2a, dec, ren, owd, e2e, infl):
        arr_[received == 0] = np.nan
    pk_b = np.where(received == 1, pk_a, np.nan)
    qp_base = 38 - 6 * math.log2(kbps / 512) + (3 if codec == "av1" else 0)
    qp_frame = np.clip(qp_base + rng.normal(0, 2.5, n) - key * 6, 1, 51)

    if reduced:
        if alt_names:
            head = ["frame_id", "t_s", "rtp_ts", "keyframe", "size_bytes", "packets_a", "packets_b", "qp", "encode_ms",
                    "capture_to_packetize_ms"] + [f"{s}_ms" for s in SEGS] + ["owd_ms", "e2e_ms", "received", "rendered"]
        else:
            head = ["frame_id", "t_s", "rtp_ts", "keyframe", "bytes", "packets_a", "packets_b", "qp", "encode_ms",
                    "capture_to_packetize_ms"] + SEGS + ["owd", "e2e", "received", "rendered"]
        rows = []
        for i in range(n):
            rows.append([1000 + i, round(t[i], 4), 90000 * i // fps, int(key[i]), int(size[i]), int(pk_a[i]),
                         None if math.isnan(pk_b[i]) else int(pk_b[i]), round(qp_frame[i], 1), round(enc[i], 3),
                         round(c2p[i], 3)] + [round(v, 3) for v in (app[i], emis[i], infl[i], arr[i], w2a[i], dec[i], ren[i])]
                        + [round(owd[i], 3), round(e2e[i], 3), received[i], rendered[i]])
        _write_csv(cd / "reduced" / "frames.csv", head, rows)

        # ---- seconds ----------------------------------------------------------------
        srows = []
        for s in range(duration_s):
            m = (t >= s) & (t < s + 1)
            pad = budget * fps * 0.04
            srows.append([s, int(size[m].sum() * 1.06 + pad), int(pk_a[m].sum() + 3),
                          int(np.nansum(np.where(received[m] == 1, size[m], 0)) * 1.06 + pad), int(np.nansum(pk_b[m]) + 3),
                          int(pad), int(m.sum()), int(received[m].sum()), int(rendered[m].sum()),
                          round(float(qp_frame[m].mean()), 2), kbps, round(float(enc[m].mean()), 3)])
        _write_csv(cd / "reduced" / "seconds.csv",
                   (["t_s", "bytes_a", "packets_a", "bytes_b", "packets_b"] if alt_names else
                    ["t", "a_bytes", "a_packets", "b_bytes", "b_packets"])
                   + ["padding_bytes_a", "frames", "frames_received", "frames_rendered",
                      "qp_mean" if alt_names else "qp", "target_kbps", "encode_ms_mean"], srows)

        # ---- spikes -------------------------------------------------------------------
        if spikes_csv:
            med = float(np.median(size))
            segm = np.vstack([app, emis, infl, arr, w2a, dec, ren])
            sp = []
            for i in np.nonzero(np.nan_to_num(owd, nan=-1) > 100)[0]:
                dom = SEGS[int(np.nanargmax(segm[:, i]))]
                r = size[i] / med
                kind = "key" if key[i] else "large" if r >= 2 else "path" if dom == "in_flight" else "hold_b"
                sp.append([1000 + i, round(t[i], 4), round(owd[i], 2), round(e2e[i], 2), dom, int(size[i]), round(r, 3),
                           int(key[i]), kind])
            _write_csv(cd / "reduced" / "spikes.csv",
                       ["frame_id", "t_s", "owd_ms", "e2e_ms", "dominant_segment", "size_bytes", "size_ratio", "keyframe", "kind"]
                       if alt_names else
                       ["frame_id", "t", "owd", "e2e", "dominant", "bytes", "size_ratio", "keyframe", "kind"], sp)

        # ---- modem ------------------------------------------------------------------
        for hname in ("a", "b"):
            probe_start = EPOCH - 60
            lines = [f"# parser=synthetic; host_minus_utc_s={-15.8 if hname == 'a' else -16.1:+.3f}; "
                     f"probe_start={probe_start:.3f} probe_end={EPOCH + duration_s + 30:.3f}; records_total=0",
                     "second_rel_probe,code,count"]
            for sec in range(0, duration_s + 90):
                for code, base in (("0xB872", 400), ("0xB873", 220), ("0xB881", 238), ("0xB883", 90),
                                   ("0xB97F", 40), ("0x1FEB", 900)):
                    lines.append(f"{sec},{code},{int(base * rng.uniform(0.85, 1.15))}")
            (cd / "reduced" / f"dlf-rates-{hname}.csv").write_text("\n".join(lines) + "\n")
            brows = [[s, "n41", 520110 if hname == "a" else 521310, 123 if hname == "a" else 77] for s in range(duration_s)]
            _write_csv(cd / "reduced" / f"band-{hname}.csv", ["t_s", "band", "arfcn", "pci"], brows)

    # ---- manifest -------------------------------------------------------------------
    excluded = status != "OK"
    gates_a = [{"name": g, "pass": True, "detail": ""} for g in
               ("code match", "PTP", "clock offset", "DIAG port free", "capture leftovers", "disk", "modem", "SFU",
                "clip", "room", "encoder", "link MTU")]
    gates_b = [{"name": g, "pass": True, "detail": ""} for g in
               ("code match", "PTP", "clock offset", "DIAG port free", "capture leftovers", "disk", "modem", "B decoder")]
    if not ptp_locked:
        gates_b[1] = {"name": "PTP", "pass": False, "detail": "servo lines stale"}
    manifest = {
        "label": label, "grid_id": GRID_ID, "index": index, "kind": "cell", "repeat": repeat,
        "variables": variables, "requested": {"width": w, "height": h},
        "negotiated": {"width": w, "height": h, "encoder_implementation": encoder, "codec": codec},
        "epoch": EPOCH + index * 400, "epoch_iso": "2026-09-26T01:00:10Z", "commit": "0123abcd", "harness_sha256": "0" * 64,
        "clock": {"a": {"host_minus_utc_s": -15.8, "measured_at": EPOCH, "servers": 3, "spread_ms": 7.9},
                  "b": {"host_minus_utc_s": -16.1, "measured_at": EPOCH, "servers": 3, "spread_ms": 6.2}},
        "ptp": {"a": {"state": "MASTER"},
                "b": {"state": "SLAVE" if ptp_locked else "LISTENING", "servo_lines_30s": 30 if ptp_locked else 0,
                      "offset_ns": 2176}},
        "band": {"a": {"band": "n41", "arfcn": 520110, "pci": 123, "share": 1.0},
                 "b": {"band": "n41", "arfcn": 521310, "pci": 77, "share": 0.98}},
        "gates": {"a": gates_a, "b": gates_b},
        "status": status, "status_reason": "" if status == "OK" else "B pcap did not close",
        "timeline": {}, "excluded_from_comparison": excluded,
        "exclusion_reason": "" if not excluded else "capture incomplete",
    }
    # the manifest epoch is what the reduced t_s are relative to
    manifest["epoch"] = EPOCH
    (cd / "manifest.json").write_text(json.dumps(manifest, indent=1))

    # ---- metrics --------------------------------------------------------------------
    def sm(v):
        return stats.summ(np.asarray(v, dtype=float).tolist())

    late100 = int(np.nansum(owd > 100))
    nrec = int(received.sum())
    ia = np.abs(np.diff(owd[np.isfinite(owd)]))
    metrics = {
        "config": {"variables": variables, "requested": manifest["requested"], "negotiated": manifest["negotiated"],
                   "epoch": EPOCH, "commit": "0123abcd", "clock": manifest["clock"], "ptp": manifest["ptp"],
                   "band": manifest["band"], "gates_passed": ptp_locked, "encoder_is_nvenc": "nvenc" in encoder.lower()},
        "frame": {"size_kb": sm(size / 1000), "packets_per_frame": sm(pk_a), "fps_delivered": rendered.sum() / duration_s,
                  "frames_captured": n + 3, "frames_encoded": n, "frames_sent": n, "frames_received": nrec,
                  "frames_rendered": int(rendered.sum()), "dropped_pre_encode": 3, "dropped_post_encode": 0,
                  "keyframes": int(key.sum()), "size_spread_pct": float(100 * size.std() / size.mean())},
        "rate": {"bytes_per_s_a": sm([r[1] for r in srows]) if reduced else sm([]),
                 "packets_per_s_a": sm([r[2] for r in srows]) if reduced else sm([]),
                 "bytes_per_s_b": sm([r[3] for r in srows]) if reduced else sm([]),
                 "packets_per_s_b": sm([r[4] for r in srows]) if reduced else sm([]),
                 "kbps_target": kbps, "kbps_achieved": float(size.sum() * 8 / 1000 / duration_s), "padding_bytes_share": 0.036},
        "encoder": {"qp": sm(qp_frame), "encode_ms": sm(enc),
                    "quality_limitation_s": {"none": duration_s, "bandwidth": 0, "cpu": 0, "other": 0}},
        "latency": {"app_to_wire_a": sm(app), "emission_a": sm(emis), "in_flight": sm(infl), "arrival_b": sm(arr),
                    "wire_to_app_b": sm(w2a), "decode": sm(dec), "render": sm(ren), "e2e": sm(e2e), "owd": sm(owd)},
        "jitter": {"owd_sd_ms": stats.sd(owd.tolist()), "interarrival_rfc3550_ms": sm(ia / 16 * 4),
                   "frame_interval_b_ms": sm(np.diff(t[received == 1]) * 1000)},
        "tail": {"owd_over_100": {"count": late100, "share": late100 / nrec},
                 "owd_over_150": {"count": int(np.nansum(owd > 150)), "share": float(np.nansum(owd > 150)) / nrec},
                 "e2e_over_100": {"count": int(np.nansum(e2e > 100)), "share": float(np.nansum(e2e > 100)) / nrec},
                 "e2e_over_150": {"count": int(np.nansum(e2e > 150)), "share": float(np.nansum(e2e > 150)) / nrec},
                 "episodes": [{"start_s": 12.0, "end_s": 12.4, "n": 12, "max_ms": 420.0, "dominant_segment": "in_flight"}]},
        "network": {"packets_lost": int((received == 0).sum() * 2), "loss_events": int((received == 0).sum()),
                    "nacks_sfu_to_a": 4, "nacks_b_to_sfu": 5, "duplicates_a": 0,
                    "in_flight_packets": sm(rng.poisson(6, 200)), "path_mtu": 1400},
        "modem": {h_: {"activity_index": 1.02, "x19ef_records": 0, "kernel_queue_max_bytes": 12000,
                       "rsrp_dbm": sm(rng.normal(-88, 2, 60)), "snr_db": sm(rng.normal(14, 2, 60))} for h_ in ("a", "b")},
        "integrity": {"captures_complete": status == "OK", "mirror_verified": True, "reduce_resyncs": 0,
                      "ptp_locked": ptp_locked, "excluded": excluded,
                      "reasons": [] if not excluded else ["B pcap did not close"]},
    }
    (cd / "metrics.json").write_text(json.dumps(metrics, indent=1, default=float))
    return cd


def make_grid(root: Path, codecs=("h264", "av1"), kbps=(512, 2500, 8000), repeats=1, extra=None, **kw) -> Path:
    gd = root / GRID_ID
    gd.mkdir(parents=True, exist_ok=True)
    axes = {"codec": list(codecs), "kbps": list(kbps)}
    extra = extra or {}
    axes.update({k: list(v) for k, v in extra.items()})
    (gd / "grid.yaml").write_text(json.dumps({"id": GRID_ID, "axes": axes}))  # JSON is valid YAML
    i = 0
    combos = [{}]
    for k, vs in extra.items():
        combos = [dict(c, **{k: v}) for c in combos for v in vs]
    for r in range(1, repeats + 1):
        for co in codecs:
            for kb in kbps:
                for ex in combos:
                    i += 1
                    make_cell(gd, i, co, kb, r, extra_vars=ex or None, **kw)
    return gd


def pdf_pages(path: Path) -> int:
    out = subprocess.run(["pdfinfo", str(path)], capture_output=True, text=True, check=True).stdout
    return int(re.search(r"^Pages:\s+(\d+)", out, re.M).group(1))


def expected_cell_pages(cd: Path) -> int:
    with (cd / "reduced" / "frames.csv").open() as f:
        n = sum(1 for r in csv.DictReader(f) if (v := r.get("owd") or r.get("owd_ms")) and float(v) > 100)
    extra = max(0, n - report_cell.SPIKE_ROWS_FIRST)
    return 8 + min(report_cell.SPIKE_MAX_CONT_PAGES, math.ceil(extra / report_cell.SPIKE_ROWS_CONT))


class ReportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="teleop-report-test-"))
        cls.grid = make_grid(cls.tmp)
        cells = sorted((cls.grid / "cells").iterdir())
        # vary the six: one INCOMPLETE (excluded), one non-NVENC, one PTP-unlocked, one without spikes.csv
        make_cell(cls.grid, 2, "h264", 2500, status="INCOMPLETE")
        make_cell(cls.grid, 4, "av1", 512, encoder="libaom (software)")
        make_cell(cls.grid, 5, "av1", 2500, alt_names=True)
        make_cell(cls.grid, 6, "av1", 8000, ptp_locked=False, spikes_csv=False)
        (cls.grid / "cells" / sorted(p.name for p in cells if "-c06-" in p.name)[0] / "reduced" / "spikes.csv").unlink(missing_ok=True)
        cls.cells = sorted((cls.grid / "cells").iterdir())

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_cell_reports(self):
        self.assertEqual(len(self.cells), 6)
        for cd in self.cells:
            pdf = report_cell.render(cd)
            self.assertTrue(pdf.is_file())
            html = (cd / "report.html").read_text()
            self.assertIn(cd.name, html)
            self.assertEqual(pdf_pages(pdf), expected_cell_pages(cd), cd.name)
            self.assertNotIn("http://", html.replace("http://www.w3.org", ""))
            self.assertNotIn("https://", html)
        # the loud flags
        nonnv = next(c for c in self.cells if "-c04-" in c.name)
        self.assertIn("not NVENC", (nonnv / "report.html").read_text())
        unlocked = next(c for c in self.cells if "-c06-" in c.name)
        self.assertIn("PTP NOT LOCKED", (unlocked / "report.html").read_text())
        locked = next(c for c in self.cells if "-c01-" in c.name)
        self.assertNotIn("PTP NOT LOCKED", (locked / "report.html").read_text())
        # the rendered HTML carries every seven-field statistic header
        self.assertIn("<th class=\"num\">p99</th>", (locked / "report.html").read_text())

    def test_cell_tolerates_missing_reduced(self):
        gd = self.tmp / "sparse"
        cd = make_cell(gd, 1, "h264", 2500, reduced=False)
        pdf = report_cell.render(cd)
        self.assertEqual(pdf_pages(pdf), 8)
        self.assertIn("frames.csv missing", (cd / "report.html").read_text())

    def test_grid_comparison(self):
        for cd in self.cells:
            if not (cd / "report.html").exists():
                report_cell.render(cd)
        out = report_grid.render(self.grid)
        for f in ("metrics.csv", "comparison.html", "comparison.pdf"):
            self.assertTrue((out / f).is_file(), f)
        self.assertEqual(pdf_pages(out / "comparison.pdf"), 1 + len(report_grid.KPIS_OF_RECORD))
        html = (out / "comparison.html").read_text()
        for cd in self.cells:
            self.assertIn(cd.name, html)
        self.assertIn("../cells/", html)
        with (out / "metrics.csv").open() as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(set(r["label"] for r in rows), {c.name for c in self.cells})
        owd = [r for r in rows if r["metric_path"] == "latency.owd"]
        self.assertEqual({r["statistic"] for r in owd}, set(stats.FIELDS))
        self.assertTrue(any(r["metric_path"] == "frame.fps_delivered" and r["statistic"] == "value" for r in rows))
        self.assertTrue(any(r["metric_path"] == "tail.owd_over_100.share" for r in rows))
        self.assertIn("codec", rows[0])
        self.assertIn("kbps", rows[0])
        # the rate model's variables are grid columns (manifest variables -> metrics.csv)
        for k in ("bpp", "resolution", "width", "height"):
            self.assertIn(k, rows[0])
        self.assertEqual({r["bpp"] for r in rows}, {"0.1"})
        self.assertEqual({r["excluded"] for r in rows if "-c02-" in r["label"]}, {"1"})

    def test_grid_one_cell_and_missing_metrics(self):
        gd = self.tmp / "one" / GRID_ID
        make_cell(gd, 1, "h264", 2500, reduced=False)
        # a cell that ran but never reduced: manifest only
        broken = gd / "cells" / f"{GRID_ID}-c02-av1-2500k-x-r1"
        broken.mkdir(parents=True)
        (broken / "manifest.json").write_text(json.dumps({"label": broken.name, "grid_id": GRID_ID, "index": 2,
                                                          "status": "INCOMPLETE", "variables": {"codec": "av1", "kbps": 2500}}))
        out = report_grid.render(gd)
        self.assertEqual(pdf_pages(out / "comparison.pdf"), 1 + len(report_grid.KPIS_OF_RECORD))
        html = (out / "comparison.html").read_text()
        self.assertIn(broken.name, html)
        self.assertIn("no metrics.json", html)

    def test_grid_48_cells(self):
        # 2 codecs x 4 kbps x 2 vbv x 3 repeats = 48; metrics only, as the comparison reads nothing else
        gd = make_grid(self.tmp / "big", kbps=(512, 1500, 2500, 8000), repeats=3, extra={"vbv_frames": (1, 5)},
                       reduced=False, duration_s=20)
        self.assertEqual(len(list((gd / "cells").iterdir())), 48)
        out = report_grid.render(gd)
        self.assertEqual(pdf_pages(out / "comparison.pdf"), 1 + len(report_grid.KPIS_OF_RECORD))

    @unittest.skipUnless((LEGACY_CELL / "metrics.json").is_file(), "legacy cell not reduced yet")
    def test_legacy_cell(self):
        pdf = report_cell.render(LEGACY_CELL)
        self.assertGreaterEqual(pdf_pages(pdf), 8)


if __name__ == "__main__":
    unittest.main()
