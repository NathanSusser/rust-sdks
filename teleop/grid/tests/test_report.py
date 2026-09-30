"""Report tests: synthetic cells and grids built in a temp dir, rendered, checked.

    python3 -m unittest teleop.grid.tests.test_report -v      (from the repo root, Host A)

Grids use layout v2 (<grid>/<combo>/r<n>/, CONTRACT.md); one test keeps the older cells/<label>/
layout readable. The analysis page (comparison/analysis.html) is checked on a 2 x 2 x 5 x 3 grid,
on one cell and on repeats without metrics; when google-chrome is installed its script is run
headless and the rendered DOM counted. No data file is committed; everything is generated here.
Nothing is written outside the temporary directory, except the optional legacy-cell check (see
the last test), which renders into that cell's own directory as the contract prescribes.
"""
from __future__ import annotations

import csv
import html as htmllib
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
from teleop.grid.report import analysis as report_analysis
from teleop.grid.report import cell as report_cell
from teleop.grid.report import grid as report_grid
from teleop.grid.tests import synth

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
              extra_vars: dict | None = None, layout: str = "v2") -> Path:
    """One realistic cell: manifest.json, metrics.json and (optionally) reduced/*.csv, at
    <grid>/<codec>-<kbps>k[-...]/r<repeat> (layout v2) or <grid>/cells/<label> (layout="cells").

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
    combo = f"{codec}-{kbps}k{vtag}"
    cd = grid_dir / "cells" / label if layout == "cells" else grid_dir / combo / f"r{repeat}"
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


def label_of(cd: Path) -> str:
    return json.loads((cd / "manifest.json").read_text())["label"]


def repeat_dirs(gd: Path) -> list[Path]:
    return sorted(p for p in gd.glob("*/r*") if p.is_dir())


def pdf_pages(path: Path) -> int:
    out = subprocess.run(["pdfinfo", str(path)], capture_output=True, text=True, check=True).stdout
    return int(re.search(r"^Pages:\s+(\d+)", out, re.M).group(1))


def expected_cell_pages(cd: Path) -> int:
    with (cd / "reduced" / "frames.csv").open() as f:
        rows = list(csv.DictReader(f))
    n = sum(1 for r in rows if (v := r.get("owd") or r.get("owd_ms")) and float(v) > 100)
    extra = max(0, n - report_cell.SPIKE_ROWS_FIRST)
    qp_page = 1 if any(r.get("qp") for r in rows) else 0     # "QP per frame" when frames carry qp
    # 8 fixed pages + "Control path (data track)" (always, "no data" when there are no logs)
    return 9 + qp_page + min(report_cell.SPIKE_MAX_CONT_PAGES, math.ceil(extra / report_cell.SPIKE_ROWS_CONT))


class ReportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="teleop-report-test-"))
        cls.grid = make_grid(cls.tmp)
        # vary the six: one INCOMPLETE (excluded), one non-NVENC, one PTP-unlocked, one without spikes.csv
        make_cell(cls.grid, 2, "h264", 2500, status="INCOMPLETE")
        make_cell(cls.grid, 4, "av1", 512, encoder="libaom (software)")
        make_cell(cls.grid, 5, "av1", 2500, alt_names=True)
        c6 = make_cell(cls.grid, 6, "av1", 8000, ptp_locked=False, spikes_csv=False)
        (c6 / "reduced" / "spikes.csv").unlink(missing_ok=True)
        cls.cells = repeat_dirs(cls.grid)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_cell_reports(self):
        self.assertEqual(len(self.cells), 6)
        for cd in self.cells:
            pdf = report_cell.render(cd)
            self.assertTrue(pdf.is_file())
            html = (cd / "report.html").read_text()
            self.assertIn(label_of(cd), html)
            self.assertEqual(pdf_pages(pdf), expected_cell_pages(cd), cd)
            self.assertNotIn("http://", html.replace("http://www.w3.org", ""))
            self.assertNotIn("https://", html)
            self.assertIn("Control path (data track)", html)
            self.assertNotIn("Screenshot", html)
        # the loud flags
        nonnv = next(c for c in self.cells if "-c04-" in label_of(c))
        self.assertIn("not NVENC", (nonnv / "report.html").read_text())
        unlocked = next(c for c in self.cells if "-c06-" in label_of(c))
        self.assertIn("PTP NOT LOCKED", (unlocked / "report.html").read_text())
        locked = next(c for c in self.cells if "-c01-" in label_of(c))
        self.assertNotIn("PTP NOT LOCKED", (locked / "report.html").read_text())
        # the rendered HTML carries every seven-field statistic header
        self.assertIn("<th class=\"num\">p99</th>", (locked / "report.html").read_text())

    def test_cell_tolerates_missing_reduced(self):
        gd = self.tmp / "sparse"
        cd = make_cell(gd, 1, "h264", 2500, reduced=False)
        pdf = report_cell.render(cd)
        self.assertEqual(pdf_pages(pdf), 9)
        html = (cd / "report.html").read_text()
        self.assertIn("frames.csv missing", html)
        self.assertIn("Control metrics incomplete", html)

    def test_grid_comparison(self):
        for cd in self.cells:
            if not (cd / "report.html").exists():
                report_cell.render(cd)
        out = report_grid.render(self.grid)
        for f in ("metrics.csv", "comparison.html", "comparison.pdf", "analysis.html"):
            self.assertTrue((out / f).is_file(), f)
        self.assertEqual(pdf_pages(out / "comparison.pdf"), 1 + len(report_grid.KPIS_OF_RECORD))
        html = (out / "comparison.html").read_text()
        for cd in self.cells:
            self.assertIn(label_of(cd), html)
            self.assertIn(f"../{cd.parent.name}/{cd.name}/report.html", html)
        # every combination got its summary (report.grid.render refreshes missing ones)
        for cd in self.cells:
            self.assertTrue((cd.parent / "summary.pdf").is_file(), cd.parent)
        with (out / "metrics.csv").open() as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(set(r["label"] for r in rows), {label_of(c) for c in self.cells})
        self.assertEqual(list(rows[0])[:8], ["grid_id", "label", "combo", "index", "repeat", "kind", "status", "excluded"])
        self.assertEqual({r["combo"] for r in rows}, {c.parent.name for c in self.cells})
        self.assertEqual({r["repeat"] for r in rows}, {"1"})
        # a metrics.json from before the control path still carries the (empty) control rows
        ctl = [r for r in rows if r["metric_path"] == "control.owd" and r["statistic"] == "p99"]
        self.assertEqual(len(ctl), len(self.cells))
        self.assertEqual({r["value"] for r in ctl}, {""})
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
        broken = gd / "av1-2500k" / "r1"
        broken.mkdir(parents=True)
        blabel = f"{GRID_ID}-c02-av1-2500k-x-r1"
        (broken / "manifest.json").write_text(json.dumps({"label": blabel, "grid_id": GRID_ID, "index": 2,
                                                          "status": "INCOMPLETE", "variables": {"codec": "av1", "kbps": 2500}}))
        # a repeat directory that was created but holds nothing yet: ignored everywhere
        (gd / "av1-2500k" / "r2" / "hosta").mkdir(parents=True)
        out = report_grid.render(gd)
        self.assertEqual(pdf_pages(out / "comparison.pdf"), 1 + len(report_grid.KPIS_OF_RECORD))
        html = (out / "comparison.html").read_text()
        self.assertIn(blabel, html)
        self.assertIn("no metrics.json", html)
        model = analysis_model(out / "analysis.html")
        self.assertEqual(sorted(len(c["repeats"]) for c in model["combos"]), [1, 1])
        rep_ = next(c for c in model["combos"] if c["name"] == "av1-2500k")["repeats"][0]
        self.assertEqual((rep_["has"], rep_["included"], rep_["status"]), (False, False, "INCOMPLETE"))
        self.assertIn("no_metrics", rep_["flags"])

    def test_grid_48_cells(self):
        # 2 codecs x 4 kbps x 2 vbv x 3 repeats = 48; metrics only, as the comparison reads nothing else
        gd = make_grid(self.tmp / "big", kbps=(512, 1500, 2500, 8000), repeats=3, extra={"vbv_frames": (1, 5)},
                       reduced=False, duration_s=20)
        self.assertEqual(len(repeat_dirs(gd)), 48)
        out = report_grid.render(gd, summaries=False)
        self.assertEqual(pdf_pages(out / "comparison.pdf"), 1 + len(report_grid.KPIS_OF_RECORD))
        model = analysis_model(out / "analysis.html")
        self.assertEqual(len(model["combos"]), 16)
        self.assertEqual(model["axes"], ["codec", "kbps", "vbv_frames"])
        self.assertEqual(model["x"], "kbps")          # no bpp axis: the first numeric one

    def test_legacy_cells_layout_still_read(self):
        gd = self.tmp / "old" / GRID_ID
        cds = [make_cell(gd, i, codec, kb, reduced=False, layout="cells")
               for i, (codec, kb) in enumerate((("h264", 512), ("av1", 2500)), 1)]
        report_cell.render(cds[0])
        out = report_grid.render(gd)
        with (out / "metrics.csv").open() as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(len({r["label"] for r in rows}), 2)
        self.assertEqual({r["combo"] for r in rows}, {""})
        self.assertIn("../cells/", (out / "comparison.html").read_text())
        self.assertEqual(len(analysis_model(out / "analysis.html")["combos"]), 2)

    @unittest.skipUnless((LEGACY_CELL / "metrics.json").is_file(), "legacy cell not reduced yet")
    def test_legacy_cell(self):  # noqa: D102
        pdf = report_cell.render(LEGACY_CELL)
        self.assertGreaterEqual(pdf_pages(pdf), 8)



# ======================================================================================
# the analysis page
# ======================================================================================

def analysis_model(path: Path) -> dict:
    """The JSON the page embeds (the same model its script renders from)."""
    text = Path(path).read_text(encoding="utf-8")
    m = re.search(r'<script type="application/json" id="analysis-data">(.*?)</script>', text, re.S)
    return json.loads(m.group(1).replace("<\\/", "</"))


def hrefs(text: str) -> list[str]:
    return [htmllib.unescape(h) for h in re.findall(r'href="([^"]+)"', text)]


CHROME = shutil.which("google-chrome") or shutil.which("chromium") or shutil.which("chromium-browser")


def chrome_dom(page: Path) -> str | None:
    """The DOM after the page's script ran (headless), or None when the browser fails to start."""
    try:
        r = subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--no-sandbox", "--no-first-run",
                            "--virtual-time-budget=4000", "--dump-dom", page.resolve().as_uri()],
                           capture_output=True, text=True, timeout=90)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout if r.returncode == 0 and "<html" in r.stdout else None


