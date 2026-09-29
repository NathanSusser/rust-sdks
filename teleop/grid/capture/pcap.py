"""Header-only packet capture on the modem interface. Standard library only.

Port of hop-recorder.sh (pcap part) and hostb-diag-capture/pcap.sh:

* 128-byte snaplen (IP+UDP+RTP+extensions, never media), nanosecond timestamps, UDP only,
  -U so the file is complete up to the last packet at every moment.
* THE CAPTURE STOPS ITSELF: -G <span> -W 1. A tcpdump run through sudo (A) or carrying
  file capabilities (B) cannot be signalled by the user that started it -- kill -KILL
  returns EPERM -- so it is never signalled. -G closes on the first packet AFTER the
  deadline, so on a quiet link the exit can lag by seconds; that is the flag working.
* No inherited fds: a tcpdump that inherited a lock fd held the lock until reboot
  (2026-09-17). spawn_detached closes every fd.
* A stale file with our name is refused, never overwritten.
"""
from __future__ import annotations

import getpass
import shlex
import shutil
import subprocess
import time
from pathlib import Path

from . import file_size, pid_alive, spawn_detached, start_ticks, verify_pid


def pcap_path(host_dir: Path, label: str, iface: str) -> Path:
    return Path(host_dir) / f"{label}.{iface}.pcap"


def tcpdump_cmd(cfg: dict) -> list[str]:
    return shlex.split(cfg["tcpdump"])


def can_capture(cfg: dict) -> tuple[bool, str]:
    """Two independent routes, and both must be tested: NOPASSWD sudo (A) or setcap (B).
    Testing only `sudo -n` reports a capability-carrying host as incapable."""
    cmd = tcpdump_cmd(cfg)
    if cmd and cmd[0] == "sudo":
        try:
            r = subprocess.run(cmd + ["--version"], capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired) as e:
            return False, f"{cfg['tcpdump']} --version failed: {e}"
        return (r.returncode == 0, "sudo route" if r.returncode == 0
                else f"`{cfg['tcpdump']}` not permitted without a password")
    exe = cmd[0] if cmd else "tcpdump"
    getcap = shutil.which("getcap") or "/usr/sbin/getcap"
    try:
        r = subprocess.run([getcap, exe], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, f"getcap failed: {e}"
    if "cap_net_raw" in r.stdout:
        return True, "setcap route"
    return False, f"{exe} has no cap_net_raw (sudo setcap cap_net_raw,cap_net_admin=eip {exe})"


def start(cfg: dict, host_dir: Path, label: str, span_s: int) -> dict:
    iface = cfg["wwan_iface"]
    if not Path(f"/sys/class/net/{iface}").exists():
        raise RuntimeError(f"no interface {iface}")
    ok, why = can_capture(cfg)
    if not ok:
        raise RuntimeError(why)
    out = pcap_path(host_dir, label, iface)
    if out.exists():
        raise RuntimeError(f"{out.name} already exists; refusing to overwrite a previous capture")
    cmd = tcpdump_cmd(cfg)
    argv = cmd + ["-i", iface, "-nn", "-s", "128", "--time-stamp-precision=nano", "-U",
                  "-G", str(int(span_s)), "-W", "1", "-w", str(out)]
    if cmd[0] == "sudo":
        argv += ["-Z", getpass.getuser()]   # drop to us, so the file is ours
    argv += ["udp"]
    log = Path(host_dir) / f"{label}.tcpdump.log"
    started = time.time()
    pid = spawn_detached(argv, log)
    return {
        "path": str(out), "pid": pid, "start_ticks": start_ticks(pid), "started": started,
        "closed": None, "bytes": None, "span_s": int(span_s), "deadline": started + int(span_s),
        "log": str(log), "argv": argv,
        "signature": [["tcpdump", str(out)]],
    }


def stop(cfg: dict, rec: dict, wait_s: float | None = None) -> dict:
    """Never signals. Waits for the -G deadline (+ margin for the first packet after it)."""
    if wait_s is None:
        wait_s = max(0.0, rec.get("deadline", time.time()) - time.time()) + 60
    end = time.time() + wait_s
    while time.time() < end and verify_pid(rec)[0]:
        time.sleep(1)
    alive = pid_alive(rec.get("pid")) and verify_pid(rec)[0]
    res = {"stopped": not alive, "bytes": file_size(rec.get("path"))}
    if alive:
        res["note"] = ("tcpdump still running; it exits at the first packet after its -G deadline "
                       "and cannot be signalled by this user")
    return res
