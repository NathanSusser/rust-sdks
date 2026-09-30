"""Control plane v2: layout paths through the agents, pull -> verify -> purge (one-way storage),
the control-log flags, the background post-processing queue, raw-capture compression and the
whole-grid disk gate. No network: agents are the real code behind a fake transport, rsync is a
local copy, the post-processing worker runs stand-in step modules.

    python3 -m unittest discover -s teleop/grid/tests -t .
"""
from __future__ import annotations

import base64
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from teleop.grid import agent as A  # noqa: E402
from teleop.grid import capture  # noqa: E402
from teleop.grid import cli  # noqa: E402
from teleop.grid import grid as G  # noqa: E402
from teleop.grid import orchestrator as O  # noqa: E402
from teleop.grid import preflight  # noqa: E402

REPO = Path(__file__).resolve().parents[3]
REAL_RUN = subprocess.run
REAL_SLEEP = time.sleep

BASE_CFG = {"peer": "peer.invalid", "wwan_iface": "wwan0", "ptp_iface": "eno2", "ptp_role": "slave",
            "modem_index": 0, "diag_tty": "/dev/ttyUSB0", "diag_venv_python": "/x/python",
            "credentials_env": "/x/.env", "tcpdump": "/usr/bin/tcpdump", "display": ":0"}


def cfg_for(role: str, root: Path, repo: Path = REPO, **kw) -> dict:
    return dict(BASE_CFG, role=role, results_root=str(root), repo=str(repo), peer_repo=str(repo),
                ptp_role="master" if role == "a" else "slave", b_keep_after_pull=False, **kw)


def agent_call(cfg: dict, cmd: str, args: dict, cell_rel: str | None, label: str | None) -> dict:
    """What `python -m teleop.grid.agent <cmd>` prints, without the process: one JSON object."""
    try:
        out = getattr(A.Agent(cfg, cell_rel, label), cmd)(args)
        out.setdefault("ok", True)
    except Exception as e:  # noqa: BLE001 -- the same wrapping as agent.main
        out = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    return json.loads(json.dumps({"cmd": cmd, "role": cfg["role"], "label": label, **out}, default=str))


def make_hostb(cell: Path, label: str, extra: dict | None = None) -> Path:
    """A closed hostb/ as B's agent leaves it: captures, CSVs, SHA256SUMS."""
    hb = cell / "hostb"
    hb.mkdir(parents=True, exist_ok=True)
    files = {f"{label}.wwan0.pcap": b"\xd4\xc3\xb2\xa1" + os.urandom(3000), f"{label}.dlf": os.urandom(5000),
             "subscriber.csv": b"frame_id,t\n" + b"".join(b"%d,%d\n" % (i, i) for i in range(900)),
             "frames-qp.csv": b"frame_id,qp\n1,30\n",
             "control.csv": b"seq,t_send_unix_us\n1,2\n", "captures.json": b"{}\n"}
    files.update(extra or {})
    for name, data in files.items():
        (hb / name).parent.mkdir(parents=True, exist_ok=True)
        (hb / name).write_bytes(data)
    capture.write_checksums(hb)
    return hb


LABEL = "tonight-c05-h265-1600x1300-30fps-b0040-2496k-v1-p1-r2"
REL = "tonight/h265-30fps-b0040/r2"


class Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="teleop-v2-test-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


# ---------------------------------------------------------------- agent: layout v2 cell directories
class CellDir(Tmp):
    def test_v2_and_control_and_legacy(self):
        A.check_cell_dir(self.tmp / REL, LABEL)
        A.check_cell_dir(self.tmp / "tonight/controls/x09", "tonight-x09-h264-1008x816-30fps-b0100-2500k-v1-p1-r2")
        A.check_cell_dir(self.tmp / "g0930a/cells" / "g0930a-c00-h264-x-r1", "g0930a-c00-h264-x-r1")

    def test_mismatches_refused(self):
        for d, label in ((self.tmp / "tonight/h265-30fps-b0040/r1", LABEL),          # r1 dir, r2 label
                         (self.tmp / "tonight/controls/x08", "tonight-x09-h264-a-r1"),  # another control
                         (self.tmp / "other/h265-30fps-b0040/r2", LABEL),             # another grid
                         (self.tmp / "tonight/h265-30fps-b0040/r2/hostb", LABEL),
                         (self.tmp / "tonight/r2", LABEL)):
            with self.assertRaises(A.AgentError, msg=str(d)):
                A.check_cell_dir(d, label)

    def test_need_cell_uses_the_relative_path(self):
        cfg = cfg_for("b", self.tmp)
        ag = A.Agent(cfg, REL, LABEL)
        ag.need_cell()
        self.assertTrue((self.tmp / REL / "hostb").is_dir())
        self.assertEqual(ag.grid_dir, self.tmp / "tonight")


