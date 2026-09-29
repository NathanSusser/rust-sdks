"""Captures for one cell on one host: packet headers (pcap), modem DIAG (dlf) and 1 Hz hop
counters (hops). Standard library only; runs on both hosts.

Process rules carried over from the shell this replaces:

* Everything is started DETACHED (setsid) with its pid, kernel start time and a command-line
  signature recorded in captures.json. Anything stopped later is stopped BY PID, after the
  pid's /proc cmdline and start time are checked against that record -- never by pattern.
  `pkill -f PATTERN` matched the calling shell and SIGINTed it mid-script (2026-09-14), and a
  bare-name match found the previous run's tcpdump carrying the same room name (2026-09-21).
* A pid is not evidence. "Armed" means a file created by THIS arm exists and is growing.
* A capability-carrying or sudo tcpdump cannot be signalled by its own uid. It is never
  signalled; it stops itself at its -G deadline.
"""
from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------- /proc helpers

CLK_TCK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100


def read_cmdline(pid: int) -> list[str]:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return []
    return [a.decode(errors="replace") for a in raw.split(b"\0") if a]


def read_comm(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/comm").read_text().strip()
    except OSError:
        return ""


def _stat_fields(pid: int) -> list[str]:
    try:
        s = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return []
    # comm may contain spaces and parens: split after the LAST ')'
    rest = s[s.rfind(")") + 2:].split()
    return rest   # rest[0] = state (field 3), rest[19] = starttime (field 22)


def start_ticks(pid: int) -> int | None:
    f = _stat_fields(pid)
    try:
        return int(f[19])
    except (IndexError, ValueError):
        return None


def ppid_of(pid: int) -> int | None:
    f = _stat_fields(pid)
    try:
        return int(f[1])
    except (IndexError, ValueError):
        return None


def pid_alive(pid: int | None) -> bool:
    """True if the pid exists and is not a zombie. EPERM (a root or setcap tcpdump) = alive."""
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    f = _stat_fields(pid)
    if f and f[0] in ("Z", "X"):
        return False
    return bool(f) or Path(f"/proc/{pid}").exists()


def own_lineage() -> set[int]:
    """This process and all its ancestors: never report or signal ourselves."""
    out, p = set(), os.getpid()
    while p and p not in out and p > 1:
        out.add(p)
        p = ppid_of(p) or 0
    return out


def list_procs(skip_self: bool = True) -> list[dict]:
    """[{pid, comm, argv}] for every visible process, minus our own lineage."""
    skip = own_lineage() if skip_self else set()
    out = []
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        pid = int(d)
        if pid in skip:
            continue
        comm = read_comm(pid)
        if not comm:
            continue
        out.append({"pid": pid, "comm": comm, "argv": read_cmdline(pid)})
    return out


def is_qcsuper(argv: list[str]) -> bool:
    """A Python interpreter running QCSuper -- not a shell whose text mentions it."""
    if len(argv) < 2:
        return False
    exe = os.path.basename(argv[0])
    return exe.startswith("python") and any("qcsuper" in os.path.basename(a) for a in argv[1:3])


def verify_pid(rec: dict) -> tuple[bool, str]:
    """Is the process recorded in `rec` still the SAME process? (pid + start time + argv)."""
    pid = rec.get("pid")
    if not pid or not pid_alive(pid):
        return False, "not running"
    st = start_ticks(pid)
    if rec.get("start_ticks") is not None and st is not None and st != rec["start_ticks"]:
        return False, f"pid {pid} was reused (start time differs)"
    argv = read_cmdline(pid)
    sig = rec.get("signature") or []
    if argv and sig and not any(all(tok in " ".join(argv) for tok in alt) for alt in sig):
        return False, f"pid {pid} cmdline does not match the record"
    return True, "same process"


def spawn_detached(argv: list[str], log_path: Path, *, env: dict | None = None, cwd: str | None = None) -> int:
    """setsid, stdin </dev/null, stdout+stderr appended to log_path, no inherited fds."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "ab") as log:
        p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                             start_new_session=True, close_fds=True, env=env, cwd=cwd)
    return p.pid


def signal_recorded(rec: dict, sig: int) -> tuple[bool, str]:
    ok, why = verify_pid(rec)
    if not ok:
        return False, why
    try:
        os.kill(rec["pid"], sig)
        return True, f"sent {signal.Signals(sig).name} to {rec['pid']}"
    except PermissionError:
        return False, f"pid {rec['pid']} cannot be signalled by this user (privileged capture)"
    except ProcessLookupError:
        return False, "exited"


def file_size(p) -> int | None:
    try:
        return os.stat(p).st_size
    except OSError:
        return None


def sha256_file(p: Path, bufsize: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        while chunk := f.read(bufsize):
            h.update(chunk)
    return h.hexdigest()


def write_json_atomic(path: Path, data) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, sort_keys=False, default=str)
        f.write("\n")
    os.replace(tmp, path)


def read_json(path: Path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


SUMS_NAME = "SHA256SUMS"


def write_checksums(host_dir: Path) -> dict:
    """`sha256  bytes  relative-path` for every file under host_dir, sorted by path."""
    host_dir = Path(host_dir)
    lines, total = [], 0
    for p in sorted(host_dir.rglob("*")):
        if not p.is_file() or p.name == SUMS_NAME or p.name.endswith(".tmp"):
            continue
        rel = p.relative_to(host_dir).as_posix()
        size = p.stat().st_size
        lines.append(f"{sha256_file(p)}  {size}  {rel}")
        total += size
    tmp = host_dir / (SUMS_NAME + ".tmp")
    tmp.write_text("\n".join(lines) + ("\n" if lines else ""))
    os.replace(tmp, host_dir / SUMS_NAME)
    return {"files": len(lines), "bytes": total, "path": str(host_dir / SUMS_NAME)}


def verify_checksums(host_dir: Path) -> dict:
    """Check every SHA256SUMS line; also flag files that exist but are not listed."""
    host_dir = Path(host_dir)
    sums = host_dir / SUMS_NAME
    if not sums.is_file():
        return {"ok": False, "problems": [f"{SUMS_NAME} missing"], "files": 0}
    problems, listed = [], set()
    for line in sums.read_text().splitlines():
        if not line.strip():
            continue
        try:
            digest, size, rel = line.split("  ", 2)
        except ValueError:
            problems.append(f"bad line: {line[:80]}")
            continue
        listed.add(rel)
        p = host_dir / rel
        if not p.is_file():
            problems.append(f"missing: {rel}")
            continue
        if p.stat().st_size != int(size):
            problems.append(f"size differs: {rel} ({p.stat().st_size} != {size})")
            continue
        if sha256_file(p) != digest:
            problems.append(f"sha256 differs: {rel}")
    for p in host_dir.rglob("*"):
        if p.is_file() and p.name != SUMS_NAME:
            rel = p.relative_to(host_dir).as_posix()
            if rel not in listed:
                problems.append(f"not in {SUMS_NAME}: {rel}")
    return {"ok": not problems, "problems": problems, "files": len(listed)}


# ---------------------------------------------------------------- Captures

KINDS = ("pcap", "dlf", "hops")


class Captures:
    """The three captures of one host for one cell. State lives in <host dir>/captures.json."""

    def __init__(self, cfg: dict, cell_dir, label: str):
        self.cfg = cfg
        self.cell_dir = Path(cell_dir)
        self.label = label
        self.role = cfg["role"]
        self.host_dir = self.cell_dir / ("hosta" if self.role == "a" else "hostb")
        self.state_path = self.host_dir / "captures.json"

    # -- state
    def load(self) -> dict:
        return read_json(self.state_path, {}) or {}

    def save(self, state: dict) -> None:
        self.host_dir.mkdir(parents=True, exist_ok=True)
        write_json_atomic(self.state_path, state)

    def _modules(self):
        from . import diag, hops, pcap  # noqa: PLC0415  (lazy: avoids a package import cycle)
        return {"pcap": pcap, "dlf": diag, "hops": hops}

    # -- arm
    def arm(self, span_s: int, *, verify_s: float = 30.0) -> dict:
        state = self.load()
        live = {k: v for k, v in state.items() if k in KINDS and verify_pid(v)[0]}
        if live:
            # Idempotent: a second arm of the same cell reports the first, never doubles it.
            st = self.status()
            st["already_armed"] = True
            return st
        if any(k in state for k in KINDS):
            raise RuntimeError(f"{self.state_path} already records captures for this cell (not alive); "
                               "a cell directory is never re-armed -- use a new label")
        self.host_dir.mkdir(parents=True, exist_ok=True)
        mods = self._modules()
        state = {"label": self.label, "role": self.role, "span_s": span_s, "armed_at": time.time()}
        errors = {}
        for kind in KINDS:
            try:
                state[kind] = mods[kind].start(self.cfg, self.host_dir, self.label, span_s)
            except Exception as e:  # noqa: BLE001 -- one capture failing must not hide the others
                state[kind] = {"path": None, "pid": None, "started": None, "closed": None, "bytes": None,
                               "error": str(e)}
                errors[kind] = str(e)
            self.save(state)
        # Positive verification: our file exists and grows. The DLF can take ~20 s to appear.
        # pcap: alive with its file present is all that can be asked before the publisher
        # starts -- an idle link may carry no UDP at all. Its growth is checked during the run.
        deadline = time.time() + verify_s
        st = {}
        while time.time() < deadline:
            st = self._probe(state, interval=2.0)
            if all(self._armed_ok(k, st[k]) for k in KINDS if state[k].get("pid")) or \
                    any(not st[k]["alive"] for k in KINDS if state[k].get("pid")):
                break
        armed = not errors and bool(st) and all(self._armed_ok(k, st[k]) for k in KINDS)
        state["armed"] = armed
        state["arm_errors"] = errors
        self.save(state)
        st.update({"armed": armed, "errors": errors})
        extra = mods["hops"].radio_check(state["hops"]) if state["hops"].get("path") else None
        if extra:
            st["hops"]["radio"] = extra
        return st

    @staticmethod
    def _armed_ok(kind: str, s: dict) -> bool:
        if kind == "pcap":
            return s["alive"] and s["bytes"] is not None
        return s["alive"] and s["growing"]

    # -- status
    def _probe(self, state: dict, interval: float = 2.0) -> dict:
        paths = {k: (state.get(k) or {}).get("path") for k in KINDS}
        a = {k: file_size(p) if p else None for k, p in paths.items()}
        time.sleep(interval)
        b = {k: file_size(p) if p else None for k, p in paths.items()}
        out = {}
        for k in KINDS:
            rec = state.get(k) or {}
            alive = verify_pid(rec)[0] if rec.get("pid") else False
            path = paths[k]
            # diag may have renamed to -PARTIAL; follow the supervisor's status file
            if k == "dlf" and rec.get("path"):
                ds = self._modules()["dlf"].read_status(rec)
                if ds.get("dlf") and ds["dlf"] != path:
                    path = ds["dlf"]
                    b[k] = file_size(path)
            out[k] = {"path": path, "alive": alive, "bytes": b[k],
                      "growing": (a[k] is not None and b[k] is not None and b[k] > a[k])}
        return out

    def status(self) -> dict:
        state = self.load()
        if not any(k in state for k in KINDS):
            return {"armed": False, "note": "no captures recorded for this cell"}
        st = self._probe(state)
        st["armed"] = state.get("armed", False)
        dl = state.get("dlf") or {}
        if dl.get("path"):
            st["dlf"]["supervisor"] = self._modules()["dlf"].read_status(dl)
        return st

    # -- close
    def close(self, *, pcap_wait_s: float | None = None) -> dict:
        """Stop in order: let the modem flush, stop DIAG (the supervisor runs diag-log-off),
        stop hops, then WAIT for tcpdump to reach its own -G deadline. Then wait until no
        file is still growing, and write captures.json final."""
        state = self.load()
        if not any(k in state for k in KINDS):
            return {"closed": False, "note": "nothing armed"}
        if state.get("closed_at"):
            return {"closed": True, "already_closed": True, "captures": {k: state.get(k) for k in KINDS},
                    "complete": state.get("complete", False)}
        mods = self._modules()
        time.sleep(3)   # let the last scheduler items flush after the stream stops
        notes = []
        for kind in ("dlf", "hops", "pcap"):
            rec = state.get(kind) or {}
            if not rec.get("pid"):
                continue
            try:
                res = mods[kind].stop(self.cfg, rec, wait_s=pcap_wait_s if kind == "pcap" else None)
            except Exception as e:  # noqa: BLE001
                res = {"stopped": False, "error": str(e)}
            rec.update(res)
            state[kind] = rec
            self.save(state)
        # Wait until nothing grows (two stats 2 s apart equal), bounded.
        for _ in range(15):
            st = self._probe(state, interval=2.0)
            if not any(v["growing"] for v in st.values()):
                break
        else:
            notes.append("a capture file was still growing after 30 s")
        now = time.time()
        complete = True
        for kind in KINDS:
            rec = state.get(kind) or {}
            if not rec.get("pid"):
                complete = False
                continue
            alive = verify_pid(rec)[0]
            path = rec.get("path")
            if kind == "dlf":
                ds = mods["dlf"].read_status(rec)
                path = ds.get("dlf") or path
                rec["path"] = path
                rec["partial"] = bool(ds.get("partial"))
                rec["log_off"] = ds.get("log_off")
                rec["crc_dropped"] = ds.get("crc_dropped")
                rec["unmatched"] = ds.get("unmatched")
                if rec["partial"] or not ds.get("log_off", {}).get("ok", False):
                    complete = False
            rec["bytes"] = file_size(path) if path else None
            rec["closed"] = now if not alive else None
            rec["still_running"] = alive
            if alive or not rec["bytes"]:
                complete = False
            state[kind] = rec
        state["closed_at"] = now
        state["complete"] = complete
        state["close_notes"] = notes
        self.save(state)
        return {"closed": True, "complete": complete, "notes": notes,
                "captures": {k: state.get(k) for k in KINDS}}

    def stop_all(self) -> list[str]:
        """Operator stop: signal only recorded, verified pids. tcpdump is left to its deadline."""
        state = self.load()
        mods = self._modules()
        actions = []
        for kind in ("dlf", "hops", "pcap"):
            rec = state.get(kind) or {}
            if rec.get("pid"):
                try:
                    res = mods[kind].stop(self.cfg, rec, wait_s=0 if kind == "pcap" else None)
                    actions.append(f"{kind}: {res}")
                except Exception as e:  # noqa: BLE001
                    actions.append(f"{kind}: {e}")
        return actions

    def live_pids(self) -> list[int]:
        state = self.load()
        return [state[k]["pid"] for k in KINDS if (state.get(k) or {}).get("pid") and verify_pid(state[k])[0]]

    def write_checksums(self) -> dict:
        live = self.live_pids()
        if live:
            raise RuntimeError(f"captures still running (pids {live}); refusing to checksum a file being written")
        return write_checksums(self.host_dir)


def python_exe() -> str:
    return sys.executable or "python3"


# The checkout THIS code runs from: the cwd for every `python -m teleop.grid...` we spawn, so a
# detached recorder always imports the same code as the agent that started it.
CODE_ROOT = str(Path(__file__).resolve().parents[3])
