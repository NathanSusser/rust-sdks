"""Grid files: schema, expansion into cells, labels, geometry and the harness invocation.

Standard library plus PyYAML: imported on Host B too (the agent receives cells as dicts, but
preflight shares the geometry rule).

A label is GENERATED from the values that are applied, never typed. On 2026-09-29 a cell
labelled `vbv-512kbps` ran at 2500 k because the label and the cap were typed separately;
here both come from the same Cell.
"""
from __future__ import annotations

import datetime as _dt
import itertools
import math
import os
import random
import re
import secrets
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

TELEOP_DIR = Path(__file__).resolve().parent.parent
VARIABLES_PATH = TELEOP_DIR / "config" / "variables.yaml"

GRID_KEYS = {"id", "description", "control", "defaults", "axes", "pairs", "order", "expect", "seed"}
CONTROL_KEYS = {"every", "cell", "thresholds"}
THRESHOLD_KEYS = {"owd_p99_ms", "packets_lost"}
EXPECT_KEYS = {"band", "arfcn", "pci"}
ID_RE = re.compile(r"^[a-z0-9][a-z0-9_]{0,23}$")
GEOM_RE = re.compile(r"^(\d+)x(\d+)$")
# Variables a cell never labels or passes to the harness, but which the grid may set.
META_VARS = ("repeats", "lead_s", "cooldown_s")
# Variables that go into manifest.json "variables" (CONTRACT.md).
MANIFEST_VARS = ("codec", "kbps", "fps", "geometry", "bpp", "vbv_frames", "padding", "target_quality",
                 "intra_refresh", "pin_bitrate", "duration_s", "clip", "lead_s")
CAPTURE_TAIL_S = 30   # variables.yaml: capture span = lead_s + duration_s + 30
# Defaults for variables that variables.yaml declares WITHOUT one (added by control-plane,
# CONTRACT.md). fps 30 is the ARCHITECTURE §4 default; everything else must be given.
FALLBACK_DEFAULTS = {"fps": 30}


class GridError(ValueError):
    pass


def _yaml():
    import yaml  # noqa: PLC0415
    return yaml


def load_variables(path: str | os.PathLike = VARIABLES_PATH) -> dict:
    with open(path) as f:
        return _yaml().safe_load(f)


# ---------------------------------------------------------------- geometry
def derive_geometry(kbps: float, fps: float, bpp: float) -> tuple[int, int]:
    """publish-cell.sh's rule, exactly: hold bits-per-pixel, keep 1600:1300, floor to /16,
    cap at 1600x1300, floor at 160x128. awk int() truncates toward zero; so does int()."""
    px = kbps * 1000 / (fps * bpp)
    h = math.sqrt(px * 1300 / 1600)
    w = h * 1600 / 1300
    w = int(w / 16) * 16
    h = int(h / 16) * 16
    if w > 1600 or h > 1300:
        w, h = 1600, 1300
    if w < 160:
        w = 160
    if h < 128:
        h = 128
    return w, h


# ---------------------------------------------------------------- value coercion
def _as_bool(name, v):
    if isinstance(v, bool):
        return v
    if isinstance(v, int) and v in (0, 1):
        return bool(v)
    if isinstance(v, str) and v.strip().lower() in ("on", "true", "yes", "1"):
        return True
    if isinstance(v, str) and v.strip().lower() in ("off", "false", "no", "0"):
        return False
    raise GridError(f"{name}: expected on/off, got {v!r}")


