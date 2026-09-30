"""Runs a grid from Host A: every cell on both hosts in lockstep (ARCHITECTURE §5).

    run_grid(path, resume=False)     stop_grid(grid_id)     grid_status(grid_id)

Each step has a timeout and writes manifest.json when it finishes; grid.log gets a
timestamped line per step. Host B is driven over SSH (BatchMode, one agent command per call);
Host A's agent is run as a local subprocess the same way, so a hung step on either host is a
timeout here, never a hang.

Statuses: OK | SKIPPED (a gate failed; nothing published) | INCOMPLETE (ran, but a capture,
liveness, close or pull/verify step failed, or B rendered under half the frames the cell should
carry -- kept, excluded from comparison) | ABORTED
(operator stop). Two consecutive SKIPPED on the same gate PAUSE the grid (it exits, resumable
with --resume), since the cause is environmental. A control cell that fails its thresholds
(or does not complete) stops the grid.

Layout v2 (CONTRACT.md): a cell is <grid>/<combo>/r<n> (controls/x<NN>); Cell.rel_path is the
only derivation and both agents are handed <grid-id>/<rel_path>. Storage is one-way: after a
cell closes, A pulls hostb/, verifies every file (sha256 and size) against B's SHA256SUMS, and
only then has B purge its copy (unless host.yaml sets b_keep_after_pull). A failed verify
keeps B's copy and makes the cell INCOMPLETE; the grid goes on.

Reduction and reports never hold up the next cell: each finished cell is queued to ONE
background worker process (nice 19, idle I/O, strictly one cell at a time), which also
zstd-compresses the cell's raw captures once its reduction has succeeded (compress_raw) and
re-renders the combination summary and the grid comparison. At the end of the grid the
orchestrator waits for the queue to drain, then renders the final comparison.

The orchestrator never reads credentials and never passes one to either host.
"""
from __future__ import annotations

import argparse
import base64
import concurrent.futures as cf
import contextlib
import datetime as _dt
import fcntl
import importlib
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

from . import grid as gridmod
from . import hostcfg, preflight
from .capture import (CODE_ROOT, SUMS_NAME, ZST_SUMS_NAME, pid_alive, read_json, sha256_file, start_ticks,
                      write_json_atomic, write_sums_file)

LIVENESS_S = 25          # publish-cell.sh: first snapshot within 25 s of the epoch, or the cell is dead
POLL_S = 10
MIN_REMAIN_S = 15        # paired-cell.sh: do not walk into at-epoch's refusal
# Below half of fps x duration, B was not watching the stream the cell measured. The H.265
# smoke cell of 2026-09-30 ran clean on A and rendered 0 frames on B, and still said OK.
MIN_RENDERED_FRACTION = 0.5
TIMEOUTS = {"identity": 90, "preflight": 60, "arm": 120, "publish": 45, "status": 45, "checksums": 1200,
            "pull": 1800, "purge": 300, "stop": 180}
FINAL = ("OK", "SKIPPED", "INCOMPLETE", "ABORTED")


def rendered_frames(hostb: Path) -> int | None:
    """Data rows in B's subscriber.csv, one per frame B rendered; None if the file is missing."""
    try:
        with (hostb / "subscriber.csv").open("rb") as f:
            return max(sum(1 for line in f if line.strip()) - 1, 0)
    except OSError:
        return None


def utc_iso(t: float | None = None, ms: bool = False) -> str:
    d = _dt.datetime.fromtimestamp(time.time() if t is None else t, _dt.timezone.utc)
    return d.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z" if ms else d.strftime("%Y-%m-%dT%H:%M:%SZ")


def append_log(grid_dir: Path, line: str) -> None:
    """One whole line per write (O_APPEND), so the orchestrator and the worker can share grid.log."""
    grid_dir.mkdir(parents=True, exist_ok=True)
    with open(grid_dir / "grid.log", "a") as f:
        f.write(line + "\n")


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


# ---------------------------------------------------------------- background post-processing
#
# <grid>/postproc/ is a spool that outlives any one process:
#   pending/<seq>.json   jobs, oldest first (written only by the orchestrator)
#   running.json         the job the worker is on (moved back to pending once if a worker dies)
#   done/<seq>.json      finished jobs, each step's outcome
#   worker.json          the current worker's pid; worker.lock (flock) = one worker per grid
#   CLOSE                the orchestrator will queue nothing more: drain, then exit
# `grid status` reads it directly, so the queue depth is visible from any shell.

POSTPROC_DIR = "postproc"
# (manifest timeline key, module, function) run on the cell directory, in order
CELL_STEPS = [["reduced", "teleop.grid.reduce", "reduce_cell"],
              ["metrics", "teleop.grid.metrics", "build"],
              ["reported", "teleop.grid.report.cell", "render"]]
COMBO_RENDER = ["teleop.grid.report.combo", "render"]     # optional module: absent = skipped
GRID_RENDER = ["teleop.grid.report.grid", "render"]
STEP_TIMEOUT_S = 3600    # a step that hangs must not wedge the queue (and the end-of-grid drain) forever
ZSTD_ARGS = ["-3", "-T2", "-q"]


class StepTimeout(BaseException):
    """Raised by SIGALRM inside a post-processing step. BaseException, so a module's own
    `except Exception` cannot swallow it and carry on past its deadline."""


@contextlib.contextmanager
def _deadline(seconds: float | None):
    """SIGALRM-based step deadline; a no-op off the main thread (tests) or without a limit."""
    if not seconds or threading.current_thread() is not threading.main_thread():
        yield
        return

    def fire(signum, frame):
        raise StepTimeout(f"step exceeded {seconds:.0f} s")
    old = signal.signal(signal.SIGALRM, fire)
    signal.alarm(int(seconds))
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)


