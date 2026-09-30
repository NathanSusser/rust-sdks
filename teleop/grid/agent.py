"""The per-host agent: one command per call, one JSON object on stdout. Standard library + PyYAML.

    python3 -m teleop.grid.agent <cmd> [--cell-dir DIR] [--label LABEL] [--json <base64 json>]

cmds: identity | preflight | arm | status | publish | close | checksums | stop | pull

It acts only on its own machine. Long-running processes (captures, publisher, subscriber) are
started detached with their pid, kernel start time and a cmdline signature recorded in
captures.json / run.json; `stop` signals only those, after verifying them.

CREDENTIALS. The agent never reads the credentials file. The detached launcher has bash
source it (`set -a; . file`) IMMEDIATELY before exec'ing the harness or subscriber, checks the
two keys are non-empty without printing them, and execs. Nothing secret is in any argv, in
any file written here, or in what is printed.

Rules from the shell this replaces, kept:
* A cell started late is not the cell that was scheduled: publish refuses an epoch in the past.
* Credentials are checked BEFORE the epoch (a cell once died 4 ms after firing on an unsourced
  .env while the partner sat in an empty room).
* The SFU comes from TELEOP_SFU_HOST only -- no fallback to LIVEKIT_URL (a same-named room on a
  different deployment joins silently and receives nothing). The URL and clip are written into
  the log FROM THE VARIABLES PASSED TO argv, and read back from the harness's own record at close.
* Every inherited LK_* variable is removed before the cell's own are applied, AFTER sourcing
  credentials too, so "target_quality off" really is unset.
"""
from __future__ import annotations

import argparse
import base64
import collections
import datetime as _dt
import hashlib
import io
import json
import os
import platform
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

from . import capture, hostcfg
from .capture import Captures, read_json, spawn_detached, start_ticks, verify_pid, write_json_atomic

TELEOP_DIR = Path(__file__).resolve().parent.parent
LIVENESS_S = 25

LAUNCH_SH = r'''set -a; . "$0" >/dev/null 2>&1; set +a
if [ -z "${LIVEKIT_API_KEY:-}" ] || [ -z "${LIVEKIT_API_SECRET:-}" ]; then
  echo "agent: LIVEKIT_API_KEY/SECRET not set after sourcing credentials_env" >&2; exit 97; fi
for v in $(compgen -e); do case "$v" in LK_*|RUST_LOG|WAYLAND_DISPLAY) unset "$v";; esac; done
exec env "$@"'''

CHECK_CREDS_SH = r'''set -a; . "$0" >/dev/null 2>&1; set +a
[ -n "${LIVEKIT_API_KEY:-}" ] && [ -n "${LIVEKIT_API_SECRET:-}" ]'''


class AgentError(RuntimeError):
    pass