def coerce(name: str, v, spec: dict):
    """Validate one value against its variables.yaml entry and return it normalised."""
    t = spec.get("type")
    if t == "enum":
        v = str(v)
        if v not in [str(x) for x in spec["values"]]:
            raise GridError(f"{name}: {v!r} not one of {spec['values']}")
        return v
    if t == "int":
        if isinstance(v, bool) or not isinstance(v, (int, str)):
            raise GridError(f"{name}: expected integer, got {v!r}")
        try:
            iv = int(v)
        except ValueError:
            raise GridError(f"{name}: expected integer, got {v!r}") from None
        if "values" in spec and iv not in spec["values"]:
            raise GridError(f"{name}: {iv} not one of {spec['values']}")
        if "min" in spec and iv < spec["min"]:
            raise GridError(f"{name}: {iv} below minimum {spec['min']}")
        if "max" in spec and iv > spec["max"]:
            raise GridError(f"{name}: {iv} above maximum {spec['max']}")
        return iv
    if t == "float":
        if isinstance(v, bool):
            raise GridError(f"{name}: expected number, got {v!r}")
        try:
            fv = float(v)
        except (TypeError, ValueError):
            raise GridError(f"{name}: expected number, got {v!r}") from None
        if "min" in spec and fv < spec["min"]:
            raise GridError(f"{name}: {fv} below minimum {spec['min']}")
        if "max" in spec and fv > spec["max"]:
            raise GridError(f"{name}: {fv} above maximum {spec['max']}")
        return fv
    if t == "bool":
        return _as_bool(name, v)
    if t == "int_or_off":
        if v is False or v is None or (isinstance(v, str) and v.strip().lower() == "off"):
            return "off"
        if isinstance(v, bool):
            raise GridError(f"{name}: expected off or 1..51, got {v!r}")
        try:
            iv = int(v)
        except (TypeError, ValueError):
            raise GridError(f"{name}: expected off or 1..51, got {v!r}") from None
        if not 1 <= iv <= 51:
            raise GridError(f"{name}: {iv} outside 1..51")
        return iv
    if t == "geometry":
        s = str(v).strip().lower()
        if s == "auto":
            return "auto"
        m = GEOM_RE.match(s)
        if not m or int(m.group(1)) < 16 or int(m.group(2)) < 16:
            raise GridError(f"{name}: expected 'auto' or WxH, got {v!r}")
        return f"{int(m.group(1))}x{int(m.group(2))}"
    if t == "path":
        if not isinstance(v, str) or not v.strip():
            raise GridError(f"{name}: expected a path, got {v!r}")
        return str(Path(v).expanduser())
    raise GridError(f"{name}: variables.yaml declares unknown type {t!r}")


def _spec_default(spec: dict):
    return spec.get("default")


# ---------------------------------------------------------------- Cell
@dataclass
class Cell:
    grid_id: str
    index: int
    kind: str                    # "cell" | "control"
    repeat: int
    values: dict
    index_width: int = 2
    combo: int = 0               # which combination (control = -1)

    @property
    def requested(self) -> tuple[int, int]:
        g = self.values["geometry"]
        if g == "auto":
            return derive_geometry(self.values["kbps"], self.values["fps"], self.values["bpp"])
        w, h = GEOM_RE.match(g).groups()
        return int(w), int(h)

    @property
    def label(self) -> str:
        w, h = self.requested
        prefix = "x" if self.kind == "control" else "c"
        v = self.values
        return (f"{self.grid_id}-{prefix}{self.index:0{self.index_width}d}-{v['codec']}-{v['kbps']}k-"
                f"{w}x{h}-v{v['vbv_frames']}-p{1 if v['padding'] else 0}-r{self.repeat}")

    @property
    def duration_s(self) -> int:
        return int(self.values["duration_s"])

    @property
    def span_s(self) -> int:
        """Capture window: lead + duration + tail, so captures outlast the cell."""
        return int(self.values["lead_s"]) + self.duration_s + CAPTURE_TAIL_S

    def harness_env(self) -> dict:
        """LK_* environment for the publisher. target_quality 'off' means UNSET, not 0.
        The agent strips every inherited LK_* before applying this, so 'unset' holds."""
        v = self.values
        env = {
            "LK_PIN_BITRATE_TO_MAX": "1" if v["pin_bitrate"] else "0",
            "LK_MAX_START_BITRATE_KBPS": str(v["kbps"]),
            "LK_NVENC_VBV_FRAMES": str(v["vbv_frames"]),
            "LK_NVENC_FILLER": "1" if v["padding"] else "0",
            "LK_NVENC_INTRA_REFRESH_FRAMES": str(v["intra_refresh"]),
            # WARN carries the NVENC "frame-size cap" line and the stale-room warning
            # without the volume of full info logging.
            "RUST_LOG": "warn",
        }
        if v["target_quality"] != "off":
            env["LK_NVENC_TARGET_QUALITY"] = str(v["target_quality"])
        return env

    def harness_args(self) -> list[str]:
        """teleop-harness args for this cell. No url, room or output paths (the agent adds
        those). --encoder nvenc always: never fall back to a software encoder."""
        v = self.values
        w, h = self.requested
        return [
            "--duration-s", str(v["duration_s"]), "--warmup-s", "5",
            "--codec", v["codec"], "--encoder", "nvenc",
            "--width", str(w), "--height", str(h), "--fps", str(v["fps"]),
            "--max-bitrate", str(int(v["kbps"]) * 1000),
            "--degradation", "locked",
            "--camera-source", v["clip"],
            "--attach-timestamp", "--attach-frame-id", "--buffering-mode", "zero_jitter",
            "--control-transport", "dc_reliable",
            "--publish-only", "--stats-poll-hz", "1", "--video-poll-hz", "1",
        ]

    def manifest_variables(self) -> dict:
        return {k: self.values[k] for k in MANIFEST_VARS}

    def to_dict(self) -> dict:
        w, h = self.requested
        return {
            "index": self.index, "label": self.label, "kind": self.kind, "repeat": self.repeat,
            "variables": dict(self.values), "requested": {"width": w, "height": h},
            "span_s": self.span_s,
            "harness_args": self.harness_args(), "harness_env": self.harness_env(),
        }