# ---------------------------------------------------------------- agent: purge on B
class Purge(Tmp):
    def setUp(self):
        super().setUp()
        self.root = self.tmp / "runs"
        self.cfg = cfg_for("b", self.root)
        self.cell = self.root / REL
        self.hb = make_hostb(self.cell, LABEL)
        self.sums = capture.sha256_file(self.hb / capture.SUMS_NAME)

    def purge(self, rel=REL, sums=None, cfg=None, cell_rel="same"):
        return agent_call(cfg or self.cfg, "purge", {"rel": rel, "sums_sha256": self.sums if sums is None else sums},
                          rel if cell_rel == "same" else cell_rel, LABEL)

    def test_deletes_and_reports(self):
        nbytes = sum(p.stat().st_size for p in self.cell.rglob("*") if p.is_file())
        r = self.purge()
        self.assertTrue(r["ok"], r)
        self.assertFalse(self.cell.exists())
        d = r["deleted"]
        self.assertEqual(d["files"], 7)                                   # 6 files + SHA256SUMS
        self.assertIn(f"hostb/{LABEL}.dlf", d["listing"])
        self.assertIn("hostb/SHA256SUMS", d["listing"])
        self.assertEqual(d["bytes"], nbytes)
        self.assertEqual(r["pruned"], ["tonight/h265-30fps-b0040"])      # the emptied combo directory
        self.assertTrue((self.root / "tonight").is_dir())                # never the grid directory
        again = self.purge()
        self.assertTrue(again["ok"])
        self.assertTrue(again["already_absent"])

    def test_combo_kept_while_another_repeat_is_there(self):
        make_hostb(self.root / "tonight/h265-30fps-b0040/r3", LABEL[:-1] + "3")
        r = self.purge()
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["pruned"], [])
        self.assertTrue((self.root / "tonight/h265-30fps-b0040/r3/hostb").is_dir())

    def assert_refused(self, r, why):
        self.assertFalse(r["ok"], r)
        self.assertRegex(r["error"], why)
        self.assertTrue((self.hb / f"{LABEL}.dlf").is_file(), "B's copy must survive a refused purge")

    def test_refuses_outside_results_root(self):
        outside = self.tmp / "elsewhere" / "h265-30fps-b0040" / "r2"
        make_hostb(outside, LABEL)
        for rel in ("../elsewhere/h265-30fps-b0040/r2", "tonight/../../elsewhere/r2", str(outside),
                    "/etc/passwd/x/r2", "tonight/h265-30fps-b0040", "tonight/h265-30fps-b0040/r2/hostb",
                    "Bad-Id/h265-30fps-b0040/r2", "tonight/h265-30fps-b0040/hostb", "tonight/.hidden/r2", ""):
            self.assert_refused(self.purge(rel=rel, cell_rel=None), "purge: rel|must be")
        self.assertTrue((outside / "hostb" / f"{LABEL}.dlf").is_file())

    def test_refuses_a_symlink_out_of_the_grid(self):
        outside = self.tmp / "elsewhere"
        make_hostb(outside / "r2", LABEL)
        (self.root / "tonight" / "linked").symlink_to(outside)
        r = self.purge(rel="tonight/linked/r2", cell_rel=None)
        self.assertFalse(r["ok"])
        self.assertIn("symlink", r["error"])
        self.assertTrue((outside / "r2" / "hostb" / f"{LABEL}.dlf").is_file())

    def test_refuses_checksum_mismatch(self):
        self.assert_refused(self.purge(sums="0" * 64), "hashes to .*A verified 0000")
        self.assert_refused(self.purge(sums="not-a-sha"), "sums_sha256 missing or not a sha256")
        self.assert_refused(self.purge(sums=""), "sums_sha256 missing")

    def test_refuses_when_b_changed_after_the_pull(self):
        with open(self.hb / f"{LABEL}.wwan0.pcap", "ab") as f:      # the pcap grew after A's pull
            f.write(b"late packet")
        self.assert_refused(self.purge(), "size changed: hostb/.*pcap")

    def test_refuses_unlisted_and_outside_files(self):
        (self.hb / "late.csv").write_text("x\n")
        self.assert_refused(self.purge(), r"not in SHA256SUMS: hostb/late\.csv")
        (self.hb / "late.csv").unlink()
        (self.cell / "hosta").mkdir()
        (self.cell / "hosta" / "stray.json").write_text("{}")
        self.assert_refused(self.purge(), "outside hostb/, never pulled: hosta/stray.json")

    def test_tmp_files_do_not_block(self):
        (self.hb / "captures.json.tmp").write_text("{")
        self.assertTrue(self.purge()["ok"])

    def test_refuses_a_live_process(self):
        me = os.getpid()
        capture.write_json_atomic(self.hb / "process.json", {"pid": me, "start_ticks": capture.start_ticks(me),
                                                             "kind": "subscriber", "signature": [["python"]]})
        capture.write_checksums(self.hb)
        self.sums = capture.sha256_file(self.hb / capture.SUMS_NAME)
        self.assert_refused(self.purge(), "still running")

    def test_host_a_never_purges(self):
        self.assert_refused(self.purge(cfg=cfg_for("a", self.root)), "Host B only")

    def test_cell_dir_must_agree(self):
        self.assert_refused(self.purge(cell_rel="tonight/h265-30fps-b0040/r3"), "different directories")

    def test_cli_wiring(self):
        """`python -m teleop.grid.agent purge` exists, prints one JSON object, exits 1 on refusal."""
        import yaml
        hy = self.tmp / "host.yaml"
        hy.write_text(yaml.safe_dump({k: v for k, v in self.cfg.items() if k != "b_keep_after_pull"}))
        b64 = base64.b64encode(json.dumps({"rel": REL, "sums_sha256": "0" * 64}).encode()).decode()
        r = REAL_RUN([sys.executable, "-m", "teleop.grid.agent", "purge", "--cell-dir", REL, "--label", LABEL,
                      "--json", b64], cwd=REPO, env=dict(os.environ, TELEOP_HOST_YAML=str(hy)),
                     capture_output=True, text=True, timeout=60)
        lines = [ln for ln in r.stdout.splitlines() if ln.strip()]
        self.assertEqual(len(lines), 1, r.stdout + r.stderr)
        out = json.loads(lines[0])
        self.assertEqual((out["cmd"], out["ok"], r.returncode), ("purge", False, 1))
        self.assertTrue(self.hb.is_dir())


# ---------------------------------------------------------------- agent: pull verifies every file
def fake_rsync(tamper=None):
    """subprocess.run stand-in: `rsync ... peer:<src>/ <dst>/` becomes a local copy (tamper may
    corrupt the copy); anything else runs for real."""
    def run(argv, *a, **kw):
        if argv and argv[0] == "rsync":
            src = Path(argv[-2].split(":", 1)[1])
            dst = Path(argv[-1])
            if not src.is_dir():
                return subprocess.CompletedProcess(argv, 23, "", f"rsync: change_dir {src} failed: No such file")
            shutil.copytree(src, dst, dirs_exist_ok=True, ignore=shutil.ignore_patterns("*.tmp"))
            if tamper:
                tamper(dst)
            return subprocess.CompletedProcess(argv, 0, "", "")
        return REAL_RUN(argv, *a, **kw)
    return run


class Pull(Tmp):
    def setUp(self):
        super().setUp()
        self.b_root, self.a_root = self.tmp / "b", self.tmp / "a"
        make_hostb(self.b_root / REL, LABEL)

    def pull(self, tamper=None):
        with mock.patch.object(A.subprocess, "run", fake_rsync(tamper)):
            return agent_call(cfg_for("a", self.a_root), "pull",
                              {"remote_dir": f"{self.b_root}/{REL}/hostb", "peer_role": "b"}, REL, LABEL)

    def test_verified_pull_returns_the_checksum_file_identity(self):
        r = self.pull()
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["verified"]["files"], 6)
        self.assertGreater(r["verified"]["bytes"], 8000)
        self.assertEqual(r["sums_sha256"], capture.sha256_file(self.a_root / REL / "hostb" / "SHA256SUMS"))
        self.assertEqual(r["sums_sha256"], capture.sha256_file(self.b_root / REL / "hostb" / "SHA256SUMS"))

    def test_every_file_is_checked(self):
        def flip(d):     # same size, one byte different: only the sha256 can see it
            p = d / f"{LABEL}.dlf"
            b = bytearray(p.read_bytes())
            b[100] ^= 0xFF
            p.write_bytes(bytes(b))
        r = self.pull(flip)
        self.assertFalse(r["ok"])
        self.assertIn(f"sha256 differs: {LABEL}.dlf", r["verified"]["problems"])
        self.assertNotIn("sums_sha256", r)

        def truncate(d):
            p = d / "subscriber.csv"
            p.write_bytes(p.read_bytes()[:-3])
        r = self.pull(truncate)
        self.assertFalse(r["ok"])
        self.assertTrue(any(x.startswith("size differs: subscriber.csv") for x in r["verified"]["problems"]))
        self.assertNotIn("sums_sha256", r)


