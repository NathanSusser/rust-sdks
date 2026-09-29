"""Grid expansion, labels, geometry, validation. Standard library unittest.

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

    def test_explicit_geometry_overrides(self):
        g = G.parse(base(axes={"codec": ["h264"], "kbps": [512], "geometry": ["640x480"]}))
        self.assertEqual(g.expand()[0].requested, (640, 480))


class Labels(unittest.TestCase):
    def test_smoke_label(self):
        g = G.load(Path(G.TELEOP_DIR) / "config" / "grids" / "smoke.yaml")
        cells = g.expand()
        self.assertEqual(len(cells), 1)
        self.assertEqual(cells[0].label, "smoke-c00-h264-2500k-1008x816-v1-p1-r1")

    def test_label_format_and_padding_flag(self):
        g = G.parse(base(axes={"codec": ["av1"], "kbps": [2500], "padding": [True, False]},
                         defaults={"clip": CLIP, "repeats": 2, "vbv_frames": 5}))
        labels = [c.label for c in g.expand()]
        self.assertIn("g0930a-c00-av1-2500k-1008x816-v5-p1-r1", labels)
        self.assertIn("g0930a-c01-av1-2500k-1008x816-v5-p0-r1", labels)
        self.assertEqual(len(labels), 4)
        self.assertEqual(sum(label.endswith("-r2") for label in labels), 2)

    def test_label_follows_applied_kbps(self):
        c = G.parse(base(axes={"codec": ["h264"], "kbps": [512]})).expand()[0]
        self.assertIn("-512k-448x368-", c.label)
        self.assertIn("--max-bitrate", c.harness_args())
        self.assertEqual(c.harness_args()[c.harness_args().index("--max-bitrate") + 1], "512000")
        self.assertEqual(c.harness_env()["LK_MAX_START_BITRATE_KBPS"], "512")

    def test_every_variable_resolved(self):
        variables = G.load_variables()
        for c in G.parse(base()).expand():
            self.assertEqual(set(c.values), set(variables))

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
        self.assertEqual(labels, ["g0930a-c00-h264-512k-448x368-v1-p1-r1", "g0930a-c01-av1-8000k-1600x1300-v1-p1-r1"])


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
        self.assertEqual(ctl[0].label, "g0930a-x00-h264-2500k-1008x816-v1-p1-r1")
        self.assertEqual(ctl[1].label, "g0930a-x04-h264-2500k-1008x816-v1-p1-r2")
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


if __name__ == "__main__":
    unittest.main()