# ---------------------------------------------------------------- Grid
@dataclass
class Grid:
    id: str
    description: str
    defaults: dict
    combos: list                 # list of dicts (axis values only)
    order: str
    seed: int
    expect: dict
    control: dict | None
    variables: dict
    source: str = ""
    raw: dict = field(default_factory=dict)

    def _resolve(self, overrides: dict) -> dict:
        vals = dict(self.defaults)
        for k, v in overrides.items():
            vals[k] = coerce(k, v, self.variables[k])
        missing = [k for k in self.variables if k not in vals]
        if missing:
            raise GridError(f"no value for {missing} (set in defaults, axes or pairs)")
        return vals

    def expand(self) -> list[Cell]:
        rng = random.Random(self.seed)
        resolved = [self._resolve(c) for c in self.combos]
        max_rep = max(int(r["repeats"]) for r in resolved)
        rounds = []
        # Interleave across repeats: round r holds every combination that has an r-th
        # repeat, so a repeat never runs back to back with itself and drift over the
        # night does not line up with one variable.
        for rep in range(1, max_rep + 1):
            rnd = [(i, rep) for i, r in enumerate(resolved) if int(r["repeats"]) >= rep]
            if self.order == "shuffle":
                rng.shuffle(rnd)
            rounds.extend(rnd)
        plan: list[tuple[str, int, int]] = []   # (kind, combo, repeat)
        every = int(self.control["every"]) if self.control else 0
        ctl_n = 0
        if self.control:
            ctl_n += 1
            plan.append(("control", -1, ctl_n))
        for n, (ci, rep) in enumerate(rounds, 1):
            plan.append(("cell", ci, rep))
            if self.control and every and n % every == 0:
                ctl_n += 1
                plan.append(("control", -1, ctl_n))
        width = max(2, len(str(len(plan) - 1)))
        ctl_vals = self._resolve(self.control["cell"]) if self.control else None
        cells = []
        for idx, (kind, ci, rep) in enumerate(plan):
            vals = ctl_vals if kind == "control" else resolved[ci]
            cells.append(Cell(self.id, idx, kind, rep, dict(vals), width, ci))
        labels = [c.label for c in cells]
        if len(set(labels)) != len(labels):
            raise GridError("two cells expand to the same label; the grid has duplicate combinations")
        return cells

    def expanded_dict(self) -> dict:
        cells = self.expand()
        return {
            "id": self.id,
            "description": self.description,
            "source": self.source,
            "expanded_at": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "git_commit": git_commit(),
            "order": self.order,
            "seed": self.seed,
            "defaults": self.defaults,
            "expect": self.expect,
            "control": self.control,
            "definition": self.raw,
            "cells": [{k: v for k, v in c.to_dict().items() if k not in ("harness_args", "harness_env")}
                      for c in cells],
        }

    def write_expanded(self, directory: str | os.PathLike) -> Path:
        d = Path(directory)
        d.mkdir(parents=True, exist_ok=True)
        data = self.expanded_dict()
        p = d / "grid.yaml"
        tmp = p.with_suffix(".yaml.tmp")
        with open(tmp, "w") as f:
            _yaml().safe_dump(data, f, sort_keys=False, default_flow_style=None, width=120)
        os.replace(tmp, p)
        return p