# ---------------------------------------------------------------- agent: control flags from --help
def fake_binary(path: Path, help_text: str) -> Path:
    """An executable whose --help prints help_text and counts its runs in <path>.runs."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\necho run >> '{path}.runs'\ncat <<'EOF'\n{help_text}\nEOF\n")
    path.chmod(0o755)
    return path


def runs(path: Path) -> int:
    try:
        return len(Path(f"{path}.runs").read_text().splitlines())
    except OSError:
        return 0


SUB_HELP_NEW = "Usage: subscriber [OPTIONS]\n      --control-log <PATH>\n      --control-buffer-frames <N>  [default: 64]"
SUB_HELP_LOG_ONLY = "Usage: subscriber [OPTIONS]\n      --control-log <PATH>"
SUB_HELP_OLD = "Usage: subscriber [OPTIONS]\n      --log-csv <PATH>\n      --sample-frames-dir <DIR>"


class ControlFlags(Tmp):
    def setUp(self):
        super().setUp()
        self.root = self.tmp / "runs"
        self.repo = self.tmp / "repo"

    def publish_b(self, help_text: str, args=None) -> tuple[dict, dict]:
        sub = fake_binary(self.repo / "target" / "release" / "subscriber", help_text)
        cfg = cfg_for("b", self.root, repo=self.repo)
        ag = A.Agent(cfg, REL, LABEL)
        ag.need_cell()
        fake_time = mock.Mock(time=time.time, sleep=lambda s: None, strftime=time.strftime, gmtime=time.gmtime)
        with mock.patch.object(A, "spawn_detached", return_value=os.getpid()), \
                mock.patch.object(A, "verify_pid", return_value=(True, "same process")), \
                mock.patch.object(A, "time", fake_time):
            out = ag._publish_b(args or {"duration_s": 180, "control_transport": "data_track_buf1",
                                         "control_rx_buffer": 64}, "wss://sfu.invalid")
        return out, capture.read_json(ag.process_json), sub

    def test_control_log_and_depth_when_help_lists_both(self):
        out, proc, _ = self.publish_b(SUB_HELP_NEW)
        argv = proc["argv"]
        hb = self.root / REL / "hostb"
        self.assertEqual(argv[argv.index("--control-log") + 1], str(hb / "control.csv"))
        self.assertEqual(argv[argv.index("--control-buffer-frames") + 1], "64")
        self.assertTrue(out["control_log"])
        self.assertEqual(out["control_buffer_frames"], 64)
        self.assertTrue(proc["control_log"])
        self.assertNotIn("--sample-frames-dir", argv)
        self.assertNotIn("--sample-every", argv)

    def test_depth_only_with_its_own_flag(self):
        out, proc, _ = self.publish_b(SUB_HELP_LOG_ONLY)
        self.assertIn("--control-log", proc["argv"])
        self.assertNotIn("--control-buffer-frames", proc["argv"])
        self.assertTrue(out["control_log"])
        self.assertIsNone(out["control_buffer_frames"])

    def test_no_control_log_on_an_older_subscriber(self):
        out, proc, _ = self.publish_b(SUB_HELP_OLD)
        self.assertNotIn("--control-log", proc["argv"])
        self.assertNotIn("--control-buffer-frames", proc["argv"])
        self.assertFalse(out["control_log"])
        self.assertIsNone(out["control_buffer_frames"])

    def test_decoder_frame_log_always(self):
        for help_text in (SUB_HELP_NEW, SUB_HELP_OLD):
            env, argv = A.subscriber_command(Path("/r/subscriber"), "wss://x", LABEL, "b", Path("/h/hostb"), ":0", {},
                                             control_log=help_text == SUB_HELP_NEW, control_buffer_frames=64)
            self.assertEqual(env["LK_DECODER_FRAME_LOG"], "/h/hostb/frames-qp.csv")
            self.assertEqual(argv[argv.index("--log-csv") + 1], "/h/hostb/subscriber.csv")
            self.assertEqual("--control-log" in argv, help_text == SUB_HELP_NEW)
            self.assertEqual("--control-buffer-frames" in argv, help_text == SUB_HELP_NEW)

    def test_help_is_cached_per_run_and_binary(self):
        sub = fake_binary(self.tmp / "bin" / "subscriber", SUB_HELP_NEW)
        cache = self.tmp / "grid"
        self.assertTrue(A.harness_supports(sub, "--control-log", cache))
        self.assertTrue(A.harness_supports(sub, "--control-buffer-frames", cache))
        self.assertFalse(A.harness_supports(sub, "--sample-every", cache))
        self.assertEqual(runs(sub), 1, "one --help per run, whatever the number of flags asked")
        self.assertTrue((cache / A.HELP_CACHE).is_file())
        # a rebuilt binary (new size/mtime) is asked again; a new grid (another cache dir) too
        fake_binary(sub, SUB_HELP_OLD)
        os.utime(sub, ns=(time.time_ns(), time.time_ns() + 5_000_000_000))
        self.assertFalse(A.harness_supports(sub, "--control-log", cache))
        self.assertTrue(A.harness_supports(sub, "--log-csv", self.tmp / "other-grid"))
        self.assertEqual(runs(sub), 3)

    def test_publisher_seq_log_and_transport(self):
        harness = fake_binary(self.repo / "target" / "release" / "teleop-harness", "--run-json <PATH>\n")
        cfg = cfg_for("a", self.root, repo=self.repo)
        label = LABEL
        ag = A.Agent(cfg, REL, label)
        ag.need_cell()
        cell = G.parse({"id": "tonight", "defaults": {"clip": "/x.mp4", "repeats": 1, "resolution": "1600x1300",
                                                      "bpp": 0.04, "control_transport": "dc_lossy"},
                        "axes": {"codec": ["h265"]}}).expand()[0]
        with mock.patch.object(A, "spawn_detached", return_value=os.getpid()):
            ag._publish_a({"epoch": int(time.time()) + 60, "harness_args": cell.harness_args(),
                           "harness_env": cell.harness_env()}, "wss://sfu.invalid")
        argv = capture.read_json(ag.process_json)["argv"]
        ha = self.root / REL / "hosta"
        self.assertEqual(argv[argv.index("--publisher-seq-log") + 1], str(ha / "control-pub.jsonl"))
        self.assertEqual(argv[argv.index("--control-transport") + 1], "dc_lossy")
        self.assertEqual(argv[argv.index("--run-json") + 1], str(ha / "run.json"))
        self.assertEqual(argv[argv.index("--room-name") + 1], label)
        self.assertEqual(runs(harness), 1)
        with self.assertRaisesRegex(A.AgentError, "must not carry"):
            ag._publish_a({"epoch": int(time.time()) + 60, "harness_args": ["--publisher-seq-log", "/x"],
                           "harness_env": {}}, "wss://sfu.invalid")


# ---------------------------------------------------------------- orchestrator with fake transport
class FakeTime:
    """The orchestrator's clock: sleep() advances it (and yields a moment of real time)."""

    def __init__(self, t0: float = 1_790_000_000.0):
        self.t = t0
        self.lock = threading.Lock()

    def time(self) -> float:
        with self.lock:
            return self.t

    def sleep(self, s: float) -> None:
        with self.lock:
            self.t += max(float(s), 0.001)
        REAL_SLEEP(0.001)