def _seq(p: Path) -> int:
    try:
        return int(p.name.split(".", 1)[0])
    except ValueError:
        return 0


def queue_paths(grid_dir: Path) -> dict:
    q = Path(grid_dir) / POSTPROC_DIR
    return {"dir": q, "pending": q / "pending", "done": q / "done", "running": q / "running.json",
            "close": q / "CLOSE", "worker": q / "worker.json", "lock": q / "worker.lock"}


def pending_jobs(grid_dir: Path) -> list[Path]:
    return sorted(queue_paths(grid_dir)["pending"].glob("*.json"), key=_seq)


def queue_status(grid_dir: Path) -> dict:
    """What `grid status` shows: depth = queued + the one running."""
    qp = queue_paths(grid_dir)
    pending = pending_jobs(grid_dir)
    running = read_json(qp["running"]) if qp["running"].exists() else None
    done = sorted(qp["done"].glob("*.json"), key=_seq) if qp["done"].is_dir() else []
    failed = 0
    for p in done:
        res = (read_json(p, {}) or {}).get("result") or {}
        if not res.get("ok", True):
            failed += 1
    worker = read_json(qp["worker"], {}) or {}
    wpid = worker.get("pid")
    alive = bool(wpid) and pid_alive(wpid) and (worker.get("start_ticks") is None
                                                or start_ticks(wpid) == worker.get("start_ticks"))
    return {"queued": len(pending), "running": (running or {}).get("label") if running else None,
            "running_since": (running or {}).get("started_at") if running else None,
            "depth": len(pending) + (1 if running else 0), "done": len(done), "failed": failed,
            "worker_pid": wpid if alive else None, "closed": qp["close"].exists(),
            "next": [(read_json(p, {}) or {}).get("label") for p in pending[:3]]}


class PostQueue:
    """The orchestrator's side of the spool: enqueue, keep one worker alive, wait."""

    def __init__(self, run_dir: Path, log, *, worker_argv: list[str] | None = None, poll_s: float = 1.0,
                 worker_poll_s: float = 1.0, autostart: bool = True):
        self.run_dir = Path(run_dir)
        self.paths = queue_paths(self.run_dir)
        self.log = log
        self.worker_argv = worker_argv
        self.poll_s = poll_s
        self.worker_poll_s = worker_poll_s
        self.autostart = autostart
        self.proc: subprocess.Popen | None = None
        self.paths["pending"].mkdir(parents=True, exist_ok=True)
        self.paths["done"].mkdir(parents=True, exist_ok=True)
        self.paths["close"].unlink(missing_ok=True)      # a resumed grid queues again
        existing = list(self.paths["pending"].glob("*.json")) + list(self.paths["done"].glob("*.json"))
        running = read_json(self.paths["running"]) if self.paths["running"].exists() else None
        self.seq = 1 + max([_seq(p) for p in existing] + [int((running or {}).get("id") or 0)])

    def depth(self) -> int:
        return len(pending_jobs(self.run_dir)) + (1 if self.paths["running"].exists() else 0)

    def queued_rel_paths(self) -> set[str]:
        out = {(read_json(p, {}) or {}).get("rel_path") for p in pending_jobs(self.run_dir)}
        if self.paths["running"].exists():
            out.add((read_json(self.paths["running"], {}) or {}).get("rel_path"))
        return {x for x in out if x}

    def enqueue(self, job: dict) -> str:
        jid = f"{self.seq:06d}"
        self.seq += 1
        job = dict(job, id=jid, queued_at=utc_iso())
        write_json_atomic(self.paths["pending"] / f"{jid}.json", job)
        what = "reduce, metrics, report" + (", compress" if job.get("compress_raw") else "") \
            if job.get("cell_steps") else "comparison only"
        self.log(f"postproc: queued {job.get('label')} ({what}); queue depth {self.depth()}")
        if self.autostart:
            self.ensure_worker()
        return jid

    def worker_alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def ensure_worker(self) -> None:
        if self.worker_alive():
            return
        if self.proc is not None:
            self.log(f"postproc: worker pid {self.proc.pid} exited (rc {self.proc.returncode}); starting another")
        argv = self.worker_argv or [sys.executable, "-m", "teleop.grid.orchestrator", "postproc-worker",
                                    "--grid-dir", str(self.run_dir), "--owner-pid", str(os.getpid()),
                                    "--owner-ticks", str(start_ticks(os.getpid()) or ""),
                                    "--poll-s", str(self.worker_poll_s)]
        with open(self.paths["dir"] / "worker.log", "ab") as out:
            # Own session: an operator's Ctrl-C of the orchestrator does not kill a reduction
            # half-way; the worker finishes the queue and exits once its owner is gone.
            self.proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                                         cwd=CODE_ROOT, start_new_session=True, close_fds=True)
        self.log(f"postproc: worker pid {self.proc.pid} started (nice 19, idle I/O, one cell at a time)")

    def close(self) -> None:
        """Nothing more will be queued: the worker drains and exits."""
        self.paths["close"].write_text(f"closed {utc_iso()}\n")

    def _wait(self, done, what: str, log_every_s: float = 60.0, timeout: float | None = None) -> bool:
        t0 = last = time.time()
        while not done():
            if self.autostart and self.depth() and not self.worker_alive():
                self.ensure_worker()
            now = time.time()
            if timeout is not None and now - t0 > timeout:
                return False
            if now - last >= log_every_s:
                st = queue_status(self.run_dir)
                self.log(f"postproc: waiting for {what}: depth {st['depth']}, running {st['running']}")
                last = now
            time.sleep(self.poll_s)
        return True

    def wait_for(self, jid: str, timeout: float | None = None) -> dict | None:
        """Block until job `jid` is done (a control cell's thresholds need its metrics.json)."""
        target = self.paths["done"] / f"{jid}.json"
        self._wait(target.exists, f"job {jid}", timeout=timeout)
        return read_json(target)

    def wait_drained(self, timeout: float | None = None) -> bool:
        ok = self._wait(lambda: self.depth() == 0, "the queue to drain", timeout=timeout)
        if ok and self.proc is not None:
            try:
                self.proc.wait(timeout=30)       # it exits by itself once CLOSE is there
            except subprocess.TimeoutExpired:
                pass
        return ok


