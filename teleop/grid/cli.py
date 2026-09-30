"""`teleop` -- the one path into the system (python3 -m teleop.grid.cli ...).

    grid new <file> --id ID --axis codec=h264,av1 --axis kbps=512,2500 [--set duration_s=60] ...
    grid new <file> --id ID --set resolution=1080x1900 --set fps=30 --axis bpp=0.04,0.06,0.1   (kbps derived)
    grid check <file> [--local-only]      expand, print the cell table, pre-flight both hosts; arms nothing
    grid run <file> [--resume]
    grid status <grid-id>
    grid stop <grid-id>
    grid report <grid-id>
    cell reduce|metrics|report <cell-dir>
    agent <cmd> ...                       (passes through to teleop.grid.agent)
"""
from __future__ import annotations

import argparse
import importlib
import json
import sys
from pathlib import Path


def _yaml():
    import yaml  # noqa: PLC0415
    return yaml


def _parse_scalar(s: str):
    return _yaml().safe_load(s)


# ---------------------------------------------------------------- grid new

def cmd_grid_new(a) -> int:
    from . import grid as gridmod  # noqa: PLC0415
    out = Path(a.file)
    if out.exists() and not a.force:
        print(f"{out} exists (use --force)", file=sys.stderr)
        return 1
    doc: dict = {"id": a.id, "description": a.description or ""}
    defaults = {}
    for kv in a.set or []:
        k, _, v = kv.partition("=")
        defaults[k.strip()] = _parse_scalar(v)
    if defaults:
        doc["defaults"] = defaults
    axes = {}
    for kv in a.axis or []:
        k, _, v = kv.partition("=")
        axes[k.strip()] = [_parse_scalar(x) for x in v.split(",") if x.strip()]
    if axes:
        doc["axes"] = axes
    doc["order"] = a.order
    if a.control_every:
        doc["control"] = {"every": a.control_every,
                          "cell": {"codec": "h264", "kbps": 2500, "duration_s": 120},
                          "thresholds": {"owd_p99_ms": 100, "packets_lost": 0}}
    if a.band:
        doc["expect"] = {"band": a.band}
    g = gridmod.parse(doc, source=str(out))   # validate before writing
    for w in g.warnings:
        print(f"warning: {w}", file=sys.stderr)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w") as f:
        _yaml().safe_dump(doc, f, sort_keys=False, default_flow_style=None)
    print(f"wrote {out}")
    return 0


# ---------------------------------------------------------------- grid check

def print_table(cells) -> None:
    print(f"{'idx':>4}  {'kind':<7} {'label':<62} {'WxH':>10} {'bpp':>7} {'kbps':>6} {'dur':>5} {'span':>5}")
    for c in cells:
        w, h = c.requested
        print(f"{c.index:>4}  {c.kind:<7} {c.label:<62} {f'{w}x{h}':>10} {c.values['bpp']:>7.4f} "
              f"{c.values['kbps']:>6} {c.duration_s:>5} {c.span_s:>5}")
    total = sum(c.duration_s + int(c.values["lead_s"]) + 30 + int(c.values["cooldown_s"]) for c in cells)
    print(f"{len(cells)} cells, ~{total / 3600:.1f} h wall clock (lead + duration + close + cooldown; "
          "excludes mirror and reduction)")


def print_gates(host: str, gates: list[dict]) -> None:
    for g in gates:
        mark = "PASS" if g["pass"] else "FAIL"
        print(f"  {host} {mark}  {g['name']:<18} {g['detail']}")


def cmd_grid_check(a) -> int:
    from . import grid as gridmod  # noqa: PLC0415
    from . import hostcfg, preflight  # noqa: PLC0415
    from .orchestrator import Host, both  # noqa: PLC0415
    g = gridmod.load(a.file)
    cells = g.expand()
    print(f"grid {g.id}: {g.description}")
    print(f"order {g.order}, seed {g.seed} (a run records its own seed unless the file sets one)"
          + (f", expect {g.expect}" if g.expect else ""))
    for w in g.warnings:
        print(f"warning: {w}")
    print_table(cells)
    clips = sorted({c.values["clip"] for c in cells})
    for clip in clips:
        print(f"clip {'ok     ' if Path(clip).is_file() else 'MISSING'} {clip}")
    cfg = hostcfg.load()
    local = Host(cfg, remote=False)
    first = cells[0]
    print(f"\npre-flight for {first.label} (nothing is armed):")
    args = {"cell": first.to_dict(), "expect": g.expect}
    if a.local_only:
        ra = local.call("preflight", args)
        rb = {"ok": False, "error": "not checked (--local-only: no ssh to the peer)"}
        ia, ib = local.call("identity"), None
    else:
        peer = Host(cfg, remote=True)
        ra, rb = both(lambda: local.call("preflight", args), lambda: peer.call("preflight", args))
        ia, ib = both(lambda: local.call("identity"), lambda: peer.call("identity"))
    me, other = cfg["role"].upper(), ("B" if cfg["role"] == "a" else "A")
    print_gates(me, ra.get("gates") or [preflight.result("agent", False, ra.get("error", "no reply"))])
    print_gates(other, rb.get("gates") or [preflight.result("agent", False, rb.get("error", "no reply"))])
    cm = None
    if ib is not None:
        cm = preflight.code_match(ia, ib) if ia.get("ok") and ib.get("ok") else \
            preflight.result("code_match", False, f"identity: {me} {ia.get('error', 'ok')}; {other} {ib.get('error', 'ok')}")
        print_gates("A+B", [cm])
    else:
        print(f"  {me} identity: commit {str(ia.get('commit'))[:8]} harness {str(ia.get('harness_sha256'))[:12]} "
              f"package {str(ia.get('package_sha256'))[:12]} dirty={ia.get('teleop_dirty')}")
    # The cross-host code match is a gate like any other: a FAIL there is NOT READY.
    ok = bool(ra.get("ok") and rb.get("ok") and (cm is None or cm.get("pass")))
    print("\nREADY" if ok else "\nNOT READY: fix the FAIL lines above before `grid run`")
    return 0 if ok else 1


