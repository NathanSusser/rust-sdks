"""Modem DIAG capture (DLF) through qcsuper-noroot-fast2. Standard library only.

Port of capture-around-cell.sh (A) and hostb-diag-capture/capture.sh (B). A detached
SUPERVISOR (`python3 -m teleop.grid.capture.diag supervise`) owns the capture for its whole
life; the agent that armed it exits. Every trap those scripts found is kept:

 1. Stdin at EOF stops QCSuper after ~4 s with no error. The supervisor holds qcsuper's
    stdin open as a pipe it never writes (the `sleep infinity |` of the shell).
 2. Nothing is stopped by pattern. The supervisor signals the qcsuper pid it started;
    the agent signals the supervisor pid it recorded, after verifying its cmdline.
 3. QCSuper's own shutdown loses the log-mask-off reply under load and leaves the modem
    streaming MB/s into every later run -- and fast2 makes on_deinit a no-op anyway. The
    supervisor ALWAYS runs diag-log-off in a `finally`, however it exits, and records the
    result. A capture whose log-off failed is not complete.
 4. No `wait` on a pipeline job (the shell's deadlock): the supervisor polls its one child.
 5. One reader per DIAG port: the supervisor takes the SAME flock the legacy tools take
    (~/diag-capture/.ttyUSB0.lock where that directory exists, i.e. Host B; otherwise beside
    the archived scripts), checks the tty with fuser, and refuses if any Python process is
    running QCSuper. A previous recorder holding the port cost A a whole cell's modem log.
 6. QCSuper prints the whole bad frame on every CRC failure (277 MB of log for a 53 MB DLF).
    Keep the event, drop the hexdump.
 7. An early exit is relaunched for the remaining window (appending: qcsuper opens the DLF
    'ab'), at most 3 attempts; a DLF that was restarted or died mid-cell is RENAMED
    <label>-PARTIAL.dlf, because a filename survives where a stderr warning does not.
 8. `grep -c` prints 0 AND exits 1 on no match (`|| echo 0` gave "0\\n0"): counts here are
    plain Python counts.
 9. A DLF with our name that already exists is refused: qcsuper appends, so a stale file
    would silently become the first part of ours.
"""
from __future__ import annotations

import argparse
import base64
import fcntl
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

if __package__ in (None, ""):   # pragma: no cover - `python3 path/to/diag.py`
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    __package__ = "teleop.grid.capture"

from . import (CODE_ROOT, file_size, is_qcsuper, list_procs, python_exe, read_json, spawn_detached, start_ticks,  # noqa: E402
               verify_pid, write_json_atomic)

QCSUPER_REL = "teleop/tools/diag/qcsuper-noroot-fast2"
LOGOFF_REL = "teleop/tools/diag/diag-log-off"
FIRST_BYTES_S = 20
MAX_ATTEMPTS = 3
CRC_RE = re.compile(r"(Wrong CRC).*")
UNM_RE = re.compile(r"(unmatched response received: [0-9]+).*")


def tools(cfg: dict) -> tuple[str, str]:
    repo = Path(cfg["repo"])
    return str(repo / QCSUPER_REL), str(repo / LOGOFF_REL)


def lock_path(cfg: dict) -> Path:
    """The lock the legacy tools take, so old and new can never both read the port."""
    if os.environ.get("DIAG_LOCK"):
        return Path(os.environ["DIAG_LOCK"])
    name = f".{os.path.basename(cfg['diag_tty'])}.lock"
    home = Path.home() / "diag-capture"
    if home.is_dir():
        return home / name
    return Path(cfg["repo"]) / "archive" / "diag-capture" / name


def lock_free(cfg: dict) -> tuple[bool, str]:
    """TEST the lock (take and release). Note this reserves nothing -- the supervisor takes it."""
    p = lock_path(cfg)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a") as f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return False, f"lock {p} is held by another capture"
            fcntl.flock(f, fcntl.LOCK_UN)
    except OSError as e:
        return False, f"cannot open lock {p}: {e}"
    return True, f"lock {p} free"