class Rig:
    """Host A and Host B: the real agent code for checksums/pull/purge on two local results
    roots, canned replies for everything that would need hardware. Records every call."""

    def __init__(self, tmp: Path, clock: FakeTime, *, bad_pull: set | None = None, after_pull=None,
                 control_log=True, on_purge=None, hostb_files: dict | None = None):
        self.root_a, self.root_b = tmp / "a-runs", tmp / "b-runs"
        self.cfg_a, self.cfg_b = cfg_for("a", self.root_a), cfg_for("b", self.root_b)
        self.clock = clock
        self.bad_pull = bad_pull or set()
        self.after_pull = after_pull
        self.on_purge = on_purge
        self.control_log = control_log
        self.hostb_files = hostb_files or {}       # label -> {name: bytes} B writes instead of the defaults
        self.calls: list[tuple] = []
        self.published: dict = {}
        self.lock = threading.Lock()

    def host(self, name: str):
        rig = self

        class H:
            def call(self, cmd, args=None, *, cell_rel=None, label=None, timeout=None):
                return rig.handle(name, cmd, dict(args or {}), cell_rel, label)
        return H()

    def cmds(self, host=None, cmd=None):
        return [c for c in self.calls if (host is None or c[0] == host) and (cmd is None or c[1] == cmd)]

    def handle(self, h, cmd, args, rel, label):
        with self.lock:
            self.calls.append((h, cmd, args, rel, label))
        root = self.root_a if h == "a" else self.root_b
        cfg = self.cfg_a if h == "a" else self.cfg_b
        now = self.clock.time()
        if cmd == "identity":
            return {"ok": True, "commit": "c0ffee", "harness_sha256": "h" if h == "a" else None,
                    "requirements_sha256": "r", "package_sha256": "p", "results_root": str(root)}
        if cmd == "preflight":
            return {"ok": True, "gates": [preflight.result("clock_offset", True, "ok",
                                                           clock={"host_minus_utc_s": -1.0})]}
        if cmd == "arm":
            hd = root / rel / ("hosta" if h == "a" else "hostb")
            hd.mkdir(parents=True, exist_ok=True)
            if h == "b":
                make_hostb(root / rel, label, self.hostb_files.get(label))
            else:
                (hd / f"{label}.wwan0.pcap").write_bytes(os.urandom(2000))
                (hd / f"{label}.dlf").write_bytes(os.urandom(4000))
            return {"ok": True, "captures": {}, "clock": {"host_minus_utc_s": -1.0}}
        if cmd == "publish" and h == "b":
            return {"ok": True, "process": {"pid": 4242}, "control_log": self.control_log,
                    "control_buffer_frames": args.get("control_rx_buffer") if self.control_log else None}
        if cmd == "publish":
            ha = args["harness_args"]
            epoch = int(args["epoch"])
            dur = int(ha[ha.index("--duration-s") + 1])
            self.published[label] = (epoch, dur)
            hd = root / rel / "hosta"
            capture.write_json_atomic(hd / "run.json", {
                "width": 1600, "height": 1300, "encoder_implementation": "NVENC", "fps": 30,
                "codec": ha[ha.index("--codec") + 1], "max_bitrate_bps": int(ha[ha.index("--max-bitrate") + 1])})
            capture.write_json_atomic(hd / "fired.json", {"fired_at": epoch + 0.004, "epoch": epoch, "pid": 1})
            capture.write_json_atomic(hd / "process.json", {"checks": {"codec_matches": True, "clip_matches": True,
                                                                        "stale_participants_at_join": 0}})
            return {"ok": True, "process": {"pid": 1}}
        if cmd == "status":
            if h == "a":
                epoch, dur = self.published[label]
                return {"ok": True, "captures": {}, "process": {"alive": now < epoch + dur,
                                                                "jsonl_bytes": 100 if now >= epoch else 0}}
            return {"ok": True, "captures": {}, "process": {"alive": True, "csv_bytes": 10}}
        if cmd == "close":
            return {"ok": True, "complete": True, "captures": {}}
        if cmd == "stop":
            return {"ok": True, "actions": []}
        if cmd == "checksums":
            return agent_call(cfg, "checksums", args, rel, label)
        if cmd == "pull":
            args = dict(args, remote_dir=args["remote_dir"])

            def tamper(d, label=label):
                if label in self.bad_pull:
                    (d / "subscriber.csv").write_bytes(b"torn")
            with mock.patch.object(A.subprocess, "run", fake_rsync(tamper)):
                r = agent_call(cfg, "pull", args, rel, label)
            if self.after_pull:
                self.after_pull(self, rel, label)
            return r
        if cmd == "purge":
            if self.on_purge:
                self.on_purge(self, rel, label)
            return agent_call(cfg, "purge", args, rel, label)
        return {"ok": False, "error": f"fake: no {cmd}"}


FAKEPOST = '''
"""Stand-in post-processing steps: each records start/end, its niceness and I/O class."""
import json, os, subprocess, time

def _io():
    try:
        return subprocess.run(["ionice", "-p", str(os.getpid())], capture_output=True, text=True).stdout.strip()
    except OSError:
        return None

def _rec(kind, arg):
    log = os.environ["FAKEPOST_LOG"]
    with open(log, "a") as f:
        f.write(json.dumps({"kind": kind, "arg": str(arg), "ev": "start", "t": time.time(), "pid": os.getpid(),
                            "nice": os.nice(0), "io": _io()}) + "\\n")
    time.sleep(float(os.environ.get("FAKEPOST_SLEEP", "0.03")))
    if kind in os.environ.get("FAKEPOST_FAIL", "").split(","):
        raise RuntimeError(f"fake {kind} failure")
    with open(log, "a") as f:
        f.write(json.dumps({"kind": kind, "arg": str(arg), "ev": "end", "t": time.time()}) + "\\n")

def reduce_cell(d): _rec("reduce", d)
def build(d): _rec("metrics", d)
def render_cell(d): _rec("report", d)
def render_combo(d): _rec("combo", d)
def render_grid(d): _rec("grid", d)
'''
FAKE_STEPS = [["reduced", "fakepost", "reduce_cell"], ["metrics", "fakepost", "build"],
              ["reported", "fakepost", "render_cell"]]


def grid_file(tmp: Path, gid="v2test", repeats=2, compress=False) -> Path:
    p = tmp / f"{gid}.yaml"
    p.write_text(textwrap.dedent(f"""\
        id: {gid}
        defaults: {{resolution: 1600x1300, fps: 30, bpp: 0.1, duration_s: 30, repeats: {repeats}, lead_s: 30,
                   cooldown_s: 10, clip: /nonexistent/clip.mp4}}
        axes: {{codec: [h265, av1]}}
        order: sequential
        compress_raw: {'true' if compress else 'false'}
        """))
    return p