# ---- the worker process

def lower_priority() -> list[str]:
    """nice 19 and the idle I/O class for this process (children inherit both: tshark, zstd).
    `ionice -c3 -p <pid>` first, else the ioprio_set syscall directly (no psutil)."""
    notes = []
    try:
        now = os.nice(0)
        notes.append(f"nice {os.nice(max(0, 19 - now))}")
    except OSError as e:
        notes.append(f"nice FAILED ({e})")
    ok = False
    exe = shutil.which("ionice")
    if exe:
        try:
            ok = subprocess.run([exe, "-c3", "-p", str(os.getpid())], capture_output=True, timeout=10).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            ok = False
    if not ok:
        ok = _ioprio_set_idle()
    notes.append("ionice idle" if ok else "ionice FAILED (normal I/O priority)")
    return notes


def _ioprio_set_idle() -> bool:
    import ctypes  # noqa: PLC0415
    import platform  # noqa: PLC0415
    nr = {"x86_64": 251, "aarch64": 30, "i386": 289, "i686": 289}.get(platform.machine())
    if nr is None:
        return False
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        # IOPRIO_WHO_PROCESS = 1, this process = 0, IOPRIO_CLASS_IDLE (3) << IOPRIO_CLASS_SHIFT (13)
        return libc.syscall(nr, 1, 0, 3 << 13) == 0
    except (OSError, AttributeError):
        return False


def find_zstd() -> str | None:
    return "/usr/bin/zstd" if os.access("/usr/bin/zstd", os.X_OK) else shutil.which("zstd")


def compress_cell(cdir: Path, *, zstd: str | None = None, run=subprocess.run) -> dict:
    """zstd -3 -T2 every *.dlf and *.pcap under hosta/ and hostb/. An original is removed only
    after `zstd -t` has passed on its .zst; any failure keeps the original (and drops the .zst).
    SHA256SUMS stays as written (the originals); SHA256SUMS.zst beside it lists the .zst files.
    Idempotent: a .zst left by an interrupted attempt is redone from the original."""
    cdir = Path(cdir)
    zstd = zstd or find_zstd()
    rec = {"at": utc_iso(), "tool": "zstd " + " ".join(ZSTD_ARGS), "files": [], "bytes_before": 0,
           "bytes_after": 0, "problems": [], "ok": True}
    if not zstd:
        rec.update(ok=False, problems=["zstd not found; raw captures left uncompressed"])
        return rec
    for host in ("hosta", "hostb"):
        hd = cdir / host
        if not hd.is_dir():
            continue
        raw = sorted(p for p in hd.rglob("*") if p.is_file() and p.suffix in (".dlf", ".pcap"))
        for f in raw:
            rel = f.relative_to(cdir).as_posix()
            z = f.with_name(f.name + ".zst")
            before = f.stat().st_size
            z.unlink(missing_ok=True)
            try:
                r = run([zstd, *ZSTD_ARGS, str(f), "-o", str(z)], capture_output=True, text=True, timeout=3600)
            except (OSError, subprocess.TimeoutExpired) as e:
                rec["problems"].append(f"{rel}: zstd failed ({e}); original kept")
                z.unlink(missing_ok=True)
                continue
            if r.returncode != 0 or not z.is_file():
                rec["problems"].append(f"{rel}: zstd rc {r.returncode} {(r.stderr or '').strip()[-160:]}; original kept")
                z.unlink(missing_ok=True)
                continue
            try:
                t = run([zstd, "-t", "-q", str(z)], capture_output=True, text=True, timeout=3600)
                tested = t.returncode == 0
                why = f"rc {t.returncode} {(t.stderr or '').strip()[-160:]}"
            except (OSError, subprocess.TimeoutExpired) as e:
                tested, why = False, str(e)
            if not tested:
                rec["problems"].append(f"{rel}: zstd -t failed ({why}); original kept, .zst removed")
                z.unlink(missing_ok=True)
                continue
            after = z.stat().st_size
            try:
                f.unlink()
            except OSError as e:
                # both stay: the tested .zst and the original (a retry redoes the .zst from it)
                rec["problems"].append(f"{rel}: compressed and tested, but the original could not be removed ({e})")
                continue
            rec["files"].append({"path": rel, "zst": rel + ".zst", "bytes_before": before, "bytes_after": after})
        zsts = [p for p in hd.rglob("*.zst") if p.is_file() and p.name != ZST_SUMS_NAME]
        if zsts:
            write_sums_file(hd, zsts, ZST_SUMS_NAME)
    rec["bytes_before"] = sum(x["bytes_before"] for x in rec["files"])
    rec["bytes_after"] = sum(x["bytes_after"] for x in rec["files"])
    rec["ok"] = not rec["problems"]
    return rec


def _update_manifest(cdir: Path, fn) -> None:
    mp = Path(cdir) / "manifest.json"
    if not mp.exists():
        return
    man = read_json(mp, {}) or {}
    fn(man)
    write_json_atomic(mp, man)