def tty_holders(tty: str) -> tuple[bool, str]:
    """(held, detail) via fuser; rc 0 = someone has it open."""
    try:
        r = subprocess.run(["fuser", tty], capture_output=True, text=True, timeout=10)
    except FileNotFoundError:
        return False, "fuser not installed; port holder unchecked"
    except subprocess.TimeoutExpired:
        return True, "fuser timed out"
    if r.returncode == 0:
        return True, f"{tty} held by pid(s) {(r.stdout + ' ' + r.stderr).split(':')[-1].strip()}"
    return False, f"{tty} not held"


def running_qcsuper() -> list[dict]:
    return [p for p in list_procs() if is_qcsuper(p["argv"])]


def dlf_path(host_dir: Path, label: str) -> Path:
    return Path(host_dir) / f"{label}.dlf"


def status_path(host_dir: Path, label: str) -> Path:
    return Path(host_dir) / f"{label}.diag.json"


def preconditions(cfg: dict) -> list[str]:
    problems = []
    tty = cfg["diag_tty"]
    if not (os.access(tty, os.R_OK) and os.access(tty, os.W_OK)):
        problems.append(f"cannot open {tty} (needs 'dialout' membership)")
    py = cfg["diag_venv_python"]
    q, lo = tools(cfg)
    for f in (py, q, lo):
        if not Path(f).exists():
            problems.append(f"missing {f}")
    ok, why = lock_free(cfg)
    if not ok:
        problems.append(why)
    held, why = tty_holders(tty)
    if held:
        problems.append(f"another process already holds {tty}; two diag clients corrupt each other ({why})")
    qs = running_qcsuper()
    if qs:
        problems.append("a qcsuper process is already running: " + ", ".join(str(p["pid"]) for p in qs))
    return problems


def start(cfg: dict, host_dir: Path, label: str, span_s: int) -> dict:
    problems = preconditions(cfg)
    if not problems:
        try:
            r = subprocess.run([cfg["diag_venv_python"], "-c", "import qcsuper"], capture_output=True, timeout=20)
            if r.returncode != 0:
                problems.append(f"qcsuper not importable from {cfg['diag_venv_python']}")
        except (OSError, subprocess.TimeoutExpired) as e:
            problems.append(f"diag venv python failed: {e}")
    dlf = dlf_path(host_dir, label)
    for p in (dlf, dlf.with_name(f"{label}-PARTIAL.dlf")):
        if p.exists():
            problems.append(f"{p.name} already exists; qcsuper appends, refusing to extend a stale capture")
    if problems:
        raise RuntimeError("; ".join(problems))
    args = {"cfg": cfg, "host_dir": str(host_dir), "label": label, "span_s": int(span_s)}
    b64 = base64.b64encode(json.dumps(args).encode()).decode()
    argv = [python_exe(), "-m", "teleop.grid.capture.diag", "supervise", "--label", label, "--json", b64]
    log = Path(host_dir) / f"{label}.diag-supervisor.log"
    started = time.time()
    pid = spawn_detached(argv, log, cwd=CODE_ROOT)
    return {
        "path": str(dlf), "pid": pid, "start_ticks": start_ticks(pid), "started": started,
        "closed": None, "bytes": None, "span_s": int(span_s), "deadline": started + int(span_s),
        "qcsuper_log": str(Path(host_dir) / f"{label}.qcsuper.log"),
        "status": str(status_path(host_dir, label)), "supervisor_log": str(log),
        "signature": [["teleop.grid.capture.diag", "supervise", label]],
    }


def read_status(rec: dict) -> dict:
    return read_json(Path(rec["status"]), {}) if rec.get("status") else {}