class GridRunTest(Tmp):
    """GridRun end to end on the fake rig, with a REAL background worker process."""

    def setUp(self):
        super().setUp()
        import yaml
        self.clock = FakeTime()
        self.post_dir = self.tmp / "post"
        self.post_dir.mkdir()
        (self.post_dir / "fakepost.py").write_text(FAKEPOST)
        self.postlog = self.tmp / "post.jsonl"
        self.rig = Rig(self.tmp, self.clock)
        self.hy = self.tmp / "host.yaml"
        self.hy.write_text(yaml.safe_dump({k: v for k, v in self.rig.cfg_a.items() if k != "b_keep_after_pull"}))
        self.env = mock.patch.dict(os.environ, {"TELEOP_HOST_YAML": str(self.hy), "FAKEPOST_LOG": str(self.postlog),
                                                "PYTHONPATH": str(self.post_dir)})
        self.env.start()
        sys.path.insert(0, str(self.post_dir))
        self.patches = [mock.patch.object(O, "time", self.clock),
                        mock.patch.object(preflight, "_free_bytes", return_value=(10 ** 13, Path("/")))]
        for p in self.patches:
            p.start()
        self.lines: list[str] = []

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.env.stop()
        sys.path.remove(str(self.post_dir))
        sys.modules.pop("fakepost", None)
        for d in (self.rig.root_a,):
            for g in d.glob("*"):
                if (g / "postproc").is_dir():
                    (g / "postproc" / "CLOSE").write_text("test over\n")
        super().tearDown()

    def gridrun(self, path, resume=False, rig=None) -> O.GridRun:
        run = O.GridRun(path, resume=resume, out=self.lines.append)
        rig = rig or self.rig
        run.a, run.b = rig.host("a"), rig.host("b")
        run.post_steps = FAKE_STEPS
        run.combo_render = ["fakepost", "render_combo"]
        run.grid_render = ["fakepost", "render_grid"]
        run.worker_poll_s = 0.05
        return run

    def events(self) -> list[dict]:
        try:
            return [json.loads(x) for x in self.postlog.read_text().splitlines() if x.strip()]
        except OSError:
            return []

    def test_grid_layout_one_way_storage_and_background_queue(self):
        path = grid_file(self.tmp)
        run = self.gridrun(path)
        self.assertEqual(run.run(), "done")
        gdir = self.rig.root_a / "v2test"
        rels = ["h265/r1", "av1/r1", "h265/r2", "av1/r2"]
        # every agent call for a cell names <grid-id>/<combo>/r<n>
        cell_calls = [c for c in self.rig.calls if c[3]]
        self.assertEqual(sorted({c[3] for c in cell_calls}), sorted(f"v2test/{r}" for r in rels))
        for h, cmd, args, rel, label in cell_calls:
            self.assertTrue(label.endswith(rel.rsplit("/", 1)[1]), (rel, label))
        for rel in rels:
            man = capture.read_json(gdir / rel / "manifest.json")
            self.assertEqual(man["status"], "OK", man["status_reason"])
            self.assertEqual((man["rel_path"], man["combo"], man["repeat"]), (rel, rel.split("/")[0], int(rel[-1])))
            self.assertTrue(man["label"].startswith("v2test-c0"))
            self.assertEqual(man["variables"]["control_transport"], "data_track_buf1")
            self.assertEqual(man["variables"]["control_rx_buffer"], 64)
            self.assertIs(man["control_log_enabled"], True)
            self.assertEqual(man["control_rx_buffer_applied"], 64)
            st = man["storage"]
            self.assertTrue(st["pull"]["ok"])
            self.assertTrue(st["purge"]["done"])
            self.assertEqual(st["pull"]["sums_sha256"], capture.sha256_file(gdir / rel / "hostb" / "SHA256SUMS"))
            self.assertTrue(man["timeline"]["purged"] and man["timeline"]["reported"] and man["timeline"]["reduced"])
            self.assertTrue(man["integrity"]["mirror_verified"])
            self.assertTrue((gdir / rel / "hostb" / "subscriber.csv").is_file(), "A keeps everything")
            self.assertFalse((self.rig.root_b / "v2test" / rel).exists(), "B keeps nothing")
        self.assertEqual(sorted(p.name for p in (self.rig.root_b / "v2test").iterdir() if p.is_dir()), [])
        # purge was handed exactly the checksum file A verified, for the directory it pulled
        purges = self.rig.cmds("b", "purge")
        self.assertEqual([p[2]["rel"] for p in purges], [f"v2test/{r}" for r in rels])
        self.assertEqual([p[3] for p in purges], [p[2]["rel"] for p in purges])
        self.assertEqual(len(self.rig.cmds("b", "pull")), 0, "no A->B push any more")
        # the background worker: one job per cell, in run order, strictly one at a time, niced
        ev = self.events()
        reduces = [e["arg"] for e in ev if e["kind"] == "reduce" and e["ev"] == "start"]
        self.assertEqual(reduces, [str(gdir / r) for r in rels])
        spans = []
        for e in ev:
            if e["kind"] in ("reduce", "metrics", "report", "combo") and e["ev"] == "start":
                spans.append([e["t"], None])
            elif e["kind"] in ("reduce", "metrics", "report", "combo") and e["ev"] == "end":
                spans[-1][1] = e["t"]
        for (s0, e0), (s1, _) in zip(spans, spans[1:]):
            self.assertLessEqual(e0, s1, "post-processing steps overlapped")
        worker = [e for e in ev if e["ev"] == "start" and e["pid"] != os.getpid()]
        self.assertEqual(len(worker), 4 * 4 + 1, "reduce, metrics, report, combo per cell + 1 comparison")
        self.assertTrue(all(e["nice"] == 19 for e in worker), {e["nice"] for e in worker})
        if shutil.which("ionice"):
            self.assertTrue(all("idle" in (e["io"] or "") for e in worker), {e["io"] for e in worker})
        combos = [e["arg"] for e in ev if e["kind"] == "combo" and e["ev"] == "start"]
        self.assertEqual(combos, [str(gdir / r.split("/")[0]) for r in rels])
        # the final comparison: rendered by the orchestrator itself, after the queue drained
        grid_starts = [e for e in ev if e["kind"] == "grid" and e["ev"] == "start"]
        self.assertEqual(grid_starts[-1]["pid"], os.getpid())
        self.assertGreaterEqual(grid_starts[-1]["t"], max(e["t"] for e in ev if e["kind"] == "report"))
        q = O.queue_status(gdir)
        self.assertEqual((q["depth"], q["done"], q["failed"]), (0, 4, 0))
        log = (gdir / "grid.log").read_text()
        self.assertIn("B's copy PURGED", log)
        self.assertIn("[postproc]", log)
        self.assertIn("storage: Host A keeps everything; B's copy of each cell is PURGED", log)
        self.assertIn("final comparison", log)
        # grid status shows the layout and the queue
        st = O.grid_status("v2test", results_root=str(self.rig.root_a))
        self.assertEqual([r["rel_path"] for r in st["cells"]], rels)
        self.assertEqual([(r["combo"], r["repeat"]) for r in st["cells"]], [("h265", 1), ("av1", 1), ("h265", 2),
                                                                            ("av1", 2)])
        self.assertEqual({r["b_copy"] for r in st["cells"]}, {"purged"})
        self.assertEqual(st["postproc"]["depth"], 0)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli.cmd_grid_status(mock.Mock(grid="v2test", json=False))
        out = buf.getvalue()
        self.assertIn("post-processing queue depth 0", out)
        self.assertRegex(out, r"\n +2 h265 +r2 +OK +purged")

    def test_verify_failure_keeps_b_and_the_grid_goes_on(self):
        g = G.load(grid_file(self.tmp))
        bad = g.expand()[1].label                      # av1/r1
        rig = Rig(self.tmp, self.clock, bad_pull={bad})
        run = self.gridrun(grid_file(self.tmp), rig=rig)
        self.assertEqual(run.run(), "done")
        gdir = rig.root_a / "v2test"
        man = capture.read_json(gdir / "av1/r1/manifest.json")
        self.assertEqual(man["status"], "INCOMPLETE")
        self.assertIn("hostb/ not verified on A", man["status_reason"])
        self.assertIn("B's copy kept", man["status_reason"])
        self.assertFalse(man["storage"]["pull"]["ok"])
        self.assertFalse(man["storage"]["purge"]["done"])
        self.assertFalse(man["integrity"]["mirror_verified"])
        self.assertTrue((rig.root_b / "v2test/av1/r1/hostb/subscriber.csv").is_file(), "B's copy kept")
        self.assertNotIn("v2test/av1/r1", [p[2]["rel"] for p in rig.cmds("b", "purge")])
        for rel in ("h265/r1", "h265/r2", "av1/r2"):          # the grid continued
            self.assertEqual(capture.read_json(gdir / rel / "manifest.json")["status"], "OK")
            self.assertFalse((rig.root_b / "v2test" / rel).exists())
        self.assertIn("B's copy KEPT", (gdir / "grid.log").read_text())
        # an INCOMPLETE cell is still reduced (it ran)
        self.assertIn(str(gdir / "av1/r1"), [e["arg"] for e in self.events() if e["kind"] == "reduce"])

    def test_b_rendering_nothing_or_too_little_is_incomplete(self):
        """The H.265 smoke of 2026-09-30: A published cleanly, B decoded nothing, and it said OK."""
        g = G.load(grid_file(self.tmp, repeats=1))
        h265, av1 = (c.label for c in g.expand())
        head = b"sample,elapsed_ms,frame_id\n"
        rig = Rig(self.tmp, self.clock, hostb_files={
            h265: {"subscriber.csv": head},
            av1: {"subscriber.csv": head + b"".join(b"%d,0,%d\n" % (i, i) for i in range(100))}})
        run = self.gridrun(grid_file(self.tmp, repeats=1), rig=rig)
        self.assertEqual(run.run(), "done")
        gdir = rig.root_a / "v2test"
        man = capture.read_json(gdir / "h265/r1/manifest.json")
        self.assertEqual(man["status"], "INCOMPLETE")
        self.assertIn("B rendered no video frames (hostb/subscriber.csv has no rows)", man["status_reason"])
        self.assertEqual(man["frames_rendered_b"], {"rows": 0, "expected": 900})
        self.assertTrue(man["integrity"]["mirror_verified"], "storage still verified and purged")
        man = capture.read_json(gdir / "av1/r1/manifest.json")
        self.assertEqual(man["status"], "INCOMPLETE")
        self.assertIn("B rendered only 100 of ~900 frames", man["status_reason"])

    def test_rendered_frames_counts_data_rows(self):
        hb = self.tmp / "hb"
        hb.mkdir()
        self.assertIsNone(O.rendered_frames(hb))
        (hb / "subscriber.csv").write_text("a,b\n")
        self.assertEqual(O.rendered_frames(hb), 0)
        (hb / "subscriber.csv").write_text("a,b\n1,2\n3,4\n\n")
        self.assertEqual(O.rendered_frames(hb), 2)

    def test_refused_purge_keeps_b_but_not_the_status(self):
        def grow(rig, rel, label):        # B's pcap grows after A pulled it
            with open(rig.root_b / rel / "hostb" / f"{label}.wwan0.pcap", "ab") as f:
                f.write(b"late")
        rig = Rig(self.tmp, self.clock, after_pull=grow)
        run = self.gridrun(grid_file(self.tmp, repeats=1), rig=rig)
        self.assertEqual(run.run(), "done")
        man = capture.read_json(rig.root_a / "v2test/h265/r1/manifest.json")
        self.assertEqual(man["status"], "OK")
        self.assertFalse(man["storage"]["purge"]["done"])
        self.assertIn("size changed", man["storage"]["purge"]["reason"])
        self.assertTrue((rig.root_b / "v2test/h265/r1/hostb").is_dir())

    def test_b_keep_after_pull(self):
        import yaml
        self.hy.write_text(yaml.safe_dump({k: v for k, v in self.rig.cfg_a.items()} | {"b_keep_after_pull": True}))
        run = self.gridrun(grid_file(self.tmp, repeats=1))
        self.assertTrue(run.keep_b)
        self.assertEqual(run.run(), "done")
        self.assertEqual(self.rig.cmds("b", "purge"), [])
        man = capture.read_json(self.rig.root_a / "v2test/h265/r1/manifest.json")
        self.assertTrue(man["storage"]["pull"]["ok"])
        self.assertIn("b_keep_after_pull", man["storage"]["purge"]["reason"])
        self.assertTrue((self.rig.root_b / "v2test/h265/r1/hostb").is_dir())
        log = (self.rig.root_a / "v2test/grid.log").read_text()
        self.assertIn("KEPT after A verifies it (b_keep_after_pull: true)", log)
        self.assertIn("B's copy KEPT (b_keep_after_pull: true)", log)

    def test_resume_with_the_new_layout(self):
        path = grid_file(self.tmp)
        gdir = self.rig.root_a / "v2test"

        def stop_after_first(rig, rel, label):
            (gdir / "STOP").write_text("test\n")
        rig = Rig(self.tmp, self.clock, on_purge=stop_after_first)
        first = self.gridrun(path, rig=rig)
        self.assertEqual(first.run(), "stopped")
        self.assertEqual(capture.read_json(gdir / "h265/r1/manifest.json")["status"], "OK")
        self.assertFalse((gdir / "av1/r1").exists())
        deadline = time.time() + 30                         # the worker finishes its queue in the background
        while O.queue_status(gdir)["depth"] and time.time() < deadline:
            REAL_SLEEP(0.05)
        self.assertEqual(O.queue_status(gdir)["depth"], 0)
        first.post.proc.wait(timeout=30)                  # CLOSE was written: it exits once idle
        # a cell the orchestrator died in, and a finished cell whose report never happened
        (gdir / "av1/r1").mkdir(parents=True)
        capture.write_json_atomic(gdir / "av1/r1/manifest.json", {"status": "RUNNING", "label": "x"})
        man = capture.read_json(gdir / "h265/r1/manifest.json")
        man["timeline"]["reported"] = None
        capture.write_json_atomic(gdir / "h265/r1/manifest.json", man)
        with self.assertRaisesRegex(RuntimeError, "exists; use --resume"):
            O.GridRun(path, out=self.lines.append)
        run = self.gridrun(path, resume=True)
        self.assertEqual(run.run(), "done")
        self.assertEqual(capture.read_json(gdir / "av1/r1/manifest.json")["status"], "INCOMPLETE")
        for rel in ("h265/r2", "av1/r2"):
            self.assertEqual(capture.read_json(gdir / rel / "manifest.json")["status"], "OK")
        reduced = [e["arg"] for e in self.events() if e["kind"] == "reduce" and e["ev"] == "start"]
        self.assertEqual(reduced, [str(gdir / "h265/r1"), str(gdir / "h265/r1"), str(gdir / "h265/r2"),
                                   str(gdir / "av1/r2")], "h265/r1 re-queued once; the interrupted cell is not")
        self.assertIn("re-queued for post-processing", (gdir / "grid.log").read_text())

    def test_resume_refuses_the_old_layout(self):
        path = grid_file(self.tmp)
        (self.rig.root_a / "v2test" / "cells").mkdir(parents=True)
        with self.assertRaisesRegex(RuntimeError, "old cells/<label> layout"):
            O.GridRun(path, resume=True, out=self.lines.append)

    def test_disk_gate_refuses_before_anything_exists(self):
        with mock.patch.object(preflight, "_free_bytes", return_value=(21 * 10 ** 9, Path("/data"))):
            run = self.gridrun(grid_file(self.tmp, repeats=3))
            self.assertEqual(run.run(), "refused")
        self.assertFalse((self.rig.root_a / "v2test").exists())
        self.assertEqual(self.rig.calls, [], "nothing contacted either host")
        msg = "\n".join(self.lines)
        self.assertIn("REFUSING the grid: not enough disk", msg)
        self.assertIn("6 cells project", msg)
        self.assertIn("free 21.0 GB", msg)
        self.assertIn("fewer repeats", msg)
        self.assertIn("shorter lead_s", msg)
        self.assertIn("compress_raw: true", msg)