# ---------------------------------------------------------------- grid run/status/stop/report

def cmd_grid_run(a) -> int:
    from .orchestrator import run_grid  # noqa: PLC0415
    res = run_grid(a.file, resume=a.resume)
    return 0 if res == "done" else 1


def _grid_id(arg: str) -> str:
    p = Path(arg)
    if p.suffix in (".yaml", ".yml") and p.is_file():
        return _yaml().safe_load(p.read_text())["id"]
    return arg


def cmd_grid_status(a) -> int:
    from .orchestrator import grid_status  # noqa: PLC0415
    st = grid_status(_grid_id(a.grid))
    if a.json:
        print(json.dumps(st, indent=2))
        return 0
    s = st["state"]
    print(f"grid {st['grid_id']}: {s.get('status', '?')} (updated {s.get('updated', '?')}), current "
          f"{s.get('current')}" + (" STOP requested" if st["stop_requested"] else "")
          + (f"\nPAUSED: {st['paused']}" if st["paused"] else ""))
    for r in st["cells"]:
        print(f"  {r['index']:>4} {r['label']:<62} {r['status'] or '':<10} {(r['status_reason'] or '')[:80]}")
    return 0


def cmd_grid_stop(a) -> int:
    from .orchestrator import stop_grid  # noqa: PLC0415
    p = stop_grid(_grid_id(a.grid))
    print(f"stop requested ({p}); the orchestrator stops the current cell and ends the grid")
    return 0


def cmd_grid_report(a) -> int:
    from . import hostcfg  # noqa: PLC0415
    cfg = hostcfg.load()
    d = Path(cfg["results_root"]) / _grid_id(a.grid)
    rc = 0
    if a.cells:
        for cdir in sorted((d / "cells").iterdir()):
            rc |= _cell_step("report", cdir)
    try:
        path = importlib.import_module("teleop.grid.report.grid").render(d)
        print(f"comparison: {path}")
    except Exception as e:  # noqa: BLE001
        print(f"report.grid.render failed: {type(e).__name__}: {e}", file=sys.stderr)
        rc = 1
    return rc


# ---------------------------------------------------------------- cell

CELL_STEPS = {"reduce": ("teleop.grid.reduce", "reduce_cell"), "metrics": ("teleop.grid.metrics", "build"),
              "report": ("teleop.grid.report.cell", "render")}


def _cell_step(step: str, cdir: Path) -> int:
    mod, fn = CELL_STEPS[step]
    try:
        r = getattr(importlib.import_module(mod), fn)(Path(cdir))
        print(f"{cdir.name}: {step} ok" + (f" -> {r}" if isinstance(r, (str, Path)) else ""))
        return 0
    except Exception as e:  # noqa: BLE001
        print(f"{cdir.name}: {step} failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 1


def cmd_cell(a) -> int:
    return _cell_step(a.step, Path(a.dir))


# ---------------------------------------------------------------- main

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="teleop")
    sub = ap.add_subparsers(dest="area", required=True)
    gp = sub.add_parser("grid").add_subparsers(dest="cmd", required=True)
    p = gp.add_parser("new", help="write a grid file from flags")
    p.add_argument("file")
    p.add_argument("--id", required=True)
    p.add_argument("--description")
    p.add_argument("--axis", action="append", help="name=v1,v2,...")
    p.add_argument("--set", action="append", help="default: name=value")
    p.add_argument("--order", choices=["shuffle", "sequential"], default="shuffle")
    p.add_argument("--control-every", type=int)
    p.add_argument("--band")
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_grid_new)
    p = gp.add_parser("check", help="expand, print the table, pre-flight both hosts; arms nothing")
    p.add_argument("file")
    p.add_argument("--local-only", action="store_true", help="do not contact the peer (no ssh)")
    p.set_defaults(fn=cmd_grid_check)
    p = gp.add_parser("run")
    p.add_argument("file")
    p.add_argument("--resume", action="store_true")
    p.set_defaults(fn=cmd_grid_run)
    for name, fn in (("status", cmd_grid_status), ("stop", cmd_grid_stop), ("report", cmd_grid_report)):
        p = gp.add_parser(name)
        p.add_argument("grid", help="grid id (or its grid file)")
        if name == "status":
            p.add_argument("--json", action="store_true")
        if name == "report":
            p.add_argument("--cells", action="store_true", help="also re-render every cell report")
        p.set_defaults(fn=fn)
    cp = sub.add_parser("cell")
    cp.add_argument("step", choices=sorted(CELL_STEPS))
    cp.add_argument("dir")
    cp.set_defaults(fn=cmd_cell)
    return ap


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "agent":
        from . import agent  # noqa: PLC0415
        return agent.main(argv[1:])
    a = build_parser().parse_args(argv)
    try:
        return a.fn(a)
    except Exception as e:  # noqa: BLE001
        print(f"teleop: {type(e).__name__}: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