def utc_iso(t: float) -> str:
    return _dt.datetime.fromtimestamp(t, _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def sha256_or_none(p: Path) -> str | None:
    return capture.sha256_file(p) if p.is_file() else None


def package_sha256(teleop_dir: Path = TELEOP_DIR) -> str:
    """Hash of the control plane's own code and shared config (not grid files, which may be
    written on A only)."""
    h = hashlib.sha256()
    files = sorted(list((teleop_dir / "grid").rglob("*.py")) + list((teleop_dir / "config").glob("*.yaml")))
    for f in files:
        if "__pycache__" in f.parts or "tests" in f.relative_to(teleop_dir).parts:
            continue
        h.update(f.relative_to(teleop_dir).as_posix().encode() + b"\0")
        h.update(f.read_bytes())
    return h.hexdigest()


# ---------------------------------------------------------------- context

class Agent:
    def __init__(self, cfg: dict, cell_dir: str | None, label: str | None):
        self.cfg = cfg
        self.role = cfg["role"]
        self.repo = Path(cfg["repo"])
        self.label = label
        self.cell_dir = None
        if cell_dir:
            p = Path(cell_dir).expanduser()
            self.cell_dir = p if p.is_absolute() else Path(cfg["results_root"]) / p
        self.host_dir = self.cell_dir / hostcfg.host_dir_name(self.role) if self.cell_dir else None

    def need_cell(self):
        if not self.cell_dir or not self.label:
            raise AgentError("--cell-dir and --label are required for this command")
        if self.cell_dir.name != self.label:
            raise AgentError(f"cell dir {self.cell_dir.name} does not match label {self.label}")
        self.host_dir.mkdir(parents=True, exist_ok=True)

    @property
    def run_json(self) -> Path:
        return self.host_dir / "run.json"

    @property
    def process_json(self) -> Path:
        """The agent's own record of the publisher/subscriber it started. Separate from
        run.json, which the harness owns and rewrites atomically (--run-json)."""
        return self.host_dir / "process.json"

    # ------------------------------------------------------------ identity
    def identity(self, args: dict) -> dict:
        def git(where: Path, *a):
            try:
                r = subprocess.run(["git", "-C", str(where), *a], capture_output=True, text=True, timeout=10)
                return r.stdout.strip() if r.returncode == 0 else None
            except (OSError, subprocess.TimeoutExpired):
                return None
        import yaml  # noqa: PLC0415
        rel = self.repo / "target" / "release"
        # `commit` identifies the code this agent is running -- the checkout this
        # module lives in, which on Host B may be a worktree separate from
        # cfg["repo"], where the subscriber binary and credentials live.
        return {
            "ok": True, "role": self.role,
            "commit": git(TELEOP_DIR, "rev-parse", "HEAD"),
            "binaries_repo_commit": git(self.repo, "rev-parse", "HEAD"),
            "teleop_dirty": bool(git(TELEOP_DIR, "status", "--porcelain", "--", str(TELEOP_DIR))),
            "harness_sha256": sha256_or_none(rel / "teleop-harness"),
            "subscriber_sha256": sha256_or_none(rel / "subscriber"),
            "requirements_sha256": sha256_or_none(TELEOP_DIR / "requirements.txt"),
            "package_sha256": package_sha256(),
            "python": platform.python_version(), "pyyaml": yaml.__version__,
            "results_root": self.cfg["results_root"], "repo": str(self.repo),
        }

    # ------------------------------------------------------------ preflight
    def preflight(self, args: dict) -> dict:
        from . import preflight  # noqa: PLC0415
        gates = preflight.run(self.cfg, args.get("cell"), self.role, expect=args.get("expect"),
                              prev_clock=args.get("prev_clock"))
        return {"ok": all(g["pass"] for g in gates), "gates": gates}

    # ------------------------------------------------------------ arm
    def arm(self, args: dict) -> dict:
        from . import clock  # noqa: PLC0415
        self.need_cell()
        span = int(args["span_s"])
        cj = self.host_dir / "clock.json"
        if not cj.exists():
            write_json_atomic(cj, clock.measure())
        st = Captures(self.cfg, self.cell_dir, self.label).arm(span)
        return {"ok": bool(st.get("armed")), "captures": st, "clock": read_json(cj)}

    # ------------------------------------------------------------ status
    def _process(self) -> dict:
        """process.json (written by the agent) + fired.json (written only by the launcher, so
        neither can overwrite the other's fields)."""
        proc = read_json(self.process_json, {}) or {}
        fired = read_json(self.host_dir / "fired.json", {}) or {}
        if fired.get("fired_at") is not None:
            proc["fired_at"] = fired["fired_at"]
        return proc

    def status(self, args: dict) -> dict:
        self.need_cell()
        caps = Captures(self.cfg, self.cell_dir, self.label).status()
        proc = self._process()
        alive = verify_pid(proc)[0] if proc.get("pid") else False
        out = {"ok": True, "captures": caps, "process": {"alive": alive, "pid": proc.get("pid"),
                                                          "fired_at": proc.get("fired_at")}}
        if self.role == "a":
            jl = self.host_dir / f"{self.label}.jsonl"
            out["process"]["jsonl_bytes"] = capture.file_size(jl)
            out["process"]["exit"] = self._exit_note()
        else:
            csv = self.host_dir / "subscriber.csv"
            out["process"]["csv_bytes"] = capture.file_size(csv)
        return out

    def _exit_note(self) -> str | None:
        log = self.host_dir / f"{self.label}.log"
        try:
            tail = log.read_text(errors="replace").splitlines()[-5:]
        except OSError:
            return None
        return " | ".join(tail)[-300:]

    # ------------------------------------------------------------ publish
    def _check_credentials(self):
        cred = self.cfg["credentials_env"]
        if not Path(cred).is_file():
            raise AgentError("credentials_env does not exist")
        r = subprocess.run(["bash", "-c", CHECK_CREDS_SH, cred], capture_output=True, timeout=10)
        if r.returncode != 0:
            raise AgentError("LIVEKIT_API_KEY/SECRET not set after sourcing credentials_env; refusing "
                             "BEFORE the epoch rather than 4 ms after it")

    @staticmethod
    def _base_env() -> dict:
        return {k: v for k, v in os.environ.items()
                if not (k.startswith("LK_") or k in ("RUST_LOG", "WAYLAND_DISPLAY", "LIVEKIT_API_KEY",
                                                      "LIVEKIT_API_SECRET", "LIVEKIT_URL"))}

    def publish(self, args: dict) -> dict:
        self.need_cell()
        proc = self._process()
        if proc.get("pid"):
            ok, why = verify_pid(proc)
            if ok:
                return {"ok": True, "already_started": True, "process": proc}
            raise AgentError(f"this cell already published once (pid {proc['pid']}, {why}); "
                             "a label is never reused")
        self._check_credentials()
        sfu = hostcfg.sfu_host()
        url = f"wss://{sfu}"
        return self._publish_a(args, url) if self.role == "a" else self._publish_b(args, url)

    def _publish_a(self, args: dict, url: str) -> dict:
        epoch = int(args["epoch"])
        now = time.time()
        if epoch <= now + 1:
            raise AgentError(f"epoch {epoch} is {now - epoch:.1f}s in the PAST (or <1 s away); "
                             "a cell started late is not the cell that was scheduled")
        harness = self.repo / "target" / "release" / "teleop-harness"
        if not os.access(harness, os.X_OK):
            raise AgentError(f"{harness} missing; cargo build --release")
        hargs = list(args["harness_args"])
        henv = dict(args["harness_env"])
        if any(a in hargs for a in ("--url", "--room-name", "--snapshots-out", "--frame-csv-out", "--api-key",
                                    "--api-secret")):
            raise AgentError("harness_args must not carry url/room/outputs/credentials; the agent adds them")
        log = self.host_dir / f"{self.label}.log"
        argv = [str(harness), "--url", url, "--room-name", self.label, *hargs,
                "--snapshots-out", str(self.host_dir / f"{self.label}.jsonl"),
                "--frame-csv-out", str(self.host_dir / self.label)]
        if harness_supports(harness, "--run-json"):
            argv += ["--run-json", str(self.run_json)]
        clip = hargs[hargs.index("--camera-source") + 1] if "--camera-source" in hargs else ""

        def av(flag):
            return hargs[hargs.index(flag) + 1] if flag in hargs else "?"
        g = args.get("geometry") or {}
        kbps = int(av("--max-bitrate")) // 1000 if av("--max-bitrate").isdigit() else "?"
        fps = av("--fps")
        frame_kb = f"{kbps / 8 / int(fps):.2f}" if isinstance(kbps, int) and fps.isdigit() else "?"
        with open(log, "a") as f:
            # Header read back from the SAME variables passed to argv, never literals.
            f.write(f"geometry: {av('--width')}x{av('--height')}@{fps} target {frame_kb} kB/frame "
                    f"(cap {kbps}k / 8 / {fps} fps, bpp {g.get('bpp', '?')})\n")
            f.write(f"gcc-overrides: LK_PIN_BITRATE_TO_MAX={henv.get('LK_PIN_BITRATE_TO_MAX')} "
                    f"LK_MAX_START_BITRATE_KBPS={henv.get('LK_MAX_START_BITRATE_KBPS')} degradation={av('--degradation')}\n")
            f.write("encoder-env: " + " ".join(f"{k}={v}" for k, v in sorted(henv.items())) + "\n")
            f.write(f"source: clip={clip} cap={kbps}k codec={av('--codec')} duration={av('--duration-s')}s\n")
            f.write(f"sfu: url={url}\n")
            f.write(f"at-epoch: now {time.strftime('%H:%M:%S', time.gmtime(now))}, waiting {epoch - now:.0f}s "
                    f"until {time.strftime('%H:%M:%S', time.gmtime(epoch))} UTC\n")
        launch = {"epoch": epoch, "cred": self.cfg["credentials_env"],
                  "argv": [f"{k}={v}" for k, v in sorted(henv.items())] + argv,
                  "fired_json": str(self.host_dir / "fired.json"), "log": str(log), "label": self.label}
        b64 = base64.b64encode(json.dumps(launch).encode()).decode()
        largv = [sys.executable, "-m", "teleop.grid.agent", "_launch", "--label", self.label, "--json", b64]
        proc = {"pid": None, "kind": "publisher", "epoch": epoch, "fired_at": None, "argv": argv, "env": henv,
                "signature": [["teleop.grid.agent", "_launch", self.label], ["--room-name", self.label]]}
        pid = spawn_detached(largv, log, env=self._base_env(), cwd=capture.CODE_ROOT)
        proc.update(pid=pid, start_ticks=start_ticks(pid), spawned_at=time.time())
        write_json_atomic(self.process_json, proc)
        return {"ok": True, "process": proc}

    def _publish_b(self, args: dict, url: str) -> dict:
        sub = self.repo / "target" / "release" / "subscriber"
        if not os.access(sub, os.X_OK):
            raise AgentError(f"{sub} missing; cargo build --release -p local_video --features desktop")
        csv = self.host_dir / "subscriber.csv"
        if csv.exists():
            raise AgentError("subscriber.csv already exists for this cell")
        envs, argv = subscriber_command(sub, url, self.label, self.role, self.host_dir, self.cfg["display"], args)
        base = self._base_env()
        ca = self.repo / ".livekit-demo" / "corp-ca.pem"
        if "SSL_CERT_FILE" not in base and ca.is_file():
            envs["SSL_CERT_FILE"] = str(ca)   # the SFU is on an internal CA: UnknownIssuer otherwise
        log = self.host_dir / "subscriber.log"
        launch = {"epoch": None, "cred": self.cfg["credentials_env"],
                  "argv": [f"{k}={v}" for k, v in sorted(envs.items())] + argv,
                  "fired_json": str(self.host_dir / "fired.json"), "log": str(log), "label": self.label}
        b64 = base64.b64encode(json.dumps(launch).encode()).decode()
        largv = [sys.executable, "-m", "teleop.grid.agent", "_launch", "--label", self.label, "--json", b64]
        proc = {"pid": None, "kind": "subscriber", "fired_at": None, "argv": argv,
                "signature": [["teleop.grid.agent", "_launch", self.label], ["--room-name", self.label]]}
        pid = spawn_detached(largv, log, env=base, cwd=capture.CODE_ROOT)
        proc.update(pid=pid, start_ticks=start_ticks(pid), spawned_at=time.time())
        write_json_atomic(self.process_json, proc)
        # Positive evidence: still alive a few seconds later (a bad CA dies in ~1 s).
        time.sleep(4)
        alive = verify_pid(proc)[0]
        return {"ok": alive, "process": proc, "alive": alive,
                **({} if alive else {"error": "subscriber exited within 4 s", "log_tail": self._tail(log)})}

    @staticmethod
    def _tail(p: Path, n=8) -> str:
        try:
            return "\n".join(p.read_text(errors="replace").splitlines()[-n:])
        except OSError:
            return ""

    # ------------------------------------------------------------ close
    def _terminate(self, proc: dict, grace_int=10.0, grace_term=5.0) -> str:
        ok, why = verify_pid(proc)
        if not ok:
            return why
        for sig, grace in ((signal.SIGINT, grace_int), (signal.SIGTERM, grace_term), (signal.SIGKILL, 3)):
            capture.signal_recorded(proc, sig)
            end = time.time() + grace
            while time.time() < end and verify_pid(proc)[0]:
                time.sleep(0.25)
            if not verify_pid(proc)[0]:
                return f"stopped with {sig.name}"
        return "still running after SIGKILL"

    def close(self, args: dict) -> dict:
        self.need_cell()
        proc = self._process()
        stopped = None
        if proc.get("pid"):
            if self.role == "b":
                # The subscriber exits by itself 10 s after the publisher goes quiet.
                end = time.time() + float(args.get("subscriber_wait_s", 20))
                while time.time() < end and verify_pid(proc)[0]:
                    time.sleep(0.5)
            stopped = self._terminate(proc)
        caps = Captures(self.cfg, self.cell_dir, self.label).close()
        run = self._write_run_json() if self.role == "a" else None
        complete = bool(caps.get("complete"))
        return {"ok": True, "complete": complete, "process_stop": stopped, "captures": caps, "run": run}

    def _write_run_json(self) -> dict:
        """run.json is the harness's. The agent only fills it when the harness did not (older
        binary) or left fields null, never overwriting a value the harness wrote. The derived
        cross-checks (codec/clip match, stale room, cap line) go into process.json."""
        proc = self._process()
        derived = derive_run_json(self.host_dir, self.label, proc)
        harness = read_json(self.run_json, None)
        if harness is None:
            data = {k: derived[k] for k in RUN_JSON_KEYS} | {"source": "agent"}
        else:
            data = dict(harness)
            filled = [k for k in RUN_JSON_KEYS if data.get(k) is None and derived.get(k) is not None]
            for k in filled:
                data[k] = derived[k]
            if filled:
                data["filled_by_agent"] = filled
        if data != harness:
            write_json_atomic(self.run_json, data)
        rec = read_json(self.process_json, {}) or {}
        rec["checks"] = derived
        write_json_atomic(self.process_json, rec)
        return data | {"checks": derived}

    # ------------------------------------------------------------ checksums / stop / pull
    def checksums(self, args: dict) -> dict:
        self.need_cell()
        proc = self._process()
        if proc.get("pid") and verify_pid(proc)[0]:
            raise AgentError("publisher/subscriber still running; close first")
        return {"ok": True, **Captures(self.cfg, self.cell_dir, self.label).write_checksums()}

    def stop(self, args: dict) -> dict:
        self.need_cell()
        actions = []
        proc = self._process()
        if proc.get("pid"):
            actions.append(f"{proc.get('kind')}: {self._terminate(proc, 5, 5)}")
        actions += Captures(self.cfg, self.cell_dir, self.label).stop_all()
        return {"ok": True, "actions": actions}

    def pull(self, args: dict) -> dict:
        self.need_cell()
        remote = args["remote_dir"].rstrip("/")
        peer_role = args.get("peer_role") or ("b" if self.role == "a" else "a")
        local = self.cell_dir / hostcfg.host_dir_name(peer_role)
        if local == self.host_dir:
            raise AgentError("refusing to pull into this host's own directory")
        local.mkdir(parents=True, exist_ok=True)
        ssh = "ssh -o BatchMode=yes -o ConnectTimeout=10"
        argv = ["rsync", "-a", "--partial", "--exclude=*.tmp", "-e", ssh, f"{self.cfg['peer']}:{remote}/",
                f"{local}/"]
        try:
            r = subprocess.run(argv, capture_output=True, text=True, timeout=float(args.get("timeout_s", 1500)))
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "rsync timed out"}
        if r.returncode != 0:
            return {"ok": False, "error": f"rsync rc {r.returncode}: {r.stderr.strip()[-300:]}"}
        v = capture.verify_checksums(local)
        return {"ok": v["ok"], "verified": v, "dir": str(local)}