# ---------------------------------------------------------------- the queue and the worker, in process
class FakeModules:
    """importer stand-in: records (kind, arg, start, end), optionally failing some kinds."""

    def __init__(self, fail=(), sleep=0.01):
        self.events: list[tuple] = []
        self.fail = set(fail)
        self.sleep = sleep
        self.lock = threading.Lock()

    def __call__(self, name):
        fm = self

        def step(kind):
            def fn(arg):
                t0 = time.time()
                REAL_SLEEP(fm.sleep)
                with fm.lock:
                    fm.events.append((kind, str(arg), t0, time.time()))
                if kind in fm.fail:
                    raise RuntimeError(f"{kind} broke")
            return fn
        if name == "teleop.grid.report.combo" and "combo-absent" in self.fail:
            raise ModuleNotFoundError(f"No module named '{name}'", name=name)
        kind = {"teleop.grid.report.cell": "report", "teleop.grid.report.combo": "combo",
                "teleop.grid.report.grid": "grid"}.get(name, name)
        return mock.Mock(reduce_cell=step("reduce"), build=step("metrics"), render=step(kind))


class Queue(Tmp):
    def job(self, i, **kw):
        return {"rel_path": f"c{i}/r1", "label": f"g-c{i:02d}-x-r1", "combo": f"c{i}", "kind": "cell",
                "status": "OK", "cell_steps": True, "compress_raw": False, **kw}

    def run_worker(self, importer, **kw):
        t = threading.Thread(target=O.worker_main, args=(self.tmp,),
                             kwargs=dict(importer=importer, priority=lambda: ["test priority"], poll_s=0.01,
                                         owner_pid=os.getpid(), echo=False, **kw))
        t.start()
        return t

    def test_fifo_one_at_a_time_and_drain(self):
        q = O.PostQueue(self.tmp, lambda m: None, autostart=False)
        ids = [q.enqueue(self.job(i)) for i in range(4)]
        self.assertEqual(ids, ["000001", "000002", "000003", "000004"])
        self.assertEqual(O.queue_status(self.tmp)["depth"], 4)
        fm = FakeModules()
        t = self.run_worker(fm)
        q.close()
        t.join(30)
        self.assertFalse(t.is_alive(), "the worker exits once the queue is empty and closed")
        reduces = [e[1] for e in fm.events if e[0] == "reduce"]
        self.assertEqual(reduces, [str(self.tmp / f"c{i}/r1") for i in range(4)])
        for a, b in zip(fm.events, fm.events[1:]):
            self.assertLessEqual(a[3], b[2] + 1e-6, "two steps ran at once")
        done = [capture.read_json(p) for p in sorted((self.tmp / "postproc/done").glob("*.json"))]
        self.assertEqual([d["id"] for d in done], ids)
        # the comparison is re-rendered only when no other job waits (the last one here)
        self.assertEqual([d["result"]["renders"]["grid"] for d in done], ["deferred", "deferred", "deferred", True])
        self.assertTrue(all(d["result"]["renders"]["combo"] is True for d in done))
        st = O.queue_status(self.tmp)
        self.assertEqual((st["depth"], st["done"], st["failed"]), (0, 4, 0))
        self.assertIn("[postproc]", (self.tmp / "grid.log").read_text())

    def test_absent_combo_module_is_tolerated(self):
        q = O.PostQueue(self.tmp, lambda m: None, autostart=False)
        q.enqueue(self.job(1))
        q.close()
        fm = FakeModules(fail={"combo-absent"})
        self.run_worker(fm).join(30)
        done = capture.read_json(next((self.tmp / "postproc/done").glob("*.json")))
        self.assertEqual(done["result"]["renders"]["combo"], "absent")
        self.assertTrue(done["result"]["ok"])

    def test_failed_step_is_recorded_not_fatal(self):
        cdir = self.tmp / "c1/r1"
        cdir.mkdir(parents=True)
        capture.write_json_atomic(cdir / "manifest.json", {"timeline": {}})
        q = O.PostQueue(self.tmp, lambda m: None, autostart=False)
        q.enqueue(self.job(1))
        q.enqueue(self.job(2))
        q.close()
        self.run_worker(FakeModules(fail={"reduce"})).join(30)
        done = [capture.read_json(p) for p in sorted((self.tmp / "postproc/done").glob("*.json"))]
        self.assertEqual(len(done), 2)
        self.assertIn("RuntimeError: reduce broke", done[0]["result"]["steps"]["reduced"])
        self.assertIs(done[0]["result"]["steps"]["reported"], True)
        self.assertIsNone(capture.read_json(cdir / "manifest.json")["timeline"]["reduced"])
        self.assertIn("reduce broke", (cdir / "postprocess-errors.log").read_text())
        self.assertEqual(O.queue_status(self.tmp)["failed"], 2)

    def test_a_job_a_dead_worker_was_on(self):
        qp = O.queue_paths(self.tmp)
        qp["pending"].mkdir(parents=True)
        capture.write_json_atomic(qp["running"], self.job(1, id="000001", attempts=1))
        (qp["dir"] / "CLOSE").write_text("x")
        fm = FakeModules()
        self.run_worker(fm).join(30)
        self.assertEqual(len([e for e in fm.events if e[0] == "reduce"]), 1, "re-run once")
        capture.write_json_atomic(qp["running"], self.job(2, id="000002", attempts=2))
        fm = FakeModules()
        self.run_worker(fm).join(30)
        self.assertEqual(fm.events, [], "given up after a worker died on it twice")
        self.assertIn("died twice", capture.read_json(qp["done"] / "000002.json")["result"]["error"])

    def test_wait_for_a_control_cells_job(self):
        q = O.PostQueue(self.tmp, lambda m: None, autostart=False, poll_s=0.01)
        jid = q.enqueue(self.job(1))
        q.enqueue(self.job(2))
        t = self.run_worker(FakeModules())
        res = q.wait_for(jid, timeout=30)
        self.assertEqual(res["id"], jid)
        q.close()
        t.join(30)