def git_commit(repo: str | os.PathLike | None = None) -> str:
    try:
        r = subprocess.run(["git", "-C", str(repo or TELEOP_DIR.parent), "rev-parse", "HEAD"],
                           capture_output=True, text=True, timeout=10)
        return r.stdout.strip() if r.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def parse(raw: dict, *, variables: dict | None = None, source: str = "", seed: int | None = None) -> Grid:
    variables = variables if variables is not None else load_variables()
    if not isinstance(raw, dict):
        raise GridError("grid file: top level must be a mapping")
    unknown = set(raw) - GRID_KEYS
    if unknown:
        raise GridError(f"grid file: unknown key(s) {sorted(unknown)}; allowed {sorted(GRID_KEYS)}")
    gid = str(raw.get("id", ""))
    if not ID_RE.match(gid):
        raise GridError(f"id {gid!r}: lowercase letters, digits and _ only, 1-24 chars "
                        "(it becomes a directory, a room and a file prefix; '-' separates label fields)")

    def check_names(d: dict, where: str):
        if not isinstance(d, dict):
            raise GridError(f"{where}: must be a mapping")
        bad = [k for k in d if k not in variables]
        if bad:
            raise GridError(f"{where}: unknown variable(s) {bad}; declared in variables.yaml: {sorted(variables)}")

    raw_defaults = raw.get("defaults") or {}
    check_names(raw_defaults, "defaults")
    defaults = {}
    for name, spec in variables.items():
        if name in raw_defaults:
            defaults[name] = coerce(name, raw_defaults[name], spec)
        elif _spec_default(spec) is not None:
            defaults[name] = coerce(name, _spec_default(spec), spec)
        elif name in FALLBACK_DEFAULTS:
            defaults[name] = coerce(name, FALLBACK_DEFAULTS[name], spec)
    if "axes" in raw and "pairs" in raw:
        raise GridError("use either axes: (cross product) or pairs: (explicit list), not both")
    combos: list[dict] = []
    if "axes" in raw:
        axes = raw["axes"] or {}
        check_names(axes, "axes")
        for k, vs in axes.items():
            if not isinstance(vs, list) or not vs:
                raise GridError(f"axes.{k}: must be a non-empty list")
            for v in vs:
                coerce(k, v, variables[k])
        keys = list(axes)
        combos = [dict(zip(keys, prod)) for prod in itertools.product(*(axes[k] for k in keys))]
    elif "pairs" in raw:
        pairs = raw["pairs"]
        if not isinstance(pairs, list) or not pairs:
            raise GridError("pairs: must be a non-empty list of mappings")
        for i, p in enumerate(pairs):
            check_names(p, f"pairs[{i}]")
            for k, v in p.items():
                coerce(k, v, variables[k])
        combos = [dict(p) for p in pairs]
    else:
        combos = [{}]
    control = raw.get("control")
    if control is not None:
        if not isinstance(control, dict):
            raise GridError("control: must be a mapping")
        bad = set(control) - CONTROL_KEYS
        if bad:
            raise GridError(f"control: unknown key(s) {sorted(bad)}")
        every = control.get("every")
        if isinstance(every, bool) or not isinstance(every, int) or every < 1:
            raise GridError("control.every: must be a positive integer")
        check_names(control.get("cell") or {}, "control.cell")
        for k, v in (control.get("cell") or {}).items():
            coerce(k, v, variables[k])
        th = control.get("thresholds") or {}
        bad = set(th) - THRESHOLD_KEYS
        if bad:
            raise GridError(f"control.thresholds: unknown key(s) {sorted(bad)}; allowed {sorted(THRESHOLD_KEYS)}")
        control = {"every": every, "cell": dict(control.get("cell") or {}), "thresholds": dict(th)}
    order = raw.get("order", "shuffle")
    if order not in ("shuffle", "sequential"):
        raise GridError(f"order: must be shuffle or sequential, got {order!r}")
    expect = raw.get("expect") or {}
    if not isinstance(expect, dict) or set(expect) - EXPECT_KEYS:
        raise GridError(f"expect: allowed keys {sorted(EXPECT_KEYS)}")
    if "band" in expect and not re.match(r"^n\d+$", str(expect["band"])):
        raise GridError(f"expect.band: expected like n41, got {expect['band']!r}")
    if seed is None:
        seed = raw.get("seed")
    if seed is None:
        seed = secrets.randbits(32)
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise GridError("seed: must be an integer")
    g = Grid(id=gid, description=str(raw.get("description", "")), defaults=defaults, combos=combos,
             order=order, seed=int(seed), expect=dict(expect), control=control, variables=variables,
             source=source, raw=raw)
    g.expand()  # validates labels are unique and every value resolves
    return g


def load(path: str | os.PathLike, *, seed: int | None = None, variables_path=VARIABLES_PATH) -> Grid:
    p = Path(path)
    with open(p) as f:
        raw = _yaml().safe_load(f)
    return parse(raw, variables=load_variables(variables_path), source=str(p.resolve()), seed=seed)