def stop(cfg: dict, rec: dict, wait_s: float | None = None) -> dict:
    """Ask the supervisor to finish: stop file + SIGTERM, then wait for it to exit (it runs
    diag-log-off first, which takes up to ~10 s, longer under load)."""
    Path(rec["status"]).with_suffix(".stop").touch()
    ok, why = verify_pid(rec)
    if ok:
        try:
            os.kill(rec["pid"], signal.SIGTERM)
        except OSError:
            pass
    end = time.time() + (wait_s if wait_s is not None else 120)
    while time.time() < end and verify_pid(rec)[0]:
        time.sleep(0.5)
    alive = verify_pid(rec)[0]
    st = read_status(rec)
    return {"stopped": not alive, "supervisor": st.get("state"),
            "note": "supervisor still running after wait" if alive else ""}


# ---------------------------------------------------------------- the supervisor process

class Supervisor:
    def __init__(self, args: dict):
        self.cfg = args["cfg"]
        self.host_dir = Path(args["host_dir"])
        self.label = args["label"]
        self.span_s = int(args["span_s"])
        self.dlf = dlf_path(self.host_dir, self.label)
        self.qlog = self.host_dir / f"{self.label}.qcsuper.log"
        self.status_file = status_path(self.host_dir, self.label)
        self.stop_file = self.status_file.with_suffix(".stop")
        self.stopping = False
        self.child: subprocess.Popen | None = None
        self.st = {"state": "starting", "supervisor_pid": os.getpid(), "dlf": str(self.dlf), "attempts": 0,
                   "early_exits": 0, "partial": False, "qcsuper_pids": [], "log_off": None,
                   "started": time.time(), "closed": None, "crc_dropped": None, "unmatched": None, "events": []}

    def save(self):
        write_json_atomic(self.status_file, self.st)

    def event(self, msg: str):
        line = f"{time.strftime('%H:%M:%S')} {msg}"
        self.st["events"].append(line)
        print(line, flush=True)
        self.save()

    def on_signal(self, signum, frame):  # noqa: ARG002
        self.stopping = True

    def _pump(self, stream):
        with open(self.qlog, "a") as out:
            for raw in iter(stream.readline, b""):
                line = raw.decode(errors="replace")
                line = CRC_RE.sub(r"\1", line)
                line = UNM_RE.sub(r"\1", line)
                out.write(line if line.endswith("\n") else line + "\n")
                out.flush()

    def launch(self):
        py = self.cfg["diag_venv_python"]
        q, _ = tools(self.cfg)
        env = dict(os.environ, PYTHONUNBUFFERED="1")
        env.pop("DISPLAY", None)
        self.child = subprocess.Popen(
            [py, q, "--usb-modem", self.cfg["diag_tty"], "--dlf-dump", str(self.dlf)],
            stdin=subprocess.PIPE,              # held open, never written: trap 1
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, close_fds=True, env=env)
        self.st["attempts"] += 1
        self.st["qcsuper_pids"].append(self.child.pid)
        threading.Thread(target=self._pump, args=(self.child.stdout,), daemon=True).start()
        self.event(f"qcsuper attempt {self.st['attempts']} pid {self.child.pid}")

    def stop_child(self):
        c = self.child
        if c is None or c.poll() is not None:
            return
        c.send_signal(signal.SIGINT)        # SIGINT so the DLF is flushed and closed
        for _ in range(20):
            if c.poll() is not None:
                break
            time.sleep(0.5)
        if c.poll() is None:
            c.terminate()
            try:
                c.wait(10)
            except subprocess.TimeoutExpired:
                c.kill()
                c.wait(5)
        try:
            c.stdin.close()
        except OSError:
            pass

    def log_off(self) -> dict:
        _, lo = tools(self.cfg)
        env = dict(os.environ, DIAG_LOCK_HELD="1")   # we hold the lock; do not deadlock against it
        try:
            r = subprocess.run([self.cfg["diag_venv_python"], lo, self.cfg["diag_tty"]], capture_output=True,
                               text=True, timeout=60, env=env, start_new_session=True)
            res = {"ok": r.returncode == 0, "rc": r.returncode, "output": (r.stdout + r.stderr).strip()[-400:]}
        except (OSError, subprocess.TimeoutExpired) as e:
            res = {"ok": False, "rc": None, "output": str(e)}
        if not res["ok"]:
            self.event("WARNING: modem diag logging may STILL be streaming; run diag-log-off by hand")
        return res

    def run(self) -> int:
        signal.signal(signal.SIGTERM, self.on_signal)
        signal.signal(signal.SIGINT, self.on_signal)
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
        signal.signal(signal.SIGPIPE, signal.SIG_IGN)
        self.save()
        lockf = open(lock_path(self.cfg), "a")  # noqa: SIM115 -- held for the process lifetime
        try:
            fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.st["state"] = "refused"
            self.event(f"another capture holds the DIAG port (lock {lock_path(self.cfg)}); not starting")
            return 1
        start = time.time()
        deadline = start + self.span_s
        rc = 0
        try:
            self.launch()
            # First bytes within 20 s, and still alive 3 s later, or the capture is not running.
            t0 = time.time()
            while time.time() - t0 < FIRST_BYTES_S and not (file_size(self.dlf) or 0) > 0:
                if self.child.poll() is not None or self.stopping:
                    break
                time.sleep(0.5)
            if not (file_size(self.dlf) or 0) > 0:
                self.st["state"] = "failed"
                self.event("qcsuper wrote nothing (exited or 20 s passed)")
                return 1
            time.sleep(3)
            if self.child.poll() is not None:
                self.event("qcsuper started then DIED within 3 s")
            else:
                self.st["state"] = "live"
                self.event(f"capture live ({file_size(self.dlf)} bytes and growing)")
            while True:
                if self.stopping or self.stop_file.exists():
                    self.event("stop requested")
                    break
                if time.time() >= deadline:
                    self.event("span reached")
                    break
                if self.child.poll() is not None:
                    left = deadline - time.time()
                    self.st["early_exits"] += 1
                    self.event(f"WARNING: qcsuper exited early (rc {self.child.returncode}), {left:.0f}s of window left")
                    if self.st["attempts"] >= MAX_ATTEMPTS or left < 20:
                        self.event("giving up; DLF covers only part of the window")
                        break
                    self.log_off()
                    time.sleep(2)
                    if self.stopping or self.stop_file.exists():
                        break
                    self.launch()
                time.sleep(0.5)
        finally:
            self.stop_child()
            time.sleep(1)   # let the log pump drain
            self.st["log_off"] = self.log_off()
            if self.st["early_exits"] and self.dlf.exists() and self.dlf.stat().st_size > 0:
                partial = self.dlf.with_name(f"{self.label}-PARTIAL.dlf")
                if not partial.exists():
                    os.rename(self.dlf, partial)
                    self.st["dlf"] = str(partial)
                    self.st["partial"] = True
                    self.event(f"INCOMPLETE CAPTURE: renamed to {partial.name}")
            try:
                text = self.qlog.read_text(errors="replace")
            except OSError:
                text = ""
            self.st["crc_dropped"] = text.count("Wrong CRC")
            self.st["unmatched"] = text.count("unmatched response received")
            self.st["closed"] = time.time()
            if self.st["state"] == "live":
                self.st["state"] = "closed-partial" if self.st["partial"] else "closed"
            self.st["bytes"] = file_size(self.st["dlf"])
            self.save()
            lockf.close()
            try:
                self.stop_file.unlink()
            except OSError:
                pass
            if self.st["partial"] or not (self.st["log_off"] or {}).get("ok"):
                rc = 3
        return rc


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="teleop.grid.capture.diag")
    ap.add_argument("cmd", choices=["supervise"])
    ap.add_argument("--label", required=True)
    ap.add_argument("--json", required=True)
    a = ap.parse_args(argv)
    args = json.loads(base64.b64decode(a.json))
    if args.get("label") != a.label:
        print("label mismatch", file=sys.stderr)
        return 2
    return Supervisor(args).run()


if __name__ == "__main__":
    sys.exit(main())