# ---------------------------------------------------------------- compression
FAKE_ZSTD = r'''#!/usr/bin/env python3
"""zstd stand-in: `-3 -T2 -q IN -o OUT` writes a marked copy, `-t -q F` checks the mark."""
import os, sys
a = sys.argv[1:]
with open(os.environ["FAKE_ZSTD_LOG"], "a") as f:
    f.write(" ".join(a) + "\n")
if "-t" in a:
    data = open(a[-1], "rb").read()
    sys.exit(1 if os.environ.get("FAKE_ZSTD_TEST_FAIL") or not data.startswith(b"ZST") else 0)
src, dst = a[a.index("-o") - 1], a[a.index("-o") + 1]
if os.environ.get("FAKE_ZSTD_FAIL"):
    sys.exit(1)
data = open(src, "rb").read()
open(dst, "wb").write(b"ZST" + data[: len(data) // 2])
'''


class Compression(Tmp):
    def setUp(self):
        super().setUp()
        self.zstd = self.tmp / "bin" / "zstd"
        self.zstd.parent.mkdir()
        self.zstd.write_text(FAKE_ZSTD)
        self.zstd.chmod(0o755)
        self.zlog = self.tmp / "zstd.log"
        self.env = mock.patch.dict(os.environ, {"FAKE_ZSTD_LOG": str(self.zlog)})
        self.env.start()
        self.grid = self.tmp / "g"
        self.cell = self.grid / "h265/r1"
        make_hostb(self.cell, LABEL)
        ha = self.cell / "hosta"
        ha.mkdir()
        (ha / f"{LABEL}.wwan0.pcap").write_bytes(os.urandom(6000))
        (ha / f"{LABEL}-PARTIAL.dlf").write_bytes(os.urandom(9000))
        (ha / f"{LABEL}.jsonl").write_text("{}\n")
        capture.write_checksums(ha)
        capture.write_json_atomic(self.cell / "manifest.json", {"label": LABEL, "timeline": {}})
        self.sums_before = {h: (self.cell / h / "SHA256SUMS").read_text() for h in ("hosta", "hostb")}

    def tearDown(self):
        self.env.stop()
        super().tearDown()

    def raw(self):
        return sorted(p.relative_to(self.cell).as_posix() for p in self.cell.rglob("*")
                      if p.suffix in (".dlf", ".pcap"))

    def job(self):
        return {"rel_path": "h265/r1", "label": LABEL, "combo": "h265", "kind": "cell", "status": "OK",
                "cell_steps": True, "compress_raw": True}

    def run_job(self, fail=()):
        lines = []
        out = O.run_job(self.grid, self.job(), lines.append, importer=FakeModules(fail=fail), zstd=str(self.zstd),
                        step_timeout=None)
        return out, lines

    def test_only_after_reduce_metrics_and_report_succeeded(self):
        raw = self.raw()
        self.assertEqual(len(raw), 4)
        for broken in ("reduce", "metrics", "report"):
            out, lines = self.run_job(fail={broken})
            self.assertIn("skipped", out["compress"])
            self.assertEqual(self.raw(), raw, f"{broken} failed: nothing may be compressed")
            self.assertFalse(self.zlog.exists(), "zstd must not even run")
            self.assertNotIn("raw_compressed", capture.read_json(self.cell / "manifest.json"))
            self.assertTrue(any("UNCOMPRESSED" in x for x in lines), lines)

    def test_compresses_after_success_and_records_it(self):
        sizes = {r: (self.cell / r).stat().st_size for r in self.raw()}
        out, lines = self.run_job()
        self.assertEqual(self.raw(), [], "every original removed")
        for r, size in sizes.items():
            z = self.cell / (r + ".zst")
            self.assertTrue(z.is_file())
        calls = self.zlog.read_text().splitlines()
        self.assertEqual(len(calls), 8)                   # compress + test, per file
        for c in calls[::2]:
            self.assertTrue(c.startswith("-3 -T2 -q "), c)
        for c in calls[1::2]:
            self.assertTrue(c.startswith("-t -q "), c)
        man = capture.read_json(self.cell / "manifest.json")
        rec = man["raw_compressed"]
        self.assertTrue(rec["ok"])
        self.assertEqual(sorted(f["path"] for f in rec["files"]), sorted(sizes))
        self.assertEqual({f["path"]: f["bytes_before"] for f in rec["files"]}, sizes)
        self.assertEqual(rec["bytes_before"], sum(sizes.values()))
        self.assertEqual(rec["bytes_after"], sum((self.cell / (r + ".zst")).stat().st_size for r in sizes))
        self.assertTrue(man["timeline"]["compressed"])
        # SHA256SUMS untouched (the originals); SHA256SUMS.zst lists the .zst files
        for h in ("hosta", "hostb"):
            self.assertEqual((self.cell / h / "SHA256SUMS").read_text(), self.sums_before[h])
            lines_z = (self.cell / h / "SHA256SUMS.zst").read_text().splitlines()
            listed = {ln.split("  ")[2]: ln.split("  ")[0] for ln in lines_z}
            self.assertEqual(set(listed), {p.name for p in (self.cell / h).glob("*.zst") if p.name != "SHA256SUMS.zst"})
            for name, digest in listed.items():
                self.assertEqual(capture.sha256_file(self.cell / h / name), digest)
        self.assertTrue(any("compressed 4 raw file(s)" in x for x in lines), lines)
        # idempotent: nothing left to do, the record stays
        self.run_job()
        self.assertEqual(capture.read_json(self.cell / "manifest.json")["raw_compressed"]["files"], rec["files"])

    def test_original_removed_only_after_zstd_t_passes(self):
        with mock.patch.dict(os.environ, {"FAKE_ZSTD_TEST_FAIL": "1"}):
            out, _ = self.run_job()
        self.assertEqual(len(self.raw()), 4, "every original kept")
        self.assertEqual(list(self.cell.rglob("*.dlf.zst")) + list(self.cell.rglob("*.pcap.zst")), [])
        rec = capture.read_json(self.cell / "manifest.json")["raw_compressed"]
        self.assertFalse(rec["ok"])
        self.assertEqual(rec["files"], [])
        self.assertTrue(all("zstd -t failed" in p for p in rec["problems"]), rec["problems"])
        self.assertIsNone(capture.read_json(self.cell / "manifest.json")["timeline"]["compressed"])

    def test_failed_compression_keeps_the_original(self):
        with mock.patch.dict(os.environ, {"FAKE_ZSTD_FAIL": "1"}):
            rec = O.compress_cell(self.cell, zstd=str(self.zstd))
        self.assertEqual(len(self.raw()), 4)
        self.assertFalse(rec["ok"])
        self.assertEqual(len(rec["problems"]), 4)

    @unittest.skipUnless(shutil.which("zstd"), "zstd not installed")
    def test_real_zstd_round_trip(self):
        originals = {r: capture.sha256_file(self.cell / r) for r in self.raw()}
        rec = O.compress_cell(self.cell)
        self.assertTrue(rec["ok"], rec["problems"])
        self.assertEqual(self.raw(), [])
        for r, digest in originals.items():
            data = REAL_RUN(["zstd", "-dc", str(self.cell / (r + ".zst"))], capture_output=True).stdout
            import hashlib
            self.assertEqual(hashlib.sha256(data).hexdigest(), digest)