class AnalysisTests(unittest.TestCase):
    """2 codecs x 2 fps x 5 bpp x 3 repeats in layout v2, the awkward repeats synth.make_grid adds
    (INCOMPLETE, not NVENC, PTP unlocked, codec fallback, SKIPPED, not run, no control logs) and
    one control cell; combination summaries and a few cell reports rendered for the links."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="teleop-analysis-test-"))
        cls.grid = synth.make_grid(cls.tmp, duration_s=20)
        cls.reports = [cls.grid / "h265-30fps-b0040" / "r1", cls.grid / "av1-25fps-b0060" / "r3"]
        for cd in cls.reports:
            report_cell.render(cd)
        cls.out = report_grid.render(cls.grid)          # renders every combination's summary too
        cls.page = (cls.out / "analysis.html").read_text(encoding="utf-8")
        cls.model = analysis_model(cls.out / "analysis.html")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_structure(self):
        m = self.model
        self.assertEqual(m["axes"], ["codec", "fps", "bpp"])
        self.assertEqual(m["x"], "bpp")
        self.assertEqual(m["series_axes"], ["codec", "fps"])
        self.assertEqual(m["values"]["bpp"], [0.04, 0.06, 0.08, 0.1, 0.14])
        self.assertEqual(len(m["combos"]), 20)
        self.assertEqual(sum(len(c["repeats"]) for c in m["combos"]), 59)       # one not run yet
        for sec in ("overview", "curves", "radar", "matrix", "repeats"):
            self.assertIn(f'id="{sec}"', self.page)
        self.assertEqual(self.page.count('<details class="how">'), 5)          # one "how to read this" each
        # self-contained: no external fetch of any kind
        self.assertNotRegex(self.page.replace("http://www.w3.org/2000/svg", ""), r"https?://")
        self.assertNotIn("<link", self.page)
        self.assertNotRegex(self.page, r"<script[^>]+src=")
        # both themes are styled
        self.assertIn('[data-theme="dark"]', self.page)
        self.assertIn("prefers-color-scheme: dark", self.page)
        # the KPIs of record, primary first
        prim = [k["id"] for k in m["kpis"] if k["primary"]]
        self.assertEqual(prim, ["e2e", "owd", "jit_sd", "jit_ia", "qp", "spread", "fps", "lost", "c_owd", "c_del"])
        self.assertEqual([r["label"] for r in m["radar"]],
                         ["e2e p50", "e2e p99", "network p99", "jitter sd", "QP p50", "QP p99", "control network p99"])
        self.assertEqual(m["qp_scale"]["av1"], "AV1 q-index 0–255")
        self.assertEqual(set(m["qp_path"].values()), {"encoder.qp_per_frame"})

    def test_flags_and_counting(self):
        reps = {(c["name"], r["n"]): r for c in self.model["combos"] for r in c["repeats"]}
        self.assertEqual(reps[("h265-30fps-b0100", 2)]["status"], "INCOMPLETE")
        self.assertFalse(reps[("h265-30fps-b0100", 2)]["included"])
        self.assertIn("incomplete", reps[("h265-30fps-b0100", 2)]["flags"])
        self.assertIn("not_nvenc", reps[("av1-25fps-b0060", 3)]["flags"])
        self.assertIn("ptp_unlocked", reps[("av1-30fps-b0140", 1)]["flags"])
        self.assertIn("codec_fallback", reps[("h265-25fps-b0040", 1)]["flags"])
        self.assertIn("no_control", reps[("h265-30fps-b0060", 3)]["flags"])
        sk = reps[("av1-25fps-b0100", 2)]
        self.assertEqual((sk["status"], sk["has"], sk["included"]), ("SKIPPED", False, False))
        self.assertNotIn(("h265-25fps-b0140", 3), reps)
        cb = next(c for c in self.model["combos"] if c["name"] == "h265-25fps-b0140")
        self.assertEqual(cb["missing"], [3])
        ov = self.model["overview"]
        self.assertEqual(ov["status"], {"OK": 57, "INCOMPLETE": 1, "SKIPPED": 1})
        self.assertEqual(ov["ptp_locked"], 57)                  # 58 with metrics, one unlocked
        self.assertIn("libaom (software)", ov["encoders"]["av1"])
        self.assertEqual(len(self.model["controls"]), 1)

    def test_medians_match_python(self):
        """The overview's computed medians use stats.percentile over the counted repeats."""
        m = self.model
        k = next(x for x in m["kpis"] if x["id"] == "e2e")
        cb = next(c for c in m["combos"] if c["name"] == "h265-30fps-b0100")      # r2 INCOMPLETE
        vals = [r["mx"]["latency.e2e"]["p99"] for r in cb["repeats"] if r["included"]]
        self.assertEqual(len(vals), 2)
        self.assertEqual(report_analysis.combo_value(m, cb, k, "p99"), stats.summ(vals)["p50"])
        allv = [r["mx"]["latency.e2e"]["p99"] for r in cb["repeats"] if r["has"]]
        self.assertEqual(report_analysis.combo_value(m, cb, k, "p99", include_all=True), stats.summ(allv)["p50"])

    def test_computed_summaries(self):
        lines = {ln["id"]: ln for ln in self.model["overview"]["kpi_lines"]}
        self.assertEqual(set(lines), {"e2e", "owd", "jit_sd", "jit_ia", "qp", "spread", "fps", "lost", "c_owd", "c_del"})
        e2e = " ".join(lines["e2e"]["text"])
        self.assertRegex(e2e, r"best .+ = [\d.]+ ms; worst .+ = [\d.]+ ms \(\+[\d.]+ ms, [\d.]+×\)")
        self.assertRegex(e2e, r"bpp 0\.04 → 0\.14: ")
        self.assertRegex(e2e, r"(h265 vs av1|av1 vs h265): median [+−][\d.]+ ms over 10 matched settings")
        qp = lines["qp"]["text"]
        self.assertTrue(any(t.startswith("av1 (AV1 q-index 0–255): best") for t in qp))
        self.assertTrue(any(t.startswith("h265 (H.265 QP 0–51): best") for t in qp))
        self.assertFalse(any(" vs " in t and "h265" in t.split(":")[1] and "av1" in t.split(":")[1] for t in qp))
        self.assertIn("computed from metrics.json", self.page)

    def test_links_resolve(self):
        base = self.out
        for cb in self.model["combos"]:
            self.assertEqual(cb["summary_pdf"], f"../{cb['name']}/summary.pdf")
            self.assertTrue((base / cb["summary_pdf"]).is_file(), cb["summary_pdf"])
            for r in cb["repeats"]:
                if r["report_pdf"]:
                    self.assertTrue((base / r["report_pdf"]).is_file())
        with_reports = {r["report_pdf"] for c in self.model["combos"] for r in c["repeats"] if r["report_pdf"]}
        self.assertEqual(with_reports, {f"../{cd.parent.name}/{cd.name}/report.pdf" for cd in self.reports})
        # the summaries link back to their repeats' reports and to the analysis page
        s = (self.grid / "h265-30fps-b0040" / "summary.html").read_text()
        self.assertIn('href="r1/report.pdf"', s)
        self.assertIn('href="../comparison/analysis.html"', s)
        for h in hrefs(self.page):
            if not h.startswith("#"):
                self.assertTrue((base / h).exists(), h)

    def page_links(self, page: Path) -> list[str]:
        """Every link a page can open: its static hrefs and the report/summary links its script builds."""
        text = page.read_text(encoding="utf-8")
        m = re.search(r'<script type="application/json" id="analysis-data">(.*?)</script>', text, re.S)
        model = json.loads(m.group(1).replace("<\\/", "</"))
        links = [h for h in hrefs(text) if not h.startswith("#")]
        for cb in model["combos"]:
            links += [cb[k] for k in ("summary_pdf", "summary_html") if cb[k]]
            links += [r[k] for r in cb["repeats"] for k in ("report_pdf", "report_html") if r[k]]
        links += [r[k] for r in model["controls"] for k in ("report_pdf", "report_html") if r[k]]
        return links

    def test_root_index_is_portable(self):
        """index.html at the grid root opens every summary and report by a path relative to the root."""
        page = self.grid / "index.html"
        self.assertTrue(page.is_file())
        m = analysis_model(page)
        for cb in m["combos"]:
            self.assertEqual(cb["summary_pdf"], f"{cb['name']}/summary.pdf")
        links = self.page_links(page)
        self.assertIn("comparison/comparison.pdf", links)
        self.assertIn("comparison/metrics.csv", links)
        for h in links:
            self.assertFalse(h.startswith(("/", "file:", "http")), h)
            self.assertNotIn("..", h)
            self.assertTrue((self.grid / h).exists(), h)
        # comparison/analysis.html keeps its own relative links
        self.assertIn("comparison.pdf", self.page_links(self.out / "analysis.html"))

    def test_moved_and_renamed_folder(self):
        """Copy the grid under another name: re-rendered pages carry the new name, keep the grid id,
        and every link still resolves inside the moved folder."""
        moved = self.tmp / "elsewhere" / "09302026_Test_Sweep_BPP"
        shutil.copytree(self.grid, moved)
        report_grid.render(moved, summaries=False)
        m = analysis_model(moved / "index.html")
        self.assertEqual(m["name"], "09302026_Test_Sweep_BPP")
        self.assertEqual(m["grid_id"], self.model["grid_id"])
        text = (moved / "index.html").read_text(encoding="utf-8")
        self.assertIn("<title>09302026_Test_Sweep_BPP · analysis</title>", text)
        self.assertIn(f"grid id {self.model['grid_id']}", text)
        for page in (moved / "index.html", moved / "comparison" / "analysis.html"):
            for h in self.page_links(page):
                self.assertTrue((page.parent / h).exists(), f"{page.name}: {h}")

    def test_metrics_csv_v2(self):
        with (self.out / "metrics.csv").open() as f:
            rows = list(csv.DictReader(f))
        combos = {r["combo"] for r in rows}
        self.assertEqual(len(combos - {"controls"}), 20)
        self.assertIn("controls", combos)
        self.assertEqual({r["repeat"] for r in rows if r["combo"] != "controls"}, {"1", "2", "3"})
        d = [r for r in rows if r["metric_path"] == "control.delivered_pct" and r["combo"] == "h265-30fps-b0040"]
        self.assertEqual(len(d), 3)
        self.assertTrue(all(98 < float(r["value"]) <= 100 for r in d))
        g = {r["value"] for r in rows if r["metric_path"] == "control.gaps.max_consecutive_lost"
             and r["combo"] == "h265-30fps-b0060" and r["repeat"] == "3"}
        self.assertEqual(g, {""})                                # older subscriber: no control log

    def test_one_cell(self):
        gd = synth.make_grid(self.tmp / "one", codecs=("av1",), fps=(30,), bpp=(0.08,), repeats=1, flags=False,
                             controls=False, duration_s=12)
        out = report_grid.render(gd)
        m = analysis_model(out / "analysis.html")
        self.assertEqual(len(m["combos"]), 1)
        self.assertEqual(m["x"], "bpp")
        lines = {ln["id"]: ln for ln in m["overview"]["kpi_lines"]}
        self.assertIn("one setting only", lines["e2e"]["text"][0])
        self.assertTrue((gd / "av1-30fps-b0080" / "summary.pdf").is_file())

    def test_no_metrics_anywhere(self):
        gd = self.tmp / "empty" / "gnone"
        for combo in ("h265-30fps-b0040", "av1-30fps-b0040"):
            d = gd / combo / "r1"
            d.mkdir(parents=True)
            (d / "manifest.json").write_text(json.dumps({"label": f"gnone-c00-{combo}-r1", "grid_id": "gnone",
                                                         "status": "SKIPPED", "variables": {
                                                             "codec": combo[:4].strip("-"), "fps": 30, "bpp": 0.04}}))
        out = report_grid.render(gd)
        m = analysis_model(out / "analysis.html")
        self.assertEqual(len(m["combos"]), 2)
        self.assertTrue(all(ln["empty"] for ln in m["overview"]["kpi_lines"]))
        self.assertTrue((gd / "h265-30fps-b0040" / "summary.pdf").is_file())

    @unittest.skipUnless(CHROME, "no headless Chrome on this host")
    def test_script_renders_in_chrome(self):
        dom = chrome_dom(self.out / "analysis.html")
        if dom is None:
            self.skipTest("headless Chrome did not start")
        n_kpi = len(self.model["kpis"])
        # one svg per chart: each KPI once, QP-like KPIs once per codec, plus the explorer
        n_codec = sum(2 for k in self.model["kpis"] if k["per_codec"]) + sum(1 for k in self.model["kpis"] if not k["per_codec"])
        self.assertGreaterEqual(dom.count('class="ch"'), n_codec)
        self.assertLessEqual(dom.count('class="ch"'), n_codec + 2)
        self.assertEqual(dom.count('<table class="mx">'), 1)
        self.assertEqual(dom.count('<table class="fx">'), 1)                        # the effects table
        self.assertGreaterEqual(dom.count('<table class="rank">'), 1)               # the ranking
        self.assertIn('class="rbar"', dom)
        self.assertEqual(dom.count('class="ring"'), 4)                              # the radar's four rings
        self.assertEqual(dom.count('class="poly"'), 5)          # default: compare bpp, codec and fps held
        self.assertIn('aria-label="variable to compare"', dom)
        self.assertIn('aria-label="hold codec at"', dom)
        self.assertIn("gsynth-c", dom)                                              # the repeats table
        self.assertGreater(n_kpi, 10)


if __name__ == "__main__":
    unittest.main()
