"""Runs a grid from Host A: every cell on both hosts in lockstep (ARCHITECTURE §5).

    run_grid(path, resume=False)     stop_grid(grid_id)

Each step has a timeout and writes manifest.json when it finishes; grid.log gets a
timestamped line per step. Host B is driven over SSH (BatchMode, one agent command per call);
Host A's agent is run as a local subprocess the same way, so a hung step on either host is a
timeout here, never a hang.

Statuses: OK | SKIPPED (a gate failed; nothing published) | INCOMPLETE (ran, but a capture,
liveness, close or mirror step failed -- kept, excluded from comparison) | ABORTED (operator
stop). Two consecutive SKIPPED on the same gate PAUSE the grid (it exits, resumable with
--resume), since the cause is environmental. A control cell that fails its thresholds (or
does not complete) stops the grid.

The orchestrator never reads credentials and never passes one to either host.
"""
from __future__ import annotations

import base64
import concurrent.futures as cf
import datetime as _dt
import importlib
import json
import shlex
import subprocess
import sys
import time
import traceback
from pathlib import Path

from . import grid as gridmod
from . import hostcfg, preflight
from .capture import read_json, write_json_atomic

LIVENESS_S = 25          # publish-cell.sh: first snapshot within 25 s of the epoch, or the cell is dead
POLL_S = 10
MIN_REMAIN_S = 15        # paired-cell.sh: do not walk into at-epoch's refusal
TIMEOUTS = {"identity": 90, "preflight": 60, "arm": 120, "publish": 45, "status": 45, "checksums": 1200,
            "pull": 1800, "stop": 180}


def utc_iso(t: float | None = None, ms: bool = False) -> str:
    d = _dt.datetime.fromtimestamp(time.time() if t is None else t, _dt.timezone.utc)
    return d.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z" if ms else d.strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- transport

class Host:
    """One host's agent. Local = subprocess on A; remote = ssh to the peer."""

    def __init__(self, cfg: dict, remote: bool):
        self.cfg = cfg
        self.remote = remote
        self.name = ("b" if cfg["role"] == "a" else "a") if remote else cfg["role"]

    def argv(self, cmd: str, args: dict, cell_rel: str | None, label: str | None) -> list[str]:
        b64 = base64.b64encode(json.dumps(args, default=str).encode()).decode()
        tail = [cmd] + (["--cell-dir", cell_rel] if cell_rel else []) + (["--label", label] if label else []) \
            + ["--json", b64]
        if not self.remote:
            return [sys.executable, "-m", "teleop.grid.agent", *tail]
        remote_cmd = f"cd {shlex.quote(self.cfg['peer_repo'])} && python3 -m teleop.grid.agent " + \
            " ".join(shlex.quote(t) for t in tail)
        return ["ssh", "-n", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", self.cfg["peer"], remote_cmd]

    def call(self, cmd: str, args: dict | None = None, *, cell_rel: str | None = None, label: str | None = None,
             timeout: float | None = None) -> dict:
        timeout = timeout or TIMEOUTS.get(cmd, 60)
        argv = self.argv(cmd, args or {}, cell_rel, label)
        t0 = time.time()
        try:
            r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                               cwd=None if self.remote else self.cfg["repo"])
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": f"{self.name}:{cmd} timed out after {timeout:.0f} s", "timeout": True}
        except OSError as e:
            return {"ok": False, "error": f"{self.name}:{cmd} could not run: {e}"}
        return parse_reply(r.stdout, r.stderr, r.returncode, f"{self.name}:{cmd}") | {"elapsed_s": round(time.time() - t0, 1)}