if __name__ == "__main__":
    unittest.main()


class PostRunChecks(unittest.TestCase):
    """Both smoke cells of 2026-09-30 06:26Z were marked INCOMPLETE by these two checks alone."""

    def test_rtsp_prefixed_file_source_matches_clip(self):
        from teleop.grid import agent
        self.assertEqual(agent._strip_source_scheme("rtsp:/m/clip.mp4"), "/m/clip.mp4")
        self.assertEqual(agent._strip_source_scheme("file:///m/clip.mp4"), "/m/clip.mp4")
        self.assertEqual(agent._strip_source_scheme("rtsp://camera/stream"), "rtsp://camera/stream")

    def test_hostb_subscriber_is_not_a_stale_participant(self):
        from teleop.grid import agent
        m = agent.STALE_RE.search('room L already has 1 participant(s) ["host-b-L"]; NOT deleting it.')
        self.assertEqual(agent._unexpected_participants(m, "L"), 0)
        m = agent.STALE_RE.search('room L already has 2 participant(s) ["host-b-L", "ghost"]')
        self.assertEqual(agent._unexpected_participants(m, "L"), 1)
        m = agent.STALE_RE.search("room L already has 1 participant(s)")
        self.assertEqual(agent._unexpected_participants(m, "L"), 1)