# ---------------------------------------------------------------- the subscriber's command line

def subscriber_command(sub: Path, url: str, label: str, role: str, host_dir: Path, display: str,
                       args: dict) -> tuple[dict, list[str]]:
    """(env, argv) for B's subscriber. The decoder's per-frame log (LK_DECODER_FRAME_LOG,
    hostb/frames-qp.csv) is ALWAYS requested: a subscriber built before it existed ignores the
    variable, and reduce treats the file as optional. Frame sampling (raw I420 into
    hostb/frames/, every sample_every-th frame ID) only when the cell asks for screenshots --
    at ~3 MB a frame it is not free on disk or in the mirror."""
    env = {"DISPLAY": display, "RUST_LOG": "info",
           "LK_DECODER_FRAME_LOG": str(Path(host_dir) / "frames-qp.csv")}
    argv = [str(sub), "--url", url, "--room-name", label, "--identity", f"host-{role}-{label}",
            "--low-latency", "--display-timestamp", "--log-csv", str(Path(host_dir) / "subscriber.csv")]
    shots = int(args.get("screenshots") or 0)
    if shots > 0:
        every = args.get("sample_every")
        if not every:
            # an orchestrator older than sample_every: derive it the way grid.sample_every does
            fps, dur = args.get("fps"), args.get("duration_s")
            if not (fps and dur):
                raise AgentError("screenshots > 0 but neither sample_every nor fps/duration_s given")
            every = max(1, int(fps) * int(dur) // shots)
        argv += ["--sample-frames-dir", str(Path(host_dir) / "frames"), "--sample-every", str(int(every))]
    return env, argv


# ---------------------------------------------------------------- run.json from what ran

RUN_JSON_KEYS = ("encoder_implementation", "width", "height", "fps", "max_bitrate_bps", "codec", "started_at")
CAP_RE = re.compile(r"NVENC (\w+) frame-size cap:.*")
STALE_RE = re.compile(r"already has (\d+) participant\(s\)")
GEOM_RE = re.compile(r"^geometry: (\d+)x(\d+)@(\d+)")


def derive_run_json(host_dir: Path, label: str, proc: dict) -> dict:
    """Fill run.json from the harness's snapshots and log, until the harness writes it itself."""
    jl = host_dir / f"{label}.jsonl"
    log = host_dir / f"{label}.log"
    meta, dims, impls, mimes, fps_vals = {}, collections.Counter(), collections.Counter(), collections.Counter(), []
    try:
        with open(jl) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if "record" in d:
                    meta = d
                vo = d.get("video_out") or {}
                if vo.get("frame_width") and vo.get("frame_height"):
                    dims[(vo["frame_width"], vo["frame_height"])] += 1
                if vo.get("encoder_implementation"):
                    impls[vo["encoder_implementation"]] += 1
                if vo.get("codec_mime_type"):
                    mimes[vo["codec_mime_type"]] += 1
                if vo.get("frames_per_second"):
                    fps_vals.append(float(vo["frames_per_second"]))
    except OSError:
        pass
    text = ""
    try:
        text = log.read_text(errors="replace")
    except OSError:
        pass
    argv = proc.get("argv") or []

    def arg(flag, cast=str):
        try:
            return cast(argv[argv.index(flag) + 1])
        except (ValueError, IndexError):
            return None
    cap = CAP_RE.search(text)
    stale = STALE_RE.search(text)
    impl = meta.get("encoder_implementation") or (impls.most_common(1)[0][0] if impls else None)
    w, h = dims.most_common(1)[0][0] if dims else (None, None)
    neg_codec = meta.get("negotiated_codec") or (mimes.most_common(1)[0][0].split("/")[-1].lower() if mimes else None)
    clip = arg("--camera-source")
    return {
        "source": "agent",
        "encoder_implementation": impl,
        "encoder_tier": meta.get("encoder_tier"),
        "width": w, "height": h,
        "requested_width": arg("--width", int), "requested_height": arg("--height", int),
        "fps": arg("--fps", int),
        "fps_delivered_median": sorted(fps_vals)[len(fps_vals) // 2] if fps_vals else None,
        "max_bitrate_bps": arg("--max-bitrate", int),
        "codec": neg_codec or arg("--codec"),
        "requested_codec": meta.get("requested_codec") or arg("--codec"),
        "codec_matches": (neg_codec == arg("--codec")) if neg_codec else None,
        "camera_source": meta.get("camera_source"),
        "clip_matches": (meta.get("camera_source") in (None, clip, os.path.basename(clip or ""))) if meta else None,
        "started_at": proc.get("fired_at"),
        "room": arg("--room-name"),
        "frame_size_cap_line": cap.group(0).strip() if cap else None,
        "stale_participants_at_join": int(stale.group(1)) if stale else 0,
        "snapshots": sum(dims.values()),
    }


def harness_supports(harness: Path, flag: str) -> bool:
    """Does THIS binary accept `flag`? (--help only: no network, no credentials.)"""
    try:
        r = subprocess.run([str(harness), "--help"], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return flag in r.stdout


# ---------------------------------------------------------------- the detached launcher

def launch(args: dict) -> int:
    """Wait for the epoch (if any), record the firing instant, then exec bash -> env -> binary.
    The pid is preserved across every exec, so the recorded pid stays valid."""
    epoch = args.get("epoch")
    if epoch is not None:
        now = time.time()
        if epoch <= now:
            print(f"at-epoch: target {epoch} is {now - epoch:.1f}s in the PAST; refusing to start late", flush=True)
            return 3
        while time.time() < epoch - 1:
            time.sleep(0.2)
        while time.time() < epoch:
            time.sleep(0.005)
    fired = time.time()
    print(f"at-epoch: firing at {utc_iso(fired)[11:23]} UTC (host clock)", flush=True)
    write_json_atomic(Path(args["fired_json"]), {"fired_at": fired, "epoch": epoch, "pid": os.getpid()})
    sys.stdout.flush()
    os.execvp("bash", ["bash", "-c", LAUNCH_SH, args["cred"], *args["argv"]])
    return 99  # not reached


# ---------------------------------------------------------------- CLI

COMMANDS = ("identity", "preflight", "arm", "status", "publish", "close", "checksums", "stop", "pull")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="teleop.grid.agent")
    ap.add_argument("cmd", choices=COMMANDS + ("_launch",))
    ap.add_argument("--cell-dir")
    ap.add_argument("--label")
    ap.add_argument("--json", default="")
    a = ap.parse_args(argv)
    try:
        args = json.loads(base64.b64decode(a.json)) if a.json else {}
    except ValueError as e:
        print(json.dumps({"ok": False, "cmd": a.cmd, "error": f"bad --json: {e}"}))
        return 1
    if a.cmd == "_launch":
        return launch(args)
    real_stdout = sys.stdout
    sys.stdout = sys.stderr          # exactly one JSON object reaches stdout
    out: dict
    try:
        cfg = hostcfg.load()
        agent = Agent(cfg, a.cell_dir or args.get("cell_dir"), a.label or args.get("label"))
        out = getattr(agent, a.cmd)(args)
        out.setdefault("ok", True)
    except Exception as e:  # noqa: BLE001 -- the orchestrator needs a JSON reason, not a traceback
        out = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    finally:
        sys.stdout = real_stdout
    out = {"cmd": a.cmd, "role": out.get("role") or _role_or_none(), "label": a.label, **out}
    buf = io.StringIO()
    json.dump(out, buf, default=str)
    print(buf.getvalue(), flush=True)
    return 0 if out.get("ok") else 1


def _role_or_none():
    try:
        return hostcfg.load()["role"]
    except Exception:  # noqa: BLE001
        return None


if __name__ == "__main__":
    sys.exit(main())