def _run_step(importer, mod: str, fn: str, arg: Path, name: str, log, cdir: Path | None,
              timeout: float | None) -> tuple[bool, str]:
    t0 = time.time()
    try:
        with _deadline(timeout):
            getattr(importer(mod), fn)(arg)
        log(f"{name}: {mod}.{fn} ok ({time.time() - t0:.0f} s)")
        return True, ""
    except (Exception, StepTimeout) as e:  # noqa: BLE001 -- modules built in parallel may be missing or failing
        why = f"{type(e).__name__}: {e}"
        log(f"{name}: {mod}.{fn} FAILED: {why}")
        if cdir is not None and cdir.exists():
            with open(cdir / "postprocess-errors.log", "a") as f:
                f.write(f"{utc_iso()} {mod}.{fn}\n{traceback.format_exc()}\n")
        return False, why


def run_job(grid_dir: Path, job: dict, log, *, importer=importlib.import_module, zstd: str | None = None,
            more_pending=lambda: False, step_timeout: float | None = STEP_TIMEOUT_S) -> dict:
    """One queued cell: reduce -> metrics -> cell report (each recorded, none fatal), then --
    only if all three succeeded -- compress its raw captures, then the combination summary and
    the grid comparison. The comparison is skipped while more jobs wait (the next job renders
    it; the end of the grid renders it once more)."""
    grid_dir = Path(grid_dir)
    cdir = grid_dir / job["rel_path"]
    name = job.get("label") or job["rel_path"]
    out: dict = {"steps": {}, "compress": None, "renders": {}}
    if job.get("cell_steps"):
        for key, mod, fn in job.get("steps") or CELL_STEPS:
            ok, why = _run_step(importer, mod, fn, cdir, name, log, cdir, step_timeout)
            out["steps"][key] = ok if ok else why
            if key in ("reduced", "reported"):
                _update_manifest(cdir, lambda m: m.setdefault("timeline", {}).__setitem__(key, utc_iso() if ok else None))
        if job.get("compress_raw"):
            failed = [k for k, v in out["steps"].items() if v is not True]
            if failed:
                out["compress"] = {"skipped": f"{', '.join(failed)} did not succeed"}
                log(f"{name}: raw captures left UNCOMPRESSED: {', '.join(failed)} did not succeed")
            elif not cdir.is_dir():
                out["compress"] = {"skipped": "no cell directory"}
            else:
                rec = compress_cell(cdir, zstd=zstd)
                out["compress"] = {k: rec[k] for k in ("ok", "bytes_before", "bytes_after", "problems")} | \
                    {"files": len(rec["files"])}
                gb = 1e9
                if rec["files"] or rec["problems"]:
                    ratio = rec["bytes_before"] / rec["bytes_after"] if rec["bytes_after"] else 0
                    log(f"{name}: compressed {len(rec['files'])} raw file(s) {rec['bytes_before'] / gb:.2f} -> "
                        f"{rec['bytes_after'] / gb:.2f} GB ({ratio:.2f}x)"
                        + (f"; PROBLEMS: {'; '.join(rec['problems'][:3])}" if rec["problems"] else ""))

                def record(m, rec=rec):
                    prev = m.get("raw_compressed") or {}
                    if rec["files"] or rec["problems"] or not prev:
                        if prev.get("files") and rec["files"]:
                            rec = dict(rec, files=prev["files"] + rec["files"])
                            rec["bytes_before"] = sum(x["bytes_before"] for x in rec["files"])
                            rec["bytes_after"] = sum(x["bytes_after"] for x in rec["files"])
                        m["raw_compressed"] = rec
                    m.setdefault("timeline", {})["compressed"] = rec["at"] if rec["ok"] else None
                _update_manifest(cdir, record)
    combo = job.get("combo")
    if job.get("kind") != "control" and combo:
        mod, fn = job.get("combo_render") or COMBO_RENDER
        try:
            module = importer(mod)
        except ModuleNotFoundError as e:
            module = None
            if e.name != mod:
                log(f"{name}: {mod} could not be imported: {e}")
            out["renders"]["combo"] = "absent"
        if module is not None:
            ok, why = _run_step(lambda _m: module, mod, fn, grid_dir / combo, f"{name} [{combo}]", log, None,
                                step_timeout)
            out["renders"]["combo"] = ok if ok else why
    if more_pending():
        out["renders"]["grid"] = "deferred"
        log(f"{name}: grid comparison deferred to the next job ({'more jobs queued'})")
    else:
        mod, fn = job.get("grid_render") or GRID_RENDER
        ok, why = _run_step(importer, mod, fn, grid_dir, f"{name} [comparison]", log, None, step_timeout)
        out["renders"]["grid"] = ok if ok else why
    out["ok"] = all(v is True for v in out["steps"].values()) and \
        (out["compress"] is None or out["compress"].get("ok", True) or "skipped" in out["compress"]) and \
        all(v in (True, "absent", "deferred") for v in out["renders"].values())
    return out


def _owner_alive(pid: int | None, ticks: int | None) -> bool:
    if not pid:
        return False
    return pid_alive(pid) and (ticks is None or start_ticks(pid) == ticks)


