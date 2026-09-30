"""Grid expansion, labels, geometry, rate derivation, validation. Standard library unittest.

    python3 -m unittest discover -s teleop/grid/tests -t .
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from teleop.grid import grid as G  # noqa: E402

CLIP = "/nonexistent/clip.mp4"


def base(**kw):
    raw = {"id": "g0930a", "defaults": {"clip": CLIP, "repeats": 1, "duration_s": 60},
           "axes": {"codec": ["h264"], "kbps": [2500]}, "order": "sequential"}
    raw.update(kw)
    return raw


class Geometry(unittest.TestCase):
    TABLE = {256: (320, 256), 512: (448, 368), 1000: (640, 512), 2500: (1008, 816),
             5000: (1424, 1152), 8000: (1600, 1300)}

    def test_table(self):
        for kbps, want in self.TABLE.items():
            self.assertEqual(G.derive_geometry(kbps, 30, 0.10), want, kbps)

    def test_multiple_of_16_and_bounds(self):
        for kbps in range(128, 20001, 97):
            w, h = G.derive_geometry(kbps, 30, 0.10)
            self.assertLessEqual((w, h), (1600, 1300))
            self.assertGreaterEqual(w, 160)
            self.assertGreaterEqual(h, 128)
            if (w, h) != (1600, 1300) and w > 160 and h > 128:
                self.assertEqual(w % 16, 0)
                self.assertEqual(h % 16, 0)

    def test_floor(self):
        self.assertEqual(G.derive_geometry(10, 30, 0.5), (160, 128))

    def test_explicit_resolution_overrides(self):
        g = G.parse(base(axes={"codec": ["h264"], "kbps": [512], "resolution": ["640x480"]}))
        self.assertEqual(g.expand()[0].requested, (640, 480))

    def test_auto_table_through_grid(self):
        # resolution auto + kbps is today's rule, unchanged (bpp defaults to 0.10)
        for kbps, want in self.TABLE.items():
            c = G.parse(base(axes={"codec": ["h264"], "kbps": [kbps]})).expand()[0]
            self.assertEqual(c.requested, want, kbps)
            self.assertEqual((c.values["width"], c.values["height"]), want)
            self.assertEqual(c.values["resolution"], "auto")
            self.assertEqual(c.values["bpp"], 0.10)
            self.assertEqual(c.values["kbps"], kbps)

    def test_auto_honours_bpp(self):
        c = G.parse(base(defaults={"clip": CLIP, "repeats": 1, "bpp": 0.05})).expand()[0]
        self.assertEqual(c.requested, G.derive_geometry(2500, 30, 0.05))
        self.assertEqual(c.values["bpp"], 0.05)


def fixed(res="1080x1900", **defaults):
    d = {"clip": CLIP, "repeats": 1, "duration_s": 60, "resolution": res}
    d.update(defaults)
    return d


class Rate(unittest.TestCase):
    """The rate rules: bpp -> kbps, kbps -> bpp, auto, and the rejections."""

    def test_bpp_derives_kbps(self):
        g = G.parse(base(defaults=fixed(), axes={"codec": ["h264"], "bpp": [0.04, 0.06, 0.08, 0.10, 0.14]}))
        got = {c.values["bpp"]: c.values["kbps"] for c in g.expand()}
        self.assertEqual(got, {0.04: 2462, 0.06: 3694, 0.08: 4925, 0.10: 6156, 0.14: 8618})
        for c in g.expand():
            self.assertEqual(c.requested, (1080, 1900))
            self.assertEqual(c.harness_env()["LK_MAX_START_BITRATE_KBPS"], str(c.values["kbps"]))
            a = c.harness_args()
            self.assertEqual(a[a.index("--max-bitrate") + 1], str(c.values["kbps"] * 1000))
            self.assertEqual(a[a.index("--width") + 1], "1080")
            self.assertEqual(a[a.index("--height") + 1], "1900")

    def test_fps_enters_kbps(self):
        c = G.parse(base(defaults=fixed(fps=15, bpp=0.10), axes={"codec": ["h264"]})).expand()[0]
        self.assertEqual(c.values["kbps"], round(1080 * 1900 * 15 * 0.10 / 1000))

    def test_fps_25(self):
        # the harness asks ffmpeg for -r 25, which drops 1 frame in 6 from the 30 fps clip
        c = G.parse(base(defaults=fixed("1600x1300", fps=25), axes={"codec": ["h264"], "bpp": [0.10]})).expand()[0]
        self.assertEqual(c.values["kbps"], 5200)
        self.assertIn("-1600x1300-25fps-b0100-5200k-", c.label)
        a = c.harness_args()
        self.assertEqual(a[a.index("--fps") + 1], "25")
        G.parse(base(defaults=fixed("1600x1300", fps=20, bpp=0.1), axes={"codec": ["h264"]}))
        with self.assertRaisesRegex(G.GridError, "fps: 24 not one of"):
            G.parse(base(defaults=fixed("1600x1300", fps=24, bpp=0.1), axes={"codec": ["h264"]}))

    def test_kbps_derives_bpp(self):
        c = G.parse(base(defaults=fixed("1600x1300"), axes={"codec": ["h264"], "kbps": [2500]})).expand()[0]
        self.assertEqual(c.values["kbps"], 2500)
        self.assertIsInstance(c.values["bpp"], float)
        self.assertAlmostEqual(c.values["bpp"], 2500 * 1000 / (1600 * 1300 * 30), places=6)
        self.assertIn("-b0040-2500k-", c.label)

    def test_both_set_rejected(self):
        with self.assertRaisesRegex(G.GridError, "set bpp or kbps, not both"):
            G.parse(base(defaults=fixed(bpp=0.1), axes={"codec": ["h264"], "kbps": [2500]}))
        with self.assertRaisesRegex(G.GridError, "set bpp or kbps, not both"):
            G.parse(base(defaults=fixed(kbps=2500), axes={"codec": ["h264"], "bpp": [0.1]}))
        with self.assertRaisesRegex(G.GridError, "set bpp or kbps, not both"):
            G.parse(base(defaults=fixed(), axes={"codec": ["h264"], "kbps": [2500], "bpp": [0.1]}))

    def test_fixed_needs_one_of_them(self):
        with self.assertRaisesRegex(G.GridError, "needs bpp"):
            G.parse(base(defaults=fixed(), axes={"codec": ["h264"]}))

    def test_auto_without_kbps_rejected(self):
        with self.assertRaisesRegex(G.GridError, "resolution auto needs kbps"):
            G.parse(base(axes={"codec": ["h264"], "bpp": [0.1]}))

    def test_derived_kbps_range(self):
        with self.assertRaisesRegex(G.GridError, r"derives kbps 30780, outside 128\.\.20000"):
            G.parse(base(defaults=fixed(), axes={"codec": ["h264"], "bpp": [0.5]}))
        with self.assertRaisesRegex(G.GridError, r"derives kbps 49, outside"):
            G.parse(base(defaults=fixed("128x128"), axes={"codec": ["h264"], "bpp": [0.1]}))

    def test_resolution_limits(self):
        G.parse(base(defaults=fixed("128x1920", bpp=0.1), axes={"codec": ["h264"]}))
        for bad, why in (("126x1900", "outside"), ("1080x1922", "outside"), ("1081x1900", "odd"),
                         ("1080", "WxH"), ("wide", "WxH")):
            with self.assertRaisesRegex(G.GridError, why, msg=bad):
                G.parse(base(defaults=fixed(bad, bpp=0.1), axes={"codec": ["h264"]}))

    def test_bpp_bounds(self):
        G.parse(base(defaults=fixed(), axes={"codec": ["h264"], "bpp": [0.01]}))
        with self.assertRaisesRegex(G.GridError, "below minimum"):
            G.parse(base(defaults=fixed(), axes={"codec": ["h264"], "bpp": [0.005]}))

    def test_aspect_warning(self):
        g = G.parse(base(defaults=fixed("1080x1900", bpp=0.1), axes={"codec": ["h264"]}))
        self.assertTrue(any("aspect" in w and "1600x1300" in w and "stretched" in w for w in g.warnings), g.warnings)
        # within 2% of 1600:1300 -> no aspect warning; 1300 is not a multiple of 16 -> that warning only
        g = G.parse(base(defaults=fixed("1600x1300", bpp=0.1), axes={"codec": ["h264"]}))
        self.assertFalse(any("aspect" in w for w in g.warnings), g.warnings)
        self.assertTrue(any("multiple of 16" in w for w in g.warnings), g.warnings)
        g = G.parse(base(defaults=fixed("1280x1040", bpp=0.1), axes={"codec": ["h264"]}))
        self.assertEqual(g.warnings, [])
        self.assertEqual(G.parse(base()).warnings, [], "auto never warns")

    def test_values_always_carry_rate(self):
        for raw in (base(), base(defaults=fixed(bpp=0.08), axes={"codec": ["h264"]}),
                    base(defaults=fixed(), axes={"codec": ["h264"], "kbps": [3000]})):
            for c in G.parse(raw).expand():
                for k in ("kbps", "bpp", "width", "height", "resolution"):
                    self.assertIsNotNone(c.values[k], k)
                    self.assertIn(k, c.manifest_variables())
                self.assertIsInstance(c.values["bpp"], float)
                self.assertNotIn("geometry", c.values)
                self.assertEqual(c.to_dict()["requested"], {"width": c.values["width"], "height": c.values["height"]})

    def test_control_cell_rate_replaces_grid_rate(self):
        g = G.parse(base(defaults=fixed(bpp=0.08), axes={"codec": ["h264"], "fps": [30, 15]},
                         control={"every": 2, "cell": {"codec": "h264", "kbps": 2500, "duration_s": 120}}))
        ctl = [c for c in g.expand() if c.kind == "control"][0]
        self.assertEqual(ctl.values["kbps"], 2500)
        self.assertAlmostEqual(ctl.values["bpp"], 2500 * 1000 / (1080 * 1900 * 30), places=6)

    def test_sample_bpp_sweep(self):
        g = G.load(Path(G.TELEOP_DIR) / "config" / "grids" / "bpp-sweep.yaml")
        cells = g.expand()
        self.assertEqual(len(cells), 15)
        self.assertEqual({c.requested for c in cells}, {(1600, 1300)})
        self.assertEqual(sorted({(c.values["bpp"], c.values["kbps"]) for c in cells}),
                         [(0.04, 2080), (0.06, 3120), (0.08, 4160), (0.10, 5200), (0.14, 7280)])
        self.assertEqual(g.expect, {"band": "n41"})


class DeprecatedGeometry(unittest.TestCase):
    def test_alias_maps_to_resolution(self):
        for where in ("defaults", "axes"):
            if where == "defaults":
                raw = base(defaults={"clip": CLIP, "repeats": 1, "geometry": "640x480"})
            else:
                raw = base(axes={"codec": ["h264"], "kbps": [512], "geometry": ["640x480"]})
            g = G.parse(raw)
            c = g.expand()[0]
            self.assertEqual(c.requested, (640, 480), where)
            self.assertEqual(c.values["resolution"], "640x480")
            self.assertNotIn("geometry", c.values)
            self.assertTrue(any("'geometry' is deprecated" in w for w in g.warnings), g.warnings)

    def test_alias_auto(self):
        g = G.parse(base(defaults={"clip": CLIP, "repeats": 1, "geometry": "auto"}))
        self.assertEqual(g.expand()[0].requested, (1008, 816))
        self.assertTrue(g.warnings)

    def test_alias_in_pairs_and_control(self):
        raw = base(control={"every": 1, "cell": {"codec": "h264", "kbps": 2500, "geometry": "640x480"}})
        raw.pop("axes")
        raw["pairs"] = [{"codec": "h264", "kbps": 512, "geometry": "320x256"}]
        cells = G.parse(raw).expand()
        self.assertEqual([c.requested for c in cells], [(640, 480), (320, 256), (640, 480)])

    def test_alias_and_new_name_together_rejected(self):
        with self.assertRaisesRegex(G.GridError, "deprecated"):
            G.parse(base(defaults={"clip": CLIP, "geometry": "640x480", "resolution": "640x480"}))


class Labels(unittest.TestCase):
    def test_smoke_label(self):
        g = G.load(Path(G.TELEOP_DIR) / "config" / "grids" / "smoke.yaml")
        cells = g.expand()
        self.assertEqual(len(cells), 1)
        self.assertEqual(cells[0].label, "smoke-c00-h264-1008x816-30fps-b0100-2500k-v1-p1-r1")

    def test_label_format_and_padding_flag(self):
        g = G.parse(base(axes={"codec": ["av1"], "kbps": [2500], "padding": [True, False]},
                         defaults={"clip": CLIP, "repeats": 2, "vbv_frames": 5}))
        labels = [c.label for c in g.expand()]
        self.assertIn("g0930a-c00-av1-1008x816-30fps-b0100-2500k-v5-p1-r1", labels)
        self.assertIn("g0930a-c01-av1-1008x816-30fps-b0100-2500k-v5-p0-r1", labels)
        self.assertEqual(len(labels), 4)
        self.assertEqual(sum(label.endswith("-r2") for label in labels), 2)

    def test_label_follows_applied_kbps(self):
        c = G.parse(base(axes={"codec": ["h264"], "kbps": [512]})).expand()[0]
        self.assertIn("-448x368-30fps-b0100-512k-", c.label)
        self.assertIn("--max-bitrate", c.harness_args())
        self.assertEqual(c.harness_args()[c.harness_args().index("--max-bitrate") + 1], "512000")
        self.assertEqual(c.harness_env()["LK_MAX_START_BITRATE_KBPS"], "512")

    def test_label_fields(self):
        c = G.parse(base(defaults=fixed("1080x1900", fps=12), axes={"codec": ["av1"], "bpp": [0.123]})).expand()[0]
        kbps = round(1080 * 1900 * 12 * 0.123 / 1000)
        self.assertEqual(c.label, f"g0930a-c00-av1-1080x1900-12fps-b0123-{kbps}k-v1-p1-r1")
        self.assertEqual(G.bpp_tag(0.1), "0100")
        self.assertEqual(G.bpp_tag(0.5), "0500")
        self.assertEqual(G.bpp_tag(0.04), "0040")
        self.assertEqual(G.bpp_tag(0.0406), "0041")

    def test_every_variable_resolved(self):
        variables = G.load_variables()
        live = {k for k, s in variables.items() if not s.get("deprecated")}
        self.assertIn("geometry", variables)
        for c in G.parse(base()).expand():
            self.assertEqual(set(c.values), live | {"width", "height"})

    def test_index_width_grows(self):
        g = G.parse(base(axes={"codec": ["h264"], "kbps": list(range(200, 200 + 120 * 10, 10))}))
        cells = g.expand()
        self.assertEqual(cells[0].label.split("-")[1], "c000")
        self.assertEqual(cells[-1].label.split("-")[1], "c119")


class Env(unittest.TestCase):
    def test_target_quality_off_is_unset(self):
        c = G.parse(base()).expand()[0]
        self.assertNotIn("LK_NVENC_TARGET_QUALITY", c.harness_env())
        c = G.parse(base(defaults={"clip": CLIP, "repeats": 1, "target_quality": 30})).expand()[0]
        self.assertEqual(c.harness_env()["LK_NVENC_TARGET_QUALITY"], "30")

    def test_bools(self):
        c = G.parse(base(defaults={"clip": CLIP, "repeats": 1, "padding": "off", "pin_bitrate": False})).expand()[0]
        self.assertEqual(c.harness_env()["LK_NVENC_FILLER"], "0")
        self.assertEqual(c.harness_env()["LK_PIN_BITRATE_TO_MAX"], "0")
        self.assertIn("-p0-", c.label)

    def test_args_never_carry_url_room_or_outputs(self):
        a = G.parse(base()).expand()[0].harness_args()
        for f in ("--url", "--room-name", "--snapshots-out", "--frame-csv-out", "--api-key", "--api-secret"):
            self.assertNotIn(f, a)
        self.assertEqual(a[a.index("--encoder") + 1], "nvenc")

    def test_span(self):
        c = G.parse(base(defaults={"clip": CLIP, "repeats": 1, "duration_s": 300, "lead_s": 60})).expand()[0]
        self.assertEqual(c.span_s, 390)


class Validation(unittest.TestCase):
    def test_unknown_variable_in_axes(self):
        with self.assertRaisesRegex(G.GridError, "unknown variable"):
            G.parse(base(axes={"codec": ["h264"], "bitrate": [1]}))

    def test_unknown_variable_in_defaults(self):
        with self.assertRaisesRegex(G.GridError, "unknown variable"):
            G.parse(base(defaults={"clip": CLIP, "band": "n41"}))

    def test_unknown_top_level_key(self):
        with self.assertRaisesRegex(G.GridError, "unknown key"):
            G.parse(base(cells=[]))

    def test_bad_enum(self):
        with self.assertRaisesRegex(G.GridError, "codec"):
            G.parse(base(axes={"codec": ["vp9"], "kbps": [512]}))

    def test_range(self):
        with self.assertRaisesRegex(G.GridError, "below minimum"):
            G.parse(base(axes={"codec": ["h264"], "kbps": [64]}))
        with self.assertRaisesRegex(G.GridError, "above maximum"):
            G.parse(base(defaults={"clip": CLIP, "duration_s": 99999}))

    def test_type(self):
        with self.assertRaisesRegex(G.GridError, "integer"):
            G.parse(base(axes={"codec": ["h264"], "kbps": ["fast"]}))
        with self.assertRaisesRegex(G.GridError, "on/off"):
            G.parse(base(defaults={"clip": CLIP, "padding": "maybe"}))
        with self.assertRaisesRegex(G.GridError, "1..51"):
            G.parse(base(defaults={"clip": CLIP, "target_quality": 60}))

    def test_missing_required_value(self):
        with self.assertRaisesRegex(G.GridError, "no value"):
            G.parse(base(defaults={"repeats": 1}))
        # kbps is optional now; with resolution auto the rate rule rejects it instead
        with self.assertRaisesRegex(G.GridError, "auto needs kbps"):
            G.parse(base(axes={"codec": ["h264"]}))

    def test_bad_id(self):
        with self.assertRaisesRegex(G.GridError, "id"):
            G.parse(base(id="G-1"))

    def test_axes_and_pairs_exclusive(self):
        with self.assertRaisesRegex(G.GridError, "either"):
            G.parse(base(pairs=[{"codec": "h264", "kbps": 512}]))

    def test_pairs(self):
        raw = base()
        raw.pop("axes")
        raw["pairs"] = [{"codec": "h264", "kbps": 512}, {"codec": "av1", "kbps": 8000}]
        labels = [c.label for c in G.parse(raw).expand()]
        self.assertEqual(labels, ["g0930a-c00-h264-448x368-30fps-b0100-512k-v1-p1-r1",
                                  "g0930a-c01-av1-1600x1300-30fps-b0100-8000k-v1-p1-r1"])


class Order(unittest.TestCase):
    def grid(self, seed):
        return G.parse(base(axes={"codec": ["h264", "av1"], "kbps": [512, 1000, 2500, 5000, 8000]},
                            defaults={"clip": CLIP, "repeats": 3}, order="shuffle"), seed=seed)

    def test_seed_reproducible(self):
        a = [c.label for c in self.grid(1234).expand()]
        b = [c.label for c in self.grid(1234).expand()]
        self.assertEqual(a, b)

    def test_seed_changes_order(self):
        a = [c.label.split("-", 2)[2] for c in self.grid(1).expand()]
        b = [c.label.split("-", 2)[2] for c in self.grid(2).expand()]
        self.assertNotEqual(a, b)
        self.assertEqual(sorted(a), sorted(b))

    def test_seed_from_file(self):
        g = G.parse(base(seed=77))
        self.assertEqual(g.seed, 77)

    def test_interleaved_repeats(self):
        cells = self.grid(5).expand()
        reps = [c.repeat for c in cells]
        self.assertEqual(reps, sorted(reps), "every combination's r1 runs before any r2")
        self.assertEqual(len(cells), 30)

    def test_sequential(self):
        g = G.parse(base(axes={"codec": ["h264", "av1"], "kbps": [512, 2500]}, order="sequential"))
        combos = [(c.values["codec"], c.values["kbps"]) for c in g.expand()]
        self.assertEqual(combos, [("h264", 512), ("h264", 2500), ("av1", 512), ("av1", 2500)])


class Control(unittest.TestCase):
    def grid(self, every, n_kbps):
        return G.parse(base(axes={"codec": ["h264"], "kbps": [500 + 100 * i for i in range(n_kbps)]},
                            control={"every": every, "cell": {"codec": "h264", "kbps": 2500, "duration_s": 120},
                                     "thresholds": {"owd_p99_ms": 100, "packets_lost": 0}}))

    def test_positions(self):
        cells = self.grid(3, 7).expand()
        kinds = "".join("X" if c.kind == "control" else "c" for c in cells)
        self.assertEqual(kinds, "XcccXcccXc")
        self.assertEqual([c.index for c in cells], list(range(10)))

    def test_control_label_and_values(self):
        cells = self.grid(3, 3).expand()
        ctl = [c for c in cells if c.kind == "control"]
        self.assertEqual(len(ctl), 2)
        self.assertEqual(ctl[0].label, "g0930a-x00-h264-1008x816-30fps-b0100-2500k-v1-p1-r1")
        self.assertEqual(ctl[1].label, "g0930a-x04-h264-1008x816-30fps-b0100-2500k-v1-p1-r2")
        self.assertEqual(ctl[0].duration_s, 120)
        self.assertEqual(cells[1].label.split("-")[1], "c01")

    def test_bad_threshold(self):
        with self.assertRaisesRegex(G.GridError, "thresholds"):
            G.parse(base(control={"every": 2, "cell": {}, "thresholds": {"jitter": 1}}))

    def test_bad_every(self):
        with self.assertRaisesRegex(G.GridError, "every"):
            G.parse(base(control={"every": 0, "cell": {}}))


class WriteExpanded(unittest.TestCase):
    def test_write(self):
        import yaml
        g = G.parse(base(axes={"codec": ["h264", "av1"], "kbps": [512]}), seed=42)
        with tempfile.TemporaryDirectory() as d:
            p = g.write_expanded(d)
            data = yaml.safe_load(p.read_text())
            self.assertEqual(data["seed"], 42)
            self.assertEqual(data["order"], "sequential")
            self.assertIn("git_commit", data)
            self.assertEqual([c["label"] for c in data["cells"]], [c.label for c in g.expand()])
            self.assertEqual(data["cells"][0]["requested"], {"width": 448, "height": 368})
            self.assertFalse(os.path.exists(os.path.join(d, "grid.yaml.tmp")))


# ---------------------------------------------------------------- control plane v2
TONIGHT = Path(G.TELEOP_DIR) / "config" / "grids" / "tonight.yaml"
SMOKE2 = Path(G.TELEOP_DIR) / "config" / "grids" / "smoke2.yaml"


class TonightGrid(unittest.TestCase):
    """config/grids/tonight.yaml: codec x fps x bpp = 20 combinations x 3 repeats."""

    def setUp(self):
        self.g = G.load(TONIGHT, seed=7)
        self.cells = self.g.expand()

    def test_sixty_cells_twenty_combos(self):
        self.assertEqual(self.g.id, "tonight")
        self.assertEqual(len(self.cells), 60)
        self.assertEqual(self.g.swept, ["codec", "fps", "bpp"])
        want = [f"{c}-{f}fps-b{b}" for c in ("h265", "av1") for f in (30, 25)
                for b in ("0040", "0060", "0080", "0100", "0140")]
        self.assertEqual(self.g.combo_names(), want)
        self.assertEqual(len(set(want)), 20)
        self.assertIn("h265-30fps-b0040", want)
        by_combo = {}
        for c in self.cells:
            by_combo.setdefault(c.combo, []).append(c.repeat)
        self.assertEqual(sorted(by_combo), sorted(want))
        self.assertTrue(all(sorted(r) == [1, 2, 3] for r in by_combo.values()))

    def test_layout_paths(self):
        paths = [c.rel_path for c in self.cells]
        self.assertEqual(len(set(paths)), 60)
        for c in self.cells:
            self.assertEqual(c.rel_path, f"{c.combo}/r{c.repeat}")
            self.assertTrue(c.label.startswith(f"tonight-{c.run_token}-"))
            self.assertTrue(c.label.endswith(f"-r{c.repeat}"))
            d = c.to_dict()
            self.assertEqual((d["combo"], d["rel_path"]), (c.combo, c.rel_path))
        c = next(c for c in self.cells if c.combo == "av1-25fps-b0140" and c.repeat == 2)
        self.assertEqual(c.rel_path, "av1-25fps-b0140/r2")
        self.assertEqual(c.values["kbps"], 7280)
        self.assertRegex(c.label, r"^tonight-c\d{2}-av1-1600x1300-25fps-b0140-7280k-v1-p1-r2$")

    def test_defaults(self):
        for c in self.cells:
            v = c.values
            self.assertEqual((v["resolution"], v["duration_s"], v["lead_s"], v["cooldown_s"]), ("1600x1300", 180, 45, 30))
            self.assertEqual((v["vbv_frames"], v["padding"], v["pin_bitrate"], v["target_quality"], v["intra_refresh"]),
                             (1, True, True, "off", 0))
            self.assertEqual(v["control_transport"], "data_track_buf1")
            self.assertEqual(v["control_rx_buffer"], 64)
            self.assertEqual(v["kbps"], round(1600 * 1300 * v["fps"] * v["bpp"] / 1000))
            a = c.harness_args()
            self.assertEqual(a[a.index("--control-transport") + 1], "data_track_buf1")
            self.assertEqual(c.span_s, 255)
        self.assertEqual(self.g.order, "shuffle")
        self.assertEqual(self.g.expect, {"band": "n41"})
        self.assertTrue(self.g.compress_raw)
        self.assertEqual(self.cells[0].values["clip"],
                         "/home/nsusser/teleop-media/depal-face-lower-20260904/depal-face-lower-src-30s.mp4")

    def test_rounds_by_repeat(self):
        reps = [c.repeat for c in self.cells]
        self.assertEqual(reps, sorted(reps))

    def test_smoke2(self):
        g = G.load(SMOKE2, seed=1)
        cells = g.expand()
        self.assertEqual([c.rel_path for c in cells], ["h265/r1", "av1/r1"])
        self.assertEqual({c.values["kbps"] for c in cells}, {6240})
        self.assertEqual({(c.duration_s, c.values["lead_s"], c.values["cooldown_s"]) for c in cells}, {(30, 30, 10)})
        self.assertEqual({c.values["control_transport"] for c in cells}, {"data_track_buf1"})


class Layout(unittest.TestCase):
    def test_combo_tokens(self):
        vals = {"codec": "av1", "fps": 25, "bpp": 0.04, "kbps": 2500, "resolution": "auto", "width": 1008,
                "height": 816, "vbv_frames": 5, "padding": False, "target_quality": "off", "intra_refresh": 30,
                "control_transport": "dc_lossy", "pin_bitrate": True, "clip": "/m/depal-face.lower.mp4"}
        self.assertEqual(G.combo_name(vals, ["codec", "fps", "bpp", "kbps", "resolution", "vbv_frames", "padding",
                                             "target_quality"]),
                         "av1-25fps-b0040-2500k-1008x816-v5-p0-tqoff")
        self.assertEqual(G.combo_name(dict(vals, target_quality=30), ["target_quality"]), "tq30")
        self.assertEqual(G.combo_name(vals, ["intra_refresh", "control_transport", "pin_bitrate", "clip"]),
                         "intra_refresh30-control_transportdc_lossy-pin_bitrate1-clipdepal_face.lower")
        self.assertEqual(G.combo_name(vals, []), "all")

    def test_no_axes_is_all(self):
        raw = base(defaults={"clip": CLIP, "repeats": 2, "kbps": 2500, "codec": "h264"})
        raw.pop("axes")
        cells = G.parse(raw).expand()
        self.assertEqual([c.rel_path for c in cells], ["all/r1", "all/r2"])

    def test_pairs_swept_in_first_appearance_order(self):
        raw = base()
        raw.pop("axes")
        raw["pairs"] = [{"codec": "h264", "kbps": 512}, {"kbps": 8000, "codec": "av1", "vbv_frames": 3}]
        g = G.parse(raw)
        self.assertEqual(g.swept, ["codec", "kbps", "vbv_frames"])
        self.assertEqual([c.rel_path for c in g.expand()], ["h264-512k-v1/r1", "av1-8000k-v3/r1"])

    def test_colliding_combo_names_rejected(self):
        with self.assertRaisesRegex(G.GridError, "both render to the directory name 'h264-b0100'"):
            G.parse(base(defaults=fixed("1600x1300"), axes={"codec": ["h264"], "bpp": [0.1, 0.1001]}))

    def test_control_cells(self):
        g = G.parse(base(axes={"codec": ["h264"], "kbps": [500, 600, 700]},
                         control={"every": 2, "cell": {"codec": "h264", "kbps": 2500}}))
        cells = g.expand()
        ctl = [c for c in cells if c.kind == "control"]
        self.assertEqual([c.rel_path for c in ctl], ["controls/x00", "controls/x03"])
        for c in ctl:
            self.assertEqual(c.combo, "controls")
            self.assertEqual(c.label.split("-")[1], c.rel_path.split("/")[1])
        self.assertEqual([c.rel_path for c in cells if c.kind == "cell"], ["h264-500k/r1", "h264-600k/r1", "h264-700k/r1"])

    def test_find_cell_dirs(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            for rel in ("h265-30fps-b0040/r1", "h265-30fps-b0040/r2", "controls/x00", "cells/old-c00-x-r1",
                        "h265-30fps-b0040/r3", "comparison/r9", "postproc/done"):
                (d / rel).mkdir(parents=True)
            for rel in ("h265-30fps-b0040/r1", "h265-30fps-b0040/r2", "controls/x00", "cells/old-c00-x-r1",
                        "comparison/r9"):
                (d / rel / "manifest.json").write_text("{}")
            got = [p.relative_to(d).as_posix() for p in G.find_cell_dirs(d)]
            self.assertEqual(got, ["cells/old-c00-x-r1", "controls/x00", "h265-30fps-b0040/r1", "h265-30fps-b0040/r2"])


class Variables(unittest.TestCase):
    def test_control_transport(self):
        c = G.parse(base()).expand()[0]
        self.assertEqual(c.values["control_transport"], "data_track_buf1")
        for t in ("dc_reliable", "dc_lossy"):
            c = G.parse(base(defaults={"clip": CLIP, "repeats": 1, "control_transport": t})).expand()[0]
            a = c.harness_args()
            self.assertEqual(a[a.index("--control-transport") + 1], t)
            self.assertEqual(c.manifest_variables()["control_transport"], t)
        with self.assertRaisesRegex(G.GridError, "control_transport"):
            G.parse(base(defaults={"clip": CLIP, "control_transport": "carrier_pigeon"}))

    def test_control_transport_sweep_names_the_combo(self):
        g = G.parse(base(axes={"codec": ["h264"], "kbps": [2500], "control_transport": ["data_track_buf1", "dc_lossy"]}))
        self.assertEqual([c.rel_path for c in g.expand()],
                         ["h264-2500k-control_transportdata_track_buf1/r1", "h264-2500k-control_transportdc_lossy/r1"])

    def test_control_rx_buffer(self):
        c = G.parse(base()).expand()[0]
        self.assertEqual(c.values["control_rx_buffer"], 64)
        self.assertEqual(c.subscriber_args(), {"duration_s": 60, "control_transport": "data_track_buf1",
                                               "control_rx_buffer": 64})
        self.assertEqual(c.manifest_variables()["control_rx_buffer"], 64)
        # B's receive depth only: the publisher (--publish-only) has no receive queue to size
        self.assertFalse({"--control-buffer-frames", "--control-buffer-size"} & set(c.harness_args()))
        c = G.parse(base(defaults={"clip": CLIP, "repeats": 1, "control_rx_buffer": 1})).expand()[0]
        self.assertEqual(c.subscriber_args()["control_rx_buffer"], 1)
        for bad in (0, 257):
            with self.assertRaises(G.GridError):
                G.parse(base(defaults={"clip": CLIP, "control_rx_buffer": bad}))

    def test_screenshots_removed(self):
        self.assertFalse(hasattr(G, "sample_every"))
        with self.assertRaisesRegex(G.GridError, "'screenshots' removed .*delete it from the grid file"):
            G.parse(base(defaults={"clip": CLIP, "repeats": 1, "screenshots": 6}))
        c = G.parse(base()).expand()[0]
        self.assertNotIn("screenshots", c.values)
        self.assertNotIn("screenshots", c.manifest_variables())
        self.assertNotIn("sample_every", c.manifest_variables())
        self.assertNotIn("screenshots", G.load_variables())

    def test_compress_raw(self):
        self.assertTrue(G.parse(base()).compress_raw)
        self.assertFalse(G.parse(base(compress_raw=False)).compress_raw)
        self.assertFalse(G.parse(base(compress_raw="off")).compress_raw)
        with self.assertRaisesRegex(G.GridError, "compress_raw"):
            G.parse(base(compress_raw="sometimes"))

    def test_expanded_records_layout(self):
        import yaml
        g = G.load(TONIGHT, seed=3)
        with tempfile.TemporaryDirectory() as d:
            data = yaml.safe_load(g.write_expanded(d).read_text())
        self.assertTrue(data["compress_raw"])
        self.assertEqual(data["swept"], ["codec", "fps", "bpp"])
        self.assertEqual(list(data["axes"]), ["codec", "fps", "bpp"])
        self.assertEqual([c["rel_path"] for c in data["cells"]], [c.rel_path for c in g.expand()])
        self.assertEqual({c["combo"] for c in data["cells"]}, set(g.combo_names()))


if __name__ == "__main__":
    unittest.main()