def parse_reply(stdout: str, stderr: str, rc: int, what: str) -> dict:
    """The agent prints exactly one JSON object; take the last line that parses (an ssh banner
    may precede it). No JSON = failure with stderr's tail as the reason."""
    for line in reversed(stdout.strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except ValueError:
                continue
    return {"ok": False, "error": f"{what}: no JSON reply (rc {rc}): {stderr.strip()[-300:]}"}


def both(fa, fb):
    with cf.ThreadPoolExecutor(2) as ex:
        a, b = ex.submit(fa), ex.submit(fb)
        return a.result(), b.result()


# ---------------------------------------------------------------- the grid run

class GridRun:
    def __init__(self, path, *, resume: bool = False, out=print):
        self.cfg = hostcfg.load()
        if self.cfg["role"] != "a":
            raise RuntimeError("the orchestrator runs on Host A (role: a)")
        self.path = Path(path)
        self.out = out
        first = gridmod.load(self.path)
        self.run_dir = Path(self.cfg["results_root"]) / first.id
        seed = None
        if self.run_dir.exists():
            if not resume:
                raise RuntimeError(f"{self.run_dir} exists; use --resume or a new grid id")
            prev = self._read_yaml(self.run_dir / "grid.yaml")
            seed = (prev or {}).get("seed")
        self.grid = gridmod.load(self.path, seed=seed) if seed is not None else first
        self.cells = self.grid.expand()
        if resume and seed is not None:
            recorded = [c["label"] for c in (prev or {}).get("cells", [])]
            if recorded and recorded != [c.label for c in self.cells]:
                raise RuntimeError("the grid file changed since this grid started; resume refuses to mix "
                                   "definitions -- use a new grid id")
        self.resume = resume
        self.a = Host(self.cfg, remote=False)
        self.b = Host(self.cfg, remote=True)
        self.ident: dict = {}
        self.prev_clock = {"a": None, "b": None}
        self.gates_cfg = preflight.load_gates()

    @staticmethod
    def _read_yaml(p: Path):
        import yaml  # noqa: PLC0415
        try:
            with open(p) as f:
                return yaml.safe_load(f)
        except OSError:
            return None

    # -- files
    def log(self, msg: str):
        line = f"{utc_iso()} {msg}"
        self.out(line)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        with open(self.run_dir / "grid.log", "a") as f:
            f.write(line + "\n")

    def state(self, **kw):
        p = self.run_dir / "state.json"
        st = read_json(p, {}) or {}
        st.update(kw, updated=utc_iso())
        write_json_atomic(p, st)

    @property
    def stop_file(self) -> Path:
        return self.run_dir / "STOP"

    def stop_requested(self) -> bool:
        return self.stop_file.exists()

    # -- step 0
    def match(self) -> dict:
        ia, ib = both(lambda: self.a.call("identity"), lambda: self.b.call("identity"))
        self.ident = {"a": ia, "b": ib}
        if not ia.get("ok") or not ib.get("ok"):
            return preflight.result("code_match", False,
                                    f"identity failed: A {ia.get('error', 'ok')}; B {ib.get('error', 'ok')}")
        return preflight.code_match(ia, ib, self.gates_cfg)

    # -- whole grid
    def run(self) -> str:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        if self.resume and self.stop_file.exists():
            self.stop_file.unlink()
        (self.run_dir / "PAUSED").unlink(missing_ok=True)
        self.grid.write_expanded(self.run_dir)
        self.log(f"grid {self.grid.id}: {len(self.cells)} cells, order {self.grid.order}, seed {self.grid.seed}"
                 + (" (resume)" if self.resume else ""))
        self.state(status="running", grid_id=self.grid.id, cells=len(self.cells), started=utc_iso())
        cm = self.match()
        self.log(f"match: {'PASS' if cm['pass'] else 'FAIL'} {cm['detail']}")
        if not cm["pass"]:
            self.state(status="refused", reason=cm["detail"])
            self.log("REFUSING the grid: the hosts do not run the same code")
            return "refused"
        self.code_match = cm
        prev_skip: set[str] = set()
        for cell in self.cells:
            cdir = self.run_dir / "cells" / cell.label
            man = read_json(cdir / "manifest.json", {}) or {}
            if man.get("status") in ("OK", "SKIPPED", "INCOMPLETE", "ABORTED"):
                self.log(f"{cell.label}: already {man['status']}, skipping on resume")
                continue
            if cdir.exists() and man:
                man.update(status="INCOMPLETE", status_reason="orchestrator stopped mid-cell (found on resume)",
                           excluded_from_comparison=True, exclusion_reason="interrupted")
                write_json_atomic(cdir / "manifest.json", man)
                self.log(f"{cell.label}: found interrupted; marked INCOMPLETE")
                continue
            if self.stop_requested():
                self.log("STOP requested; ending the grid before the next cell")
                self.state(status="stopped")
                return "stopped"
            self.state(current=cell.label)
            status, failed_gates, man = CellRun(self, cell).run()
            self.post_process(cdir, status)
            if status == "ABORTED":
                self.state(status="stopped")
                return "stopped"
            if status == "SKIPPED":
                both_same = prev_skip & failed_gates
                if both_same:
                    why = f"two consecutive cells SKIPPED on {sorted(both_same)}: the cause is environmental"
                    self.log(f"PAUSED: {why}. Fix it, then: teleop grid run {self.path} --resume")
                    (self.run_dir / "PAUSED").write_text(why + "\n")
                    self.state(status="paused", reason=why)
                    return "paused"
                prev_skip = failed_gates
            else:
                prev_skip = set()
            if cell.kind == "control":
                verdict = self.control_verdict(cdir, status)
                self.log(f"control {cell.label}: {verdict}")
                if verdict.startswith("FAIL"):
                    self.state(status="aborted", reason=f"control {cell.label} {verdict}")
                    self.log("ABORTING the grid: the control cell failed its thresholds")
                    return "aborted"
            if cell is not self.cells[-1] and status != "SKIPPED":
                self.cooldown(int(cell.values["cooldown_s"]))
        self.state(status="done", current=None)
        self.log("grid done")
        return "done"

    def cooldown(self, s: int):
        end = time.time() + s
        while time.time() < end and not self.stop_requested():
            time.sleep(min(2, max(0, end - time.time())))

    def control_verdict(self, cdir: Path, status: str) -> str:
        th = (self.grid.control or {}).get("thresholds") or {}
        if status == "INCOMPLETE":
            return "FAIL: control cell INCOMPLETE"
        if status != "OK":
            return f"not evaluated (status {status})"
        m = read_json(cdir / "metrics.json")
        if not m:
            return "WARN: thresholds not evaluable (no metrics.json)"
        fails, notes = [], []
        if "owd_p99_ms" in th:
            v = ((m.get("latency") or {}).get("owd") or {}).get("p99")
            if v is None:
                notes.append("owd p99 unknown")
            elif v >= float(th["owd_p99_ms"]):
                fails.append(f"owd p99 {v:.1f} ms >= {th['owd_p99_ms']}")
        if "packets_lost" in th:
            v = (m.get("network") or {}).get("packets_lost")
            if v is None:
                notes.append("packets_lost unknown")
            elif v > int(th["packets_lost"]):
                fails.append(f"packets_lost {v} > {th['packets_lost']}")
        if fails:
            return "FAIL: " + "; ".join(fails)
        return "PASS" + (f" ({'; '.join(notes)})" if notes else "")

    # -- steps 8, 9: other agents' modules, imported lazily, never fatal
    def post_process(self, cdir: Path, status: str):
        steps = []
        if status in ("OK", "INCOMPLETE"):
            steps += [("reduced", "teleop.grid.reduce", "reduce_cell", cdir),
                      ("metrics", "teleop.grid.metrics", "build", cdir),
                      ("reported", "teleop.grid.report.cell", "render", cdir)]
        steps.append(("comparison", "teleop.grid.report.grid", "render", self.run_dir))
        for key, mod, fn, arg in steps:
            t0 = time.time()
            try:
                getattr(importlib.import_module(mod), fn)(arg)
                self.log(f"{cdir.name}: {mod}.{fn} ok ({time.time() - t0:.0f} s)")
                ok = True
            except Exception as e:  # noqa: BLE001 -- modules built in parallel may be missing or failing
                self.log(f"{cdir.name}: {mod}.{fn} FAILED: {type(e).__name__}: {e}")
                if cdir.exists():
                    with open(cdir / "postprocess-errors.log", "a") as f:
                        f.write(f"{utc_iso()} {mod}.{fn}\n{traceback.format_exc()}\n")
                ok = False
            if key in ("reduced", "reported") and (cdir / "manifest.json").exists():
                mp = cdir / "manifest.json"
                man = read_json(mp, {}) or {}
                man.setdefault("timeline", {})[key] = utc_iso() if ok else None
                write_json_atomic(mp, man)


# ---------------------------------------------------------------- one cell

class CellRun:
    def __init__(self, g: GridRun, cell: gridmod.Cell):
        self.g = g
        self.cell = cell
        self.label = cell.label
        self.rel = f"{g.grid.id}/cells/{cell.label}"
        self.dir = g.run_dir / "cells" / cell.label
        self.problems: list[str] = []
        w, h = cell.requested
        self.man = {
            "label": cell.label, "grid_id": g.grid.id, "index": cell.index, "kind": cell.kind,
            "repeat": cell.repeat, "variables": cell.manifest_variables(),
            "requested": {"width": w, "height": h},
            "negotiated": {"width": None, "height": None, "encoder_implementation": None, "codec": None},
            "epoch": None, "epoch_iso": None,
            "commit": g.ident.get("a", {}).get("commit"), "harness_sha256": g.ident.get("a", {}).get("harness_sha256"),
            "clock": {"a": None, "b": None}, "ptp": {"a": None, "b": None},
            "band": {"a": None, "b": None},
            "gates": {"a": [], "b": []},
            "status": "RUNNING", "status_reason": "",
            "timeline": {k: None for k in ("preflight", "armed_b", "armed_a", "published", "closed", "mirrored",
                                           "reduced", "reported")},
            "steps": {},
            "excluded_from_comparison": True, "exclusion_reason": "running",
        }

    def save(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        write_json_atomic(self.dir / "manifest.json", self.man)

    def step(self, name: str, ok: bool, detail="", **extra):
        self.man["steps"][name] = {"ok": ok, "at": utc_iso(), "detail": detail, **extra}
        self.g.log(f"{self.label}: {name} {'ok' if ok else 'FAILED'}{(' -- ' + str(detail)) if detail else ''}")
        self.save()

    def call(self, host: Host, cmd: str, args=None, timeout=None) -> dict:
        return host.call(cmd, args or {}, cell_rel=self.rel, label=self.label, timeout=timeout)

    def finish(self, status: str, reason: str = "") -> tuple[str, set, dict]:
        self.man["status"] = status
        self.man["status_reason"] = reason
        self.man["excluded_from_comparison"] = status != "OK"
        self.man["exclusion_reason"] = "" if status == "OK" else (reason or status)
        self.save()
        self.g.log(f"{self.label}: {status}{(' -- ' + reason) if reason else ''}")
        failed = {f"{h}:{x['name']}" for h in ("a", "b") for x in self.man["gates"][h] if not x["pass"]}
        return status, failed, self.man

    def aborted(self) -> bool:
        return self.g.stop_requested()

    # ----------------------------------------------------------------
    def run(self) -> tuple[str, set, dict]:
        g, cell = self.g, self.cell
        self.save()
        g.log(f"{self.label}: start ({cell.kind}, {cell.duration_s} s, lead {cell.values['lead_s']} s)")
        # 1 preflight, both hosts in parallel
        pf_args = {"cell": cell.to_dict(), "expect": g.grid.expect}
        pa, pb = both(lambda: self.call(g.a, "preflight", pf_args | {"prev_clock": g.prev_clock["a"]}),
                      lambda: self.call(g.b, "preflight", pf_args | {"prev_clock": g.prev_clock["b"]}))
        ga = [g.code_match] + (pa.get("gates") or [preflight.result("agent", False, pa.get("error", "no reply"))])
        gb = pb.get("gates") or [preflight.result("agent", False, pb.get("error", "no reply"))]
        self.man["gates"] = {"a": ga, "b": gb}
        for h, gl in (("a", ga), ("b", gb)):
            for x in gl:
                if x["name"] == "clock_offset" and (x.get("data") or {}).get("clock"):
                    self.man["clock"][h] = x["data"]["clock"]
                    g.prev_clock[h] = x["data"]["clock"]
                if x["name"] == "ptp":
                    d = x.get("data") or {}
                    self.man["ptp"][h] = {k: d.get(k) for k in ("state", "servo_lines_30s", "offset_ns", "carrier",
                                                                "speed_mbps") if k in d}
                if x["name"] == "modem":
                    d = x.get("data") or {}
                    self.man.setdefault("modem_preflight", {})[h] = {k: d.get(k) for k in
                                                                     ("band", "arfcn", "pci", "allowed_nr", "state")}
        self.man["timeline"]["preflight"] = utc_iso()
        failed = [f"{h.upper()} {x['name']}: {x['detail']}" for h, gl in (("a", ga), ("b", gb)) for x in gl
                  if not x["pass"]]
        self.step("preflight", not failed, "; ".join(failed)[:600])
        if failed:
            return self.finish("SKIPPED", "gate: " + "; ".join(failed)[:400])
        if self.aborted():
            return self.finish("ABORTED", "operator stop before arming")

        # 2+3 arm both hosts (captures + clock); the epoch is computed ONCE, here.
        lead = int(cell.values["lead_s"])
        epoch = int(time.time()) + lead
        self.man["epoch"], self.man["epoch_iso"] = epoch, utc_iso(epoch)
        self.save()
        return self._armed_run(epoch, time.time())

    def _armed_run(self, epoch: int, t_arm: float) -> tuple[str, set, dict]:
        g, cell = self.g, self.cell
        arm_args = {"span_s": cell.span_s}
        ra, rb = both(lambda: self.call(g.a, "arm", arm_args), lambda: self.call(g.b, "arm", arm_args))
        self.man["clock"]["a"] = ra.get("clock") or self.man["clock"]["a"]
        self.man["clock"]["b"] = rb.get("clock") or self.man["clock"]["b"]
        self.step("arm_b", bool(rb.get("ok")), rb.get("error") or _cap_summary(rb.get("captures")))
        self.step("arm_a", bool(ra.get("ok")), ra.get("error") or _cap_summary(ra.get("captures")))
        if rb.get("ok"):
            self.man["timeline"]["armed_b"] = utc_iso()
        if ra.get("ok"):
            self.man["timeline"]["armed_a"] = utc_iso()
        if not (ra.get("ok") and rb.get("ok")):
            self.teardown("arming failed")
            which = [h for h, r in (("a", ra), ("b", rb)) if not r.get("ok")]
            for h in which:
                self.man["gates"][h].append(preflight.result(f"arm_{h}", False, str(
                    (ra if h == "a" else rb).get("error") or "captures did not come up")))
            return self.finish("SKIPPED", f"arming failed on {', '.join(which).upper()}: captures did not come up")
        if self.aborted():
            self.teardown("operator stop")
            return self.finish("ABORTED", "operator stop after arming")
        # B's subscriber joins the room first; the publisher fires at the epoch.
        sb = self.call(g.b, "publish", cell.subscriber_args())
        self.step("subscriber", bool(sb.get("ok")), sb.get("error") or f"pid {(sb.get('process') or {}).get('pid')}")
        if not sb.get("ok"):
            self.teardown("subscriber did not start")
            self.man["gates"]["b"].append(preflight.result("subscriber", False, str(sb.get("error"))))
            return self.finish("SKIPPED", f"subscriber did not start: {sb.get('error')}")
        remain = epoch - time.time()
        armed_for = time.time() - t_arm
        if remain < MIN_REMAIN_S:
            self.teardown("lead too short")
            why = (f"arming took {armed_for:.0f} s of the {cell.values['lead_s']} s lead; only {remain:.0f} s left. "
                   f"Use lead_s >= {int(armed_for) + 30}")
            self.man["gates"]["a"].append(preflight.result("lead_time", False, why))
            return self.finish("SKIPPED", why)
        pa = self.call(g.a, "publish", {"epoch": epoch, "harness_args": cell.harness_args(),
                                        "harness_env": cell.harness_env(),
                                        "geometry": {"bpp": cell.values["bpp"]}})
        self.step("publish", bool(pa.get("ok")), pa.get("error") or
                  f"scheduled at {utc_iso(epoch)} ({remain:.0f} s after arming took {armed_for:.0f} s)")
        if not pa.get("ok"):
            self.teardown("publisher did not schedule")
            return self.finish("INCOMPLETE", f"publisher did not start: {pa.get('error')}")

        # 5 run: poll both until the publisher exits or the cell overruns
        status = self.poll(epoch)
        # 6 close (always), 7 mirror
        closed_ok = self.close_both()
        if status == "ABORTED":
            return self.finish("ABORTED", "operator stop during the cell")
        mirrored = self.mirror() if closed_ok is not None else False
        run = read_json(self.dir / "hosta" / "run.json", {}) or {}
        proc = read_json(self.dir / "hosta" / "process.json", {}) or {}
        proc["fired_at"] = (read_json(self.dir / "hosta" / "fired.json", {}) or {}).get("fired_at")
        checks = proc.get("checks") or {}
        self.man["negotiated"] = {"width": run.get("width"), "height": run.get("height"),
                                  "encoder_implementation": run.get("encoder_implementation"),
                                  "codec": run.get("codec")}
        fired = proc.get("fired_at")
        self.man["epoch_fired_at"] = fired
        if fired is None:
            self.problems.append("publisher never fired")
        elif abs(fired - epoch) > 1.0:
            self.problems.append(f"ANCHOR MISMATCH: planned epoch {epoch}, fired {fired:.3f}")
        if run.get("encoder_implementation") and "nvenc" not in str(run["encoder_implementation"]).lower() \
                and "nvidia" not in str(run["encoder_implementation"]).lower():
            self.problems.append(f"encoder was {run['encoder_implementation']}, not NVENC")
        if checks.get("codec_matches") is False:
            self.problems.append(f"negotiated codec {checks.get('codec')} != requested {checks.get('requested_codec')}")
        if checks.get("clip_matches") is False:
            self.problems.append(f"harness used source {checks.get('camera_source')}, not the grid's clip")
        if checks.get("stale_participants_at_join"):
            self.problems.append(f"room had {checks['stale_participants_at_join']} participant(s) before the publisher joined")
        if self.cell.values["kbps"] * 1000 != (run.get("max_bitrate_bps") or checks.get("max_bitrate_bps")):
            self.problems.append(f"bitrate cap ran as {run.get('max_bitrate_bps')} bps, grid asked "
                                 f"{self.cell.values['kbps'] * 1000}")
        if not closed_ok:
            self.problems.append("a capture did not close complete")
        if not mirrored:
            self.problems.append("mirror not verified")
        self.man["integrity"] = {"captures_complete": bool(closed_ok), "mirror_verified": bool(mirrored),
                                 "problems": list(self.problems)}
        if self.problems:
            return self.finish("INCOMPLETE", "; ".join(self.problems)[:600])
        return self.finish("OK")

    def poll(self, epoch: int) -> str:
        g, cell = self.g, self.cell
        hard_end = epoch + cell.duration_s + 60
        seen_live = False
        while True:
            if self.aborted():
                self.g.log(f"{self.label}: STOP requested mid-cell")
                self.call(g.a, "stop", timeout=TIMEOUTS["stop"])
                self.call(g.b, "stop", timeout=TIMEOUTS["stop"])
                return "ABORTED"
            time.sleep(POLL_S)
            now = time.time()
            sa, sb = both(lambda: self.call(g.a, "status"), lambda: self.call(g.b, "status"))
            pa, pb = sa.get("process") or {}, sb.get("process") or {}
            if not sa.get("ok") or not sb.get("ok"):
                self.problems.append(f"status failed at +{now - epoch:.0f} s: A {sa.get('error')} B {sb.get('error')}")
            for h, s in (("A", sa), ("B", sb)):
                for kind, c in ((s.get("captures") or {}).items()):
                    if not (isinstance(c, dict) and "alive" in c) or now >= epoch + cell.duration_s:
                        continue
                    msg = None
                    if not c["alive"]:
                        msg = f"{h} {kind} capture died mid-cell"
                    elif now > epoch + 20 and not c.get("growing"):
                        msg = f"{h} {kind} capture stopped growing mid-cell"
                    if msg and msg not in self.problems:
                        self.problems.append(msg)
            if now >= epoch:
                if pa.get("jsonl_bytes"):
                    if not seen_live:
                        seen_live = True
                        self.man["timeline"]["published"] = utc_iso()
                        self.step("live", True, f"first snapshot by +{now - epoch:.0f} s")
                elif now > epoch + LIVENESS_S:
                    what = "DEAD" if not pa.get("alive") else "STALL"
                    self.problems.append(f"publisher {what}: no snapshot within {LIVENESS_S} s of the epoch "
                                         f"({pa.get('exit') or ''})"[:300])
                    self.step("live", False, self.problems[-1])
                    return "INCOMPLETE"
                if seen_live and not pa.get("alive"):
                    early = now < epoch + cell.duration_s - 5
                    self.step("publisher_exit", not early,
                              f"exited at +{now - epoch:.0f} s of {cell.duration_s} s" + (" (EARLY)" if early else ""))
                    if early:
                        self.problems.append("publisher exited early")
                    if not pb.get("alive") and now < epoch + cell.duration_s - 5:
                        self.problems.append("subscriber exited before the publisher finished")
                    return "RAN"
                if seen_live and not pb.get("alive"):
                    msg = "subscriber died mid-cell"
                    if msg not in self.problems:
                        self.problems.append(msg)
            if now > hard_end:
                self.problems.append(f"publisher still running {now - epoch:.0f} s after the epoch; stopped")
                return "RAN"

    def close_both(self) -> bool | None:
        g = self.g
        span_end = self.man["epoch"] - int(self.cell.values["lead_s"]) + self.cell.span_s
        t = max(0, span_end - time.time()) + 400
        ca, cb = both(lambda: self.call(g.a, "close", timeout=t), lambda: self.call(g.b, "close", timeout=t))
        self.step("close", bool(ca.get("complete") and cb.get("complete")),
                  f"A complete={ca.get('complete')} {ca.get('error') or ''}; B complete={cb.get('complete')} "
                  f"{cb.get('error') or ''}")
        self.man["captures"] = {"a": ca.get("captures"), "b": cb.get("captures")}
        self.man["timeline"]["closed"] = utc_iso()
        ka, kb = both(lambda: self.call(g.a, "checksums"), lambda: self.call(g.b, "checksums"))
        self.step("checksums", bool(ka.get("ok") and kb.get("ok")),
                  f"A {ka.get('files', ka.get('error'))} files; B {kb.get('files', kb.get('error'))} files")
        if not (ka.get("ok") and kb.get("ok")):
            self.problems.append("sha256 manifest not written on " +
                                 ", ".join(h for h, k in (("A", ka), ("B", kb)) if not k.get("ok")))
            return None
        return bool(ca.get("complete") and cb.get("complete"))

    def mirror(self) -> bool:
        g = self.g
        ra, rb = g.ident["a"]["results_root"], g.ident["b"]["results_root"]
        pull_b = self.call(g.a, "pull", {"remote_dir": f"{rb}/{self.rel}/hostb", "peer_role": "b"})
        push_a = self.call(g.b, "pull", {"remote_dir": f"{ra}/{self.rel}/hosta", "peer_role": "a"})
        ok = bool(pull_b.get("ok") and push_a.get("ok"))
        self.step("mirror", ok, f"A<-B {_mirror_summary(pull_b)}; B<-A {_mirror_summary(push_a)}")
        if ok:
            self.man["timeline"]["mirrored"] = utc_iso()
        return ok

    def teardown(self, why: str):
        """Stop what was started (recorded pids only) and close captures so diag-log-off runs."""
        g = self.g
        self.g.log(f"{self.label}: teardown ({why})")
        both(lambda: self.call(g.a, "stop", timeout=TIMEOUTS["stop"]),
             lambda: self.call(g.b, "stop", timeout=TIMEOUTS["stop"]))
        t = self.cell.span_s + 300
        both(lambda: self.call(g.a, "close", timeout=t), lambda: self.call(g.b, "close", timeout=t))


def _cap_summary(c) -> str:
    if not isinstance(c, dict):
        return ""
    return ", ".join(f"{k} {'alive' if v.get('alive') else 'DEAD'} {v.get('bytes')} B"
                     for k, v in c.items() if isinstance(v, dict) and "alive" in v)


def _mirror_summary(r: dict) -> str:
    if r.get("ok"):
        return f"{(r.get('verified') or {}).get('files')} files verified"
    return str(r.get("error") or (r.get("verified") or {}).get("problems", [])[:3])


# ---------------------------------------------------------------- entry points

def run_grid(grid_path, *, resume: bool = False, out=print) -> str:
    return GridRun(grid_path, resume=resume, out=out).run()


def stop_grid(grid_id: str) -> Path:
    cfg = hostcfg.load()
    d = Path(cfg["results_root"]) / grid_id
    if not d.is_dir():
        raise RuntimeError(f"no grid {grid_id} under {cfg['results_root']}")
    p = d / "STOP"
    p.write_text(f"stop requested {utc_iso()}\n")
    return p


def grid_status(grid_id: str) -> dict:
    cfg = hostcfg.load()
    d = Path(cfg["results_root"]) / grid_id
    st = read_json(d / "state.json", {}) or {}
    rows = []
    for m in sorted((d / "cells").glob("*/manifest.json")):
        man = read_json(m, {}) or {}
        rows.append({k: man.get(k) for k in ("index", "label", "kind", "status", "status_reason")})
    rows.sort(key=lambda r: r.get("index") or 0)
    return {"grid_id": grid_id, "dir": str(d), "state": st, "cells": rows,
            "stop_requested": (d / "STOP").exists(), "paused": (d / "PAUSED").read_text().strip()
            if (d / "PAUSED").exists() else None}