def worker_main(grid_dir, *, owner_pid: int | None = None, owner_ticks: int | None = None, poll_s: float = 1.0,
                importer=importlib.import_module, priority=lower_priority, zstd: str | None = None,
                step_timeout: float | None = STEP_TIMEOUT_S, echo: bool = True) -> int:
    """The background worker: take the oldest pending job, run it, repeat. Exits when the queue
    is empty and either CLOSE exists or the orchestrator that started it is gone."""
    grid_dir = Path(grid_dir)
    qp = queue_paths(grid_dir)
    qp["pending"].mkdir(parents=True, exist_ok=True)
    qp["done"].mkdir(parents=True, exist_ok=True)
    notes = priority()

    def log(msg: str):
        line = f"{utc_iso()} [postproc] {msg}"
        if echo:
            print(line, flush=True)      # worker.log (its stdout) keeps a copy beside the tracebacks
        append_log(grid_dir, line)

    with open(qp["lock"], "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)          # one worker per grid: a second one waits here
        me = os.getpid()
        write_json_atomic(qp["worker"], {"pid": me, "start_ticks": start_ticks(me), "started": utc_iso(),
                                         "owner_pid": owner_pid, "priority": notes})
        log(f"worker pid {me} running ({', '.join(notes)})")
        if qp["running"].exists():
            job = read_json(qp["running"], {}) or {}
            jid = job.get("id") or "000000"
            if int(job.get("attempts") or 0) >= 2:
                job["result"] = {"ok": False, "error": "a worker died twice on this job; not retried"}
                job["finished_at"] = utc_iso()
                write_json_atomic(qp["done"] / f"{jid}.json", job)
                log(f"{job.get('label')}: a worker died twice on this job; given up (see worker.log)")
            else:
                write_json_atomic(qp["pending"] / f"{jid}.json", job)
                log(f"{job.get('label')}: re-queued (a worker died while on it)")
            qp["running"].unlink(missing_ok=True)
        while True:
            jobs = pending_jobs(grid_dir)
            if not jobs:
                if qp["close"].exists() or not _owner_alive(owner_pid, owner_ticks):
                    break
                time.sleep(poll_s)
                continue
            path = jobs[0]
            os.replace(path, qp["running"])
            job = read_json(qp["running"], {}) or {}
            job.update(started_at=utc_iso(), attempts=int(job.get("attempts") or 0) + 1, worker_pid=me)
            write_json_atomic(qp["running"], job)
            t0 = time.time()
            log(f"{job.get('label')}: post-processing ({len(jobs) - 1} more queued)")
            try:
                job["result"] = run_job(grid_dir, job, log, importer=importer, zstd=zstd, step_timeout=step_timeout,
                                        more_pending=lambda: bool(pending_jobs(grid_dir)))
            except Exception as e:  # noqa: BLE001 -- a bug here must not stop the queue
                job["result"] = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                log(f"{job.get('label')}: post-processing CRASHED: {type(e).__name__}: {e}")
                traceback.print_exc()
            job["finished_at"] = utc_iso()
            job["elapsed_s"] = round(time.time() - t0, 1)
            write_json_atomic(qp["done"] / f"{job.get('id') or path.stem}.json", job)
            qp["running"].unlink(missing_ok=True)
            log(f"{job.get('label')}: post-processing done in {job['elapsed_s']:.0f} s"
                f" ({'ok' if (job['result'] or {}).get('ok') else 'with FAILURES'}); "
                f"{len(pending_jobs(grid_dir))} queued")
        log(f"worker pid {me} exiting: queue empty")
    return 0


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
        prev = None
        if self.run_dir.exists():
            if not resume:
                raise RuntimeError(f"{self.run_dir} exists; use --resume or a new grid id")
            if (self.run_dir / "cells").is_dir():
                raise RuntimeError(f"{self.run_dir} was started with the old cells/<label> layout; resume needs "
                                   "the layout it started with -- use a new grid id")
            prev = self._read_yaml(self.run_dir / "grid.yaml")
            seed = (prev or {}).get("seed")
        self.grid = gridmod.load(self.path, seed=seed) if seed is not None else first
        self.cells = self.grid.expand()
        if resume and seed is not None:
            recorded = [c["label"] for c in (prev or {}).get("cells", [])]
            if recorded and recorded != [c.label for c in self.cells]:
                raise RuntimeError("the grid file changed since this grid started; resume refuses to mix "
                                   "definitions -- use a new grid id")
            paths = [c.get("rel_path") for c in (prev or {}).get("cells", [])]
            if recorded and all(paths) and paths != [c.rel_path for c in self.cells]:
                raise RuntimeError("the cell directories this code derives differ from the ones this grid "
                                   "started with; resume refuses -- use a new grid id")
        self.resume = resume
        self.a = Host(self.cfg, remote=False)
        self.b = Host(self.cfg, remote=True)
        self.ident: dict = {}
        self.prev_clock = {"a": None, "b": None}
        self.gates_cfg = preflight.load_gates()
        self.keep_b = bool(self.cfg.get("b_keep_after_pull", False))
        self.post: PostQueue | None = None
        # What the background worker runs, carried in each job (None = CELL_STEPS, COMBO_RENDER,
        # GRID_RENDER); tests point these at stand-in modules.
        self.post_steps: list | None = None
        self.combo_render: list | None = None
        self.grid_render: list | None = None
        self.worker_poll_s = 1.0

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
        append_log(self.run_dir, line)

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

    def manifest(self, cell: gridmod.Cell) -> dict:
        return read_json(self.run_dir / cell.rel_path / "manifest.json", {}) or {}

    # -- step 0
    def match(self) -> dict:
        ia, ib = both(lambda: self.a.call("identity"), lambda: self.b.call("identity"))
        self.ident = {"a": ia, "b": ib}
        if not ia.get("ok") or not ib.get("ok"):
            return preflight.result("code_match", False,
                                    f"identity failed: A {ia.get('error', 'ok')}; B {ib.get('error', 'ok')}")
        return preflight.code_match(ia, ib, self.gates_cfg)

    def disk_gate(self) -> dict:
        """The whole grid (the cells still to run) against Host A's free space."""
        todo = [c for c in self.cells if not self.manifest(c)]
        return preflight.gate_grid_disk(todo, self.cfg["results_root"], self.grid.compress_raw)

    # -- whole grid
    def run(self) -> str:
        dg = self.disk_gate()
        if not dg["pass"]:
            msg = f"REFUSING the grid: not enough disk on Host A for it. {dg['detail']}"
            if self.run_dir.exists():
                self.log(msg)
                self.state(status="refused", reason=dg["detail"])
            else:
                self.out(f"{utc_iso()} {msg}")   # nothing created for a grid that never started
            return "refused"
        self.run_dir.mkdir(parents=True, exist_ok=True)
        if self.resume and self.stop_file.exists():
            self.stop_file.unlink()
        (self.run_dir / "PAUSED").unlink(missing_ok=True)
        self.grid.write_expanded(self.run_dir)
        self.log(f"grid {self.grid.id}: {len(self.cells)} cells, order {self.grid.order}, seed {self.grid.seed}"
                 + (" (resume)" if self.resume else ""))
        self.log(f"disk: {dg['detail']}")
        self.log("storage: Host A keeps everything; " + (
            "B's copy of each cell is KEPT after A verifies it (b_keep_after_pull: true)" if self.keep_b else
            "B's copy of each cell is PURGED once A has verified every file") +
            f"; raw captures {'zstd-compressed after reduction' if self.grid.compress_raw else 'kept uncompressed'}")
        self.state(status="running", grid_id=self.grid.id, cells=len(self.cells), started=utc_iso())
        self.post = PostQueue(self.run_dir, self.log, worker_poll_s=self.worker_poll_s)
        result = "crashed"
        try:
            result = self._run()
            return result
        finally:
            self._end_post(result)

    def _run(self) -> str:
        cm = self.match()
        self.log(f"match: {'PASS' if cm['pass'] else 'FAIL'} {cm['detail']}")
        if not cm["pass"]:
            self.state(status="refused", reason=cm["detail"])
            self.log("REFUSING the grid: the hosts do not run the same code")
            return "refused"
        self.code_match = cm
        if self.resume:
            self.requeue_unfinished()
        prev_skip: set[str] = set()
        for cell in self.cells:
            cdir = self.run_dir / cell.rel_path
            man = read_json(cdir / "manifest.json", {}) or {}
            if man.get("status") in FINAL:
                self.log(f"{cell.label} ({cell.rel_path}): already {man['status']}, skipping on resume")
                continue
            if cdir.exists() and man:
                man.update(status="INCOMPLETE", status_reason="orchestrator stopped mid-cell (found on resume)",
                           excluded_from_comparison=True, exclusion_reason="interrupted")
                write_json_atomic(cdir / "manifest.json", man)
                self.log(f"{cell.label} ({cell.rel_path}): found interrupted; marked INCOMPLETE")
                continue
            if self.stop_requested():
                self.log("STOP requested; ending the grid before the next cell")
                self.state(status="stopped")
                return "stopped"
            self.state(current=cell.label, current_dir=cell.rel_path)
            status, failed_gates, man = CellRun(self, cell).run()
            jid = self.enqueue_post(cell, status)
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
                # the thresholds read this cell's metrics.json: wait for its own job (only it)
                self.log(f"control {cell.label}: waiting for its reduction before judging the thresholds")
                self.post.wait_for(jid)
                verdict = self.control_verdict(cdir, status)
                self.log(f"control {cell.label}: {verdict}")
                if verdict.startswith("FAIL"):
                    self.state(status="aborted", reason=f"control {cell.label} {verdict}")
                    self.log("ABORTING the grid: the control cell failed its thresholds")
                    return "aborted"
            if cell is not self.cells[-1] and status != "SKIPPED":
                self.cooldown(int(cell.values["cooldown_s"]))
        self.state(status="done", current=None, current_dir=None)
        self.log("grid done: every cell ran")
        return "done"

    def enqueue_post(self, cell: gridmod.Cell, status: str) -> str:
        job = {"rel_path": cell.rel_path, "label": cell.label, "combo": cell.combo, "kind": cell.kind,
               "status": status, "cell_steps": status in ("OK", "INCOMPLETE"),
               "compress_raw": bool(self.grid.compress_raw)}
        for key, spec in (("steps", self.post_steps), ("combo_render", self.combo_render),
                          ("grid_render", self.grid_render)):
            if spec:
                job[key] = spec
        return self.post.enqueue(job)

    def requeue_unfinished(self) -> None:
        """Resume: a finished cell whose post-processing never completed (the orchestrator died
        with it queued, or a step failed) goes back in the queue -- unless it is queued already."""
        queued = self.post.queued_rel_paths()
        n = 0
        for cell in self.cells:
            man = self.manifest(cell)
            if man.get("status") in ("OK", "INCOMPLETE") and man.get("exclusion_reason") != "interrupted" \
                    and not (man.get("timeline") or {}).get("reported") and cell.rel_path not in queued:
                self.enqueue_post(cell, man["status"])
                n += 1
        if n:
            self.log(f"resume: {n} finished cell(s) re-queued for post-processing")

    def _end_post(self, result: str) -> None:
        """End of the grid, however it ended: nothing more will be queued. After a complete grid,
        wait for the queue to drain and render the final comparison; otherwise leave the worker
        to finish in the background (grid status shows it)."""
        if self.post is None:
            return
        self.post.close()
        if result != "done":
            st = queue_status(self.run_dir)
            if st["depth"]:
                self.log(f"postproc: {st['depth']} job(s) still queued; the worker (pid {st['worker_pid']}) finishes "
                         "them in the background -- `teleop grid status` shows progress")
            return
        depth = self.post.depth()
        if depth:
            self.log(f"postproc: waiting for {depth} queued job(s) before the final comparison")
        self.post.wait_drained()
        t0 = time.time()
        mod, fn = self.grid_render or GRID_RENDER
        try:
            out = getattr(importlib.import_module(mod), fn)(self.run_dir)
            self.log(f"final comparison: {out} ({time.time() - t0:.0f} s)")
        except Exception as e:  # noqa: BLE001
            self.log(f"final comparison FAILED: {type(e).__name__}: {e} (re-run: teleop grid report {self.grid.id})")

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


# ---------------------------------------------------------------- one cell

class CellRun:
    def __init__(self, g: GridRun, cell: gridmod.Cell):
        self.g = g
        self.cell = cell
        self.label = cell.label
        # layout v2: computed here, once, from Cell.rel_path; both agents get the same string
        self.rel = f"{g.grid.id}/{cell.rel_path}"
        self.dir = g.run_dir / cell.rel_path
        self.problems: list[str] = []
        self.collected = False
        self.b_sums_ok = False       # B wrote its SHA256SUMS: the precondition for pulling hostb/
        w, h = cell.requested
        self.man = {
            "label": cell.label, "grid_id": g.grid.id, "index": cell.index, "kind": cell.kind,
            "repeat": cell.repeat, "combo": cell.combo, "rel_path": cell.rel_path,
            "variables": cell.manifest_variables(),
            "requested": {"width": w, "height": h},
            "negotiated": {"width": None, "height": None, "encoder_implementation": None, "codec": None},
            "epoch": None, "epoch_iso": None,
            "commit": g.ident.get("a", {}).get("commit"), "harness_sha256": g.ident.get("a", {}).get("harness_sha256"),
            "clock": {"a": None, "b": None}, "ptp": {"a": None, "b": None},
            "band": {"a": None, "b": None},
            "gates": {"a": [], "b": []},
            "control_log_enabled": None, "control_rx_buffer_applied": None,
            "storage": {"b_keep_after_pull": g.keep_b, "pull": None, "purge": None},
            "status": "RUNNING", "status_reason": "",
            "timeline": {k: None for k in ("preflight", "armed_b", "armed_a", "published", "closed", "mirrored",
                                           "purged", "reduced", "reported", "compressed")},
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
        g.log(f"{self.label}: start ({cell.kind}, {cell.rel_path}, {cell.duration_s} s, lead {cell.values['lead_s']} s)")
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
        self.man["control_log_enabled"] = sb.get("control_log")
        self.man["control_rx_buffer_applied"] = sb.get("control_buffer_frames")
        self.step("subscriber", bool(sb.get("ok")), sb.get("error") or
                  f"pid {(sb.get('process') or {}).get('pid')}; control log "
                  + ({True: "ON (hostb/control.csv, --control-buffer-frames "
                             f"{sb.get('control_buffer_frames')})",
                      False: "OFF (this subscriber's --help has no --control-log)"}.get(sb.get("control_log"), "unknown")))
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
        # 6 close (always), 7 collect B's half onto A (pull, verify, purge)
        closed_ok = self.close_both()
        verified = self.collect() if self.b_sums_ok else False
        if status == "ABORTED":
            return self.finish("ABORTED", "operator stop during the cell")
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
        if not verified:
            reason = ((self.man.get("storage") or {}).get("pull") or {}).get("reason") or "not attempted"
            self.problems.append(f"hostb/ not verified on A ({reason}); B's copy kept")
        else:
            got = rendered_frames(self.dir / "hostb")
            want = int(self.cell.values["fps"]) * self.cell.duration_s
            self.man["frames_rendered_b"] = {"rows": got, "expected": want}
            if not got:
                self.problems.append("B rendered no video frames (hostb/subscriber.csv "
                                     + ("missing)" if got is None else "has no rows)"))
            elif got < MIN_RENDERED_FRACTION * want:
                self.problems.append(f"B rendered only {got} of ~{want} frames")
        self.man["integrity"] = {"captures_complete": bool(closed_ok), "mirror_verified": bool(verified),
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
        self.b_sums_ok = bool(kb.get("ok"))
        self.step("checksums", bool(ka.get("ok") and kb.get("ok")),
                  f"A {ka.get('files', ka.get('error'))} files; B {kb.get('files', kb.get('error'))} files")
        if not (ka.get("ok") and kb.get("ok")):
            self.problems.append("sha256 manifest not written on " +
                                 ", ".join(h for h, k in (("A", ka), ("B", kb)) if not k.get("ok")))
            return None
        return bool(ca.get("complete") and cb.get("complete"))

    def collect(self) -> bool:
        """B's half of the cell onto A, one way: pull hostb/, verify EVERY file (sha256 and size)
        against B's SHA256SUMS, record the result, and only then have B purge that one directory
        -- with the sha256 of the SHA256SUMS just verified, which B checks against its own.
        A verify failure keeps B's copy (the caller makes the cell INCOMPLETE); a refused purge
        keeps B's copy too but does not touch the cell's status: A's copy is verified.
        Returns whether A's copy verified."""
        g = self.g
        self.collected = True
        storage = self.man.setdefault("storage", {"b_keep_after_pull": g.keep_b})
        rb = (g.ident.get("b") or {}).get("results_root")
        if not rb:
            storage["pull"] = {"ok": False, "at": utc_iso(), "reason": "B's results_root unknown (no identity)"}
            self.step("pull_b", False, storage["pull"]["reason"])
            return False
        pull = self.call(g.a, "pull", {"remote_dir": f"{rb}/{self.rel}/hostb", "peer_role": "b"})
        v = pull.get("verified") or {}
        ok = bool(pull.get("ok") and v.get("ok"))
        reason = "" if ok else (pull.get("error") or "; ".join((v.get("problems") or [])[:3]) or "verify failed")
        sums_sha = None
        if ok:
            local = self.dir / "hostb" / SUMS_NAME
            sums_sha = sha256_file(local) if local.is_file() else None
            if not sums_sha or sums_sha != pull.get("sums_sha256"):
                ok = False
                reason = (f"hostb/{SUMS_NAME} on A hashes to {str(sums_sha)[:12]}, the pull verified "
                          f"{str(pull.get('sums_sha256'))[:12]}")
        storage["pull"] = {"ok": ok, "at": utc_iso(), "files": v.get("files"), "bytes": v.get("bytes"),
                           "sums_sha256": sums_sha if ok else None, "reason": reason,
                           "problems": (v.get("problems") or [])[:20], "elapsed_s": pull.get("elapsed_s")}
        mb = (v.get("bytes") or 0) / 1e6
        self.step("pull_b", ok, f"A<-B {v.get('files')} files, {mb:.0f} MB, every file sha256+size verified"
                  if ok else f"{reason}; B's copy KEPT")
        if not ok:
            storage["purge"] = {"done": False, "reason": "hostb/ did not verify on A"}
            self.save()
            return False
        self.man["timeline"]["mirrored"] = utc_iso()
        if g.keep_b:
            storage["purge"] = {"done": False, "reason": "b_keep_after_pull: true (host.yaml)"}
            self.step("purge_b", True, "SKIPPED: B's copy KEPT (b_keep_after_pull: true); A's copy verified")
            return True
        pr = self.call(g.b, "purge", {"rel": self.rel, "sums_sha256": sums_sha})
        if pr.get("ok"):
            d = pr.get("deleted") or {}
            storage["purge"] = {"done": True, "at": utc_iso(), "files": d.get("files"), "bytes": d.get("bytes"),
                                "already_absent": bool(pr.get("already_absent")), "pruned": pr.get("pruned") or []}
            self.man["timeline"]["purged"] = utc_iso()
            self.step("purge_b", True, "B's copy was already gone" if pr.get("already_absent") else
                      f"B's copy PURGED: {d.get('files')} files, {(d.get('bytes') or 0) / 1e6:.0f} MB")
        else:
            storage["purge"] = {"done": False, "reason": pr.get("error") or "purge failed"}
            self.step("purge_b", False, f"B's copy KEPT: {pr.get('error')}")
        return True

    def teardown(self, why: str):
        """Stop what was started (recorded pids only), close captures so diag-log-off runs, then
        bring B's partial copy to A (Host B keeps nothing, even from a cell that did not run)."""
        g = self.g
        self.g.log(f"{self.label}: teardown ({why})")
        both(lambda: self.call(g.a, "stop", timeout=TIMEOUTS["stop"]),
             lambda: self.call(g.b, "stop", timeout=TIMEOUTS["stop"]))
        t = self.cell.span_s + 300
        both(lambda: self.call(g.a, "close", timeout=t), lambda: self.call(g.b, "close", timeout=t))
        kb = self.call(g.b, "checksums")
        if kb.get("ok"):
            self.collect()
        else:
            self.g.log(f"{self.label}: B's partial copy KEPT: checksums on B failed ({kb.get('error')})")


def _cap_summary(c) -> str:
    if not isinstance(c, dict):
        return ""
    return ", ".join(f"{k} {'alive' if v.get('alive') else 'DEAD'} {v.get('bytes')} B"
                     for k, v in c.items() if isinstance(v, dict) and "alive" in v)


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


def grid_status(grid_id: str, results_root: str | None = None) -> dict:
    d = Path(results_root or hostcfg.load()["results_root"]) / grid_id
    st = read_json(d / "state.json", {}) or {}
    rows = []
    for cdir in gridmod.find_cell_dirs(d):
        man = read_json(cdir / "manifest.json", {}) or {}
        row = {k: man.get(k) for k in ("index", "label", "kind", "combo", "repeat", "status", "status_reason")}
        row["rel_path"] = man.get("rel_path") or cdir.relative_to(d).as_posix()
        purge = (man.get("storage") or {}).get("purge") or {}
        row["b_copy"] = "purged" if purge.get("done") else ("kept" if purge else None)
        row["compressed"] = bool((man.get("timeline") or {}).get("compressed"))
        rows.append(row)
    rows.sort(key=lambda r: r.get("index") if r.get("index") is not None else 1 << 30)
    return {"grid_id": grid_id, "dir": str(d), "state": st, "cells": rows, "postproc": queue_status(d),
            "stop_requested": (d / "STOP").exists(), "paused": (d / "PAUSED").read_text().strip()
            if (d / "PAUSED").exists() else None}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="teleop.grid.orchestrator")
    sub = ap.add_subparsers(dest="cmd", required=True)
    w = sub.add_parser("postproc-worker", help="the grid's background post-processing worker (started by the "
                                               "orchestrator)")
    w.add_argument("--grid-dir", required=True)
    w.add_argument("--owner-pid", type=int)
    w.add_argument("--owner-ticks", default="")
    w.add_argument("--poll-s", type=float, default=1.0)
    a = ap.parse_args(argv)
    return worker_main(Path(a.grid_dir), owner_pid=a.owner_pid,
                       owner_ticks=int(a.owner_ticks) if str(a.owner_ticks).strip() else None, poll_s=a.poll_s)


if __name__ == "__main__":
    sys.exit(main())
