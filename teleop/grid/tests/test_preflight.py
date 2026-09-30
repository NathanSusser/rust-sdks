"""Pre-flight gates against injected command output, plus the capture helpers they share.

    python3 -m unittest teleop/grid/tests/test_preflight.py
"""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from teleop.grid import capture, hostcfg, preflight  # noqa: E402
from teleop.grid.capture import hops  # noqa: E402

CFG_B = {"role": "b", "peer": "192.0.2.1", "wwan_iface": "wwan0", "ptp_iface": "eno2", "ptp_role": "slave",
         "modem_index": 0, "diag_tty": "/dev/ttyUSB0", "diag_venv_python": "/x/python", "credentials_env": "/x/.env",
         "tcpdump": "/usr/bin/tcpdump", "results_root": "/tmp", "display": ":0", "repo": "/x", "peer_repo": "/x"}
CFG_A = dict(CFG_B, role="a", ptp_iface="enp5s0", ptp_role="master", modem_index=1)

PMC_SLAVE = """sending: GET PORT_DATA_SET
\t0123ab.fffe.000001-1 seq 0 RESPONSE MANAGEMENT PORT_DATA_SET
\t\tportIdentity            0123ab.fffe.000001-1
\t\tportState               SLAVE
\t\tlogMinDelayReqInterval  -3
"""
PMC_CURRENT = """sending: GET CURRENT_DATA_SET
\t0123ab.fffe.000001-0 seq 0 RESPONSE MANAGEMENT CURRENT_DATA_SET
\t\tstepsRemoved     1
\t\toffsetFromMaster 2176.0
\t\tmeanPathDelay    20000.0
"""


def servo_journal(n, state=2, offset=150):
    t = time.time()
    return "\n".join(f"{t - 30 + i:.6f} hostb ptp4l[812]: [{9000 + i}.1] master offset {offset} s{state} freq -1102 "
                     f"path delay 942" for i in range(n))


class Fake:
    """argv-prefix -> (rc, stdout, stderr); files -> contents."""

    def __init__(self, cmds=None, files=None, procs=None):
        self.cmds = cmds or {}
        self.files = files or {}
        self._procs = procs or []
        self.calls = []

    def run(self, argv, timeout=10):
        self.calls.append(argv)
        key = " ".join(argv)
        best = None
        for prefix, res in self.cmds.items():
            if key.startswith(prefix) and (best is None or len(prefix) > len(best[0])):
                best = (prefix, res)
        return best[1] if best else (127, "", f"{argv[0]}: not faked")

    def read(self, path):
        return self.files.get(path)

    def procs(self):
        return list(self._procs)

    def ctx(self, cfg, **kw):
        return preflight.Ctx(cfg=cfg, role=cfg["role"], gates=preflight.load_gates(), run=self.run, read=self.read,
                             procs=self.procs, **kw)


def ptp_b_fake(carrier="1", speed="1000", pmc=PMC_SLAVE, journal=None):
    return Fake(cmds={
        "pmc -u -b 0 -i": (0, pmc, "") if pmc else (0, "sending: GET PORT_DATA_SET\n", ""),
        "pmc -u -b 0 -i /tmp": (0, pmc, "") if pmc else (0, "sending\n", ""),
        "sudo -n pmc": (1, "", "sudo: a password is required"),
        "journalctl --since -30s": (0, servo_journal(30) if journal is None else journal, ""),
        "journalctl -b": (0, "", ""),
    }, files={"/sys/class/net/eno2/carrier": carrier + "\n", "/sys/class/net/eno2/speed": speed + "\n"})


class PtpB(unittest.TestCase):
    def test_pass(self):
        f = ptp_b_fake()
        # CURRENT_DATA_SET goes through the same pmc prefix; answer both.
        f.cmds["pmc -u -b 0 -i"] = (0, PMC_SLAVE + PMC_CURRENT, "")
        r = preflight.gate_ptp_b(f.ctx(CFG_B))
        self.assertTrue(r["pass"], r["detail"])
        self.assertEqual(r["data"]["state"], "SLAVE")
        self.assertEqual(r["data"]["servo_lines_30s"], 30)

    def test_cable_unplugged_fails_even_with_servo_s2(self):
        """The exact failure of the old gate: s2 lines present, carrier down."""
        r = preflight.gate_ptp_b(ptp_b_fake(carrier="0").ctx(CFG_B))
        self.assertFalse(r["pass"])
        self.assertIn("carrier DOWN", r["detail"])

    def test_slow_link_fails(self):
        r = preflight.gate_ptp_b(ptp_b_fake(speed="100").ctx(CFG_B))
        self.assertFalse(r["pass"])
        self.assertIn("100 Mb/s", r["detail"])

    def test_stale_servo_fails(self):
        r = preflight.gate_ptp_b(ptp_b_fake(journal=servo_journal(3)).ctx(CFG_B))
        self.assertFalse(r["pass"])
        self.assertIn("fresh ptp4l servo lines", r["detail"])

    def test_servo_not_locked_fails(self):
        r = preflight.gate_ptp_b(ptp_b_fake(journal=servo_journal(30, state=1)).ctx(CFG_B))
        self.assertFalse(r["pass"])
        self.assertIn("s1", r["detail"])

    def test_listening_fails(self):
        r = preflight.gate_ptp_b(ptp_b_fake(pmc=PMC_SLAVE.replace("SLAVE\n", "LISTENING\n")).ctx(CFG_B))
        self.assertFalse(r["pass"])
        self.assertIn("LISTENING", r["detail"])

    def test_large_offset_fails(self):
        r = preflight.gate_ptp_b(ptp_b_fake(journal=servo_journal(30, offset=50000)).ctx(CFG_B))
        self.assertFalse(r["pass"])
        self.assertIn("offset", r["detail"])

    def test_no_pmc_uses_journal_state_and_says_so(self):
        f = ptp_b_fake(pmc=None)
        f.cmds["journalctl -b"] = (0, "1.0 hostb ptp4l[812]: [5.1] port 1 (eno2): UNCALIBRATED to SLAVE on "
                                      "MASTER_CLOCK_SELECTED\n", "")
        r = preflight.gate_ptp_b(f.ctx(CFG_B))
        self.assertTrue(r["pass"], r["detail"])
        self.assertIn("pmc unavailable", r["detail"])

    def test_phc2sys_acquiring_fails(self):
        j = servo_journal(30) + "\n" + f"{time.time():.3f} hostb phc2sys[900]: [9.9] CLOCK_REALTIME phc offset  -27 s0 freq -1 delay 942"
        r = preflight.gate_ptp_b(ptp_b_fake(journal=j).ctx(CFG_B))
        self.assertFalse(r["pass"])
        self.assertIn("phc2sys", r["detail"])

    def test_rms_summary_lines_count(self):
        t = time.time()
        j = "\n".join(f"{t:.3f} hostb ptp4l[1]: [1.0] rms 120 max 300 freq -1100 +/- 5 delay 900 +/- 2"
                      for _ in range(30))
        r = preflight.gate_ptp_b(ptp_b_fake(journal=j).ctx(CFG_B))
        self.assertTrue(r["pass"], r["detail"])


class PtpA(unittest.TestCase):
    JOURNAL = ("1.0 hosta ptp4l[4591]: [7936.859] port 1 (enp5s0): FAULTY to LISTENING on INIT_COMPLETE\n"
               "2.0 hosta ptp4l[4591]: [7944.799] port 1 (enp5s0): LISTENING to MASTER on ANNOUNCE_RECEIPT_TIMEOUT_EXPIRES\n")

    def fake(self, journal=JOURNAL, carrier="1", running=True):
        return Fake(cmds={"pmc": (0, "sending\n", ""), "sudo -n pmc": (1, "", "password required"),
                          "journalctl -b": (0, journal, ""), "pgrep -x ptp4l": (0 if running else 1, "4591\n", "")},
                    files={"/sys/class/net/enp5s0/carrier": carrier, "/sys/class/net/enp5s0/speed": "1000"})

    def test_master_from_journal(self):
        r = preflight.gate_ptp_a(self.fake().ctx(CFG_A))
        self.assertTrue(r["pass"], r["detail"])
        self.assertEqual(r["data"]["state"], "MASTER")

    def test_faulty_after_master(self):
        j = self.JOURNAL + "3.0 hosta ptp4l[4591]: [8000.0] port 1 (enp5s0): MASTER to FAULTY on FAULT_DETECTED\n"
        r = preflight.gate_ptp_a(self.fake(journal=j).ctx(CFG_A))
        self.assertFalse(r["pass"])
        self.assertIn("FAULTY", r["detail"])

    def test_other_interface_ignored(self):
        j = self.JOURNAL + "3.0 hosta ptp4l[4591]: [8000.0] port 2 (eth9): MASTER to FAULTY on FAULT_DETECTED\n"
        self.assertTrue(preflight.gate_ptp_a(self.fake(journal=j).ctx(CFG_A))["pass"])

    def test_not_running(self):
        r = preflight.gate_ptp_a(self.fake(running=False).ctx(CFG_A))
        self.assertFalse(r["pass"])


class Encoder(unittest.TestCase):
    LSMOD = "Module                  Size  Used by\nnvidia_uvm 1 0\nnvidia 15106048 201 nvidia_uvm\n"

    def test_pass(self):
        f = Fake(cmds={"lsmod": (0, self.LSMOD, ""), "ldconfig -p": (0, "libnvidia-encode.so.1 (libc6,x86-64)\n", "")})
        self.assertTrue(preflight.gate_encoder(f.ctx(CFG_A))["pass"])

    def test_no_module_never_falls_back(self):
        f = Fake(cmds={"lsmod": (0, "Module Size Used by\nnvidia_uvm 1 0\n", ""), "ldconfig -p": (0, "", "")})
        r = preflight.gate_encoder(f.ctx(CFG_A))
        self.assertFalse(r["pass"])
        self.assertIn("no software fallback", r["detail"])

    def test_no_nvenc_library(self):
        f = Fake(cmds={"lsmod": (0, self.LSMOD, ""), "ldconfig -p": (0, "libc.so.6\n", "")})
        self.assertFalse(preflight.gate_encoder(f.ctx(CFG_A))["pass"])


MMCLI_L = "    /org/freedesktop/ModemManager1/Modem/0 [Quectel] RM520N-GL\n"


def mm_json(state="connected", tech=("5gnr",), bands=("eutran-2", "ngran-41"), port="cdc-wdm8"):
    return json.dumps({"modem": {"generic": {"state": state, "access-technologies": list(tech),
                                             "current-bands": list(bands), "primary-port": port}}})


RF = """[/dev/cdc-wdm8] Successfully got RF band info
Band Information:
\tRadio Interface:   '5gnr'
\tActive Band Class: 'nr5g-41'
\tActive Channel:    '61038'
Band Information (Extended):
\tRadio Interface:   '5gnr'
\tActive Band Class: 'nr5g-41'
\tActive Channel:    '520110'
Bandwidth:
\tRadio Interface:   '5gnr'
\tBandwidth:         '(null)'
"""


class Modem(unittest.TestCase):
    def fake(self, js=None, rf=RF, lst=MMCLI_L):
        return Fake(cmds={"mmcli -L": (0, lst, ""), "mmcli -m 0 -J": (0, js or mm_json(), ""),
                          "sudo -n qmicli -d /dev/cdc-wdm8 -p --nas-get-rf-band-info": (0 if rf else 1, rf or "", ""),
                          "qmicli": (1, "", "denied"),
                          "sudo -n qmicli -d /dev/cdc-wdm8 -p --nas-get-cell-location-info": (1, "", "")})

    def test_pass_and_index_fallback_is_reported(self):
        r = preflight.gate_modem(self.fake().ctx(CFG_A, expect={"band": "n41"}))
        self.assertTrue(r["pass"], r["detail"])
        self.assertEqual(r["data"]["band"], "n41")
        self.assertEqual(r["data"]["arfcn"], 520110)
        self.assertIn("modem_index 1 not listed", r["detail"])

    def test_band_lock_excluding_expectation_fails(self):
        """B was band-locked to n25 under the tests by another session."""
        r = preflight.gate_modem(self.fake(js=mm_json(bands=("ngran-25",)), rf=None).ctx(CFG_B, expect={"band": "n41"}))
        self.assertFalse(r["pass"])
        self.assertIn("band-locked", r["detail"])

    def test_live_band_mismatch(self):
        r = preflight.gate_modem(self.fake(rf=RF.replace("nr5g-41", "nr5g-25")).ctx(
            CFG_A, expect={"band": "n41"}))
        self.assertFalse(r["pass"])

    def test_unverified_live_band_passes_and_says_so(self):
        r = preflight.gate_modem(self.fake(rf=None).ctx(CFG_B, expect={"band": "n41"}))
        self.assertTrue(r["pass"], r["detail"])
        self.assertIn("UNVERIFIED", r["detail"])

    def test_not_attached(self):
        r = preflight.gate_modem(self.fake(js=mm_json(state="searching")).ctx(CFG_A))
        self.assertFalse(r["pass"])

    def test_lte_only(self):
        r = preflight.gate_modem(self.fake(js=mm_json(tech=("lte",))).ctx(CFG_A))
        self.assertFalse(r["pass"])

    def test_no_modem(self):
        r = preflight.gate_modem(self.fake(lst="No modems were found\n").ctx(CFG_A))
        self.assertFalse(r["pass"])


class DiagAndLeftovers(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["DIAG_LOCK"] = os.path.join(self.tmp.name, ".ttyUSB0.lock")

    def tearDown(self):
        os.environ.pop("DIAG_LOCK", None)
        self.tmp.cleanup()

    def test_port_held(self):
        f = Fake(cmds={"fuser": (0, "", "/dev/ttyUSB0:  4242")})
        r = preflight.gate_diag_port(f.ctx(CFG_A))
        self.assertFalse(r["pass"])
        self.assertIn("4242", r["detail"])

    def test_lock_held(self):
        import fcntl
        with open(os.environ["DIAG_LOCK"], "w") as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)
            r = preflight.gate_diag_port(Fake(cmds={"fuser": (1, "", "")}).ctx(CFG_A))
        self.assertFalse(r["pass"])
        self.assertIn("held by another capture", r["detail"])

    def test_qcsuper_running(self):
        procs = [{"pid": 77, "comm": "python3", "argv": ["/v/bin/python3", "/x/qcsuper-noroot-fast2", "--usb-modem"]}]
        r = preflight.gate_diag_port(Fake(cmds={"fuser": (1, "", "")}, procs=procs).ctx(CFG_A))
        self.assertFalse(r["pass"])
        self.assertIn("qcsuper already running", r["detail"])

    def test_shell_mentioning_qcsuper_is_not_qcsuper(self):
        """pgrep -f self-match: a shell whose command text names qcsuper is not a capture."""
        self.assertFalse(capture.is_qcsuper(["bash", "-c", "pgrep -f qcsuper-noroot"]))
        self.assertFalse(capture.is_qcsuper(["grep", "qcsuper"]))
        self.assertTrue(capture.is_qcsuper(["/v/bin/python", "/x/qcsuper-noroot", "--usb-modem", "/dev/ttyUSB0"]))

    def test_leftovers(self):
        procs = [
            {"pid": 10, "comm": "tcpdump", "argv": ["tcpdump", "-w", "old.pcap"]},
            {"pid": 11, "comm": "bash", "argv": ["bash", "hop-recorder.sh", "old", "300", "d"]},
            {"pid": 12, "comm": "vim", "argv": ["vim", "hop-recorder.sh"]},
            {"pid": 13, "comm": "python3", "argv": ["python3", "-m", "teleop.grid.capture.hops", "record"]},
            {"pid": 14, "comm": "teleop-harness", "argv": ["./teleop-harness"]},
            {"pid": 15, "comm": "bash", "argv": ["bash", "-c", "echo tcpdump subscriber"]},
        ]
        r = preflight.gate_capture_leftovers(Fake(procs=procs).ctx(CFG_A))
        self.assertFalse(r["pass"])
        pids = sorted(x["pid"] for x in r["data"]["leftovers"])
        self.assertEqual(pids, [10, 11, 13, 14])

    def test_no_leftovers(self):
        r = preflight.gate_capture_leftovers(Fake(procs=[{"pid": 1, "comm": "systemd", "argv": ["/sbin/init"]}]).ctx(CFG_A))
        self.assertTrue(r["pass"])

    def test_list_procs_excludes_self(self):
        mine = {p["pid"] for p in capture.list_procs()}
        self.assertNotIn(os.getpid(), mine)


class Clock(unittest.TestCase):
    def m(self, off, servers=3, spread=3.0):
        return lambda: {"host_minus_utc_s": off, "servers": servers, "spread_ms": spread, "asked": [1, 2, 3]}

    def test_pass(self):
        r = preflight.gate_clock_offset(Fake().ctx(CFG_A, clock=self.m(-15.8)))
        self.assertTrue(r["pass"])
        self.assertEqual(r["data"]["clock"]["host_minus_utc_s"], -15.8)

    def test_warn_on_jump(self):
        r = preflight.gate_clock_offset(Fake().ctx(CFG_A, clock=self.m(-24.0), prev_clock={"host_minus_utc_s": -16.0}))
        self.assertTrue(r["pass"])
        self.assertIn("WARN changed", r["detail"])

    def test_too_few_servers(self):
        r = preflight.gate_clock_offset(Fake().ctx(CFG_A, clock=self.m(None, servers=0, spread=None)))
        self.assertFalse(r["pass"])

    def test_spread(self):
        self.assertFalse(preflight.gate_clock_offset(Fake().ctx(CFG_A, clock=self.m(-1, spread=40)))["pass"])


class Other(unittest.TestCase):
    def test_decoder_av1(self):
        f = Fake(cmds={"lsmod": (0, "Module\n", ""), "ldconfig -p": (0, "", "")})
        ctx = f.ctx(CFG_B, cell={"variables": {"codec": "av1"}})
        self.assertFalse(preflight.gate_decoder_b(ctx)["pass"])
        ctx = f.ctx(CFG_B, cell={"variables": {"codec": "h264"}})
        self.assertTrue(preflight.gate_decoder_b(ctx)["pass"])

    def test_disk(self):
        class St:
            f_bavail, f_frsize = 10, 1_000_000_000
        ctx = Fake().ctx(CFG_A, cell={"variables": {"duration_s": 300, "lead_s": 60}})
        self.assertTrue(preflight.gate_disk(ctx, statvfs=lambda p: St)["pass"])  # 10 GB > 3*7MB*390
        St.f_bavail = 5
        self.assertFalse(preflight.gate_disk(ctx, statvfs=lambda p: St)["pass"])

    def test_room_is_skipped_not_failed(self):
        r = preflight.gate_room(Fake().ctx(CFG_A))
        self.assertTrue(r["pass"])
        self.assertTrue(r["data"]["skipped"])

    def test_code_match(self):
        a = {"commit": "c1", "harness_sha256": "h", "requirements_sha256": "r", "package_sha256": "p"}
        self.assertTrue(preflight.code_match(a, dict(a), {})["pass"])
        r = preflight.code_match(a, dict(a, harness_sha256="h2"), {})
        self.assertFalse(r["pass"])
        self.assertIn("harness_sha256", r["detail"])
        self.assertFalse(preflight.code_match(dict(a, commit=None), dict(a, commit=None), {})["pass"])


class Timing(unittest.TestCase):
    def test_hanging_gate_is_a_fail_not_a_hang(self):
        def slow(ctx):
            time.sleep(5)
            return preflight.result("slow", True, "")

        def fast(ctx):
            return preflight.result("fast", True, "ok")

        def boom(ctx):
            raise ValueError("bad")
        t0 = time.monotonic()
        out = preflight.run_gates(Fake().ctx(CFG_A), [slow, fast, boom], timeout=0.5)
        self.assertLess(time.monotonic() - t0, 2)
        by = {r["name"]: r for r in out}
        self.assertFalse(by["slow"]["pass"])
        self.assertIn("timed out", by["slow"]["detail"])
        self.assertTrue(by["fast"]["pass"])
        self.assertFalse(by["boom"]["pass"])
        self.assertIn("ValueError", by["boom"]["detail"])


class Parsers(unittest.TestCase):
    def test_signal_5g_only(self):
        js = json.dumps({"modem": {"signal": {"lte": {"rsrp": "-80.00"}, "5g": {"rsrp": "-95.00", "rsrq": "-11.00",
                                                                                "snr": "5.00"}}}})
        self.assertEqual(hops.parse_mmcli_signal(js), {"nr_rsrp_dbm": "-95", "nr_rsrq_db": "-11", "nr_snr_db": "5"})
        js = json.dumps({"modem": {"signal": {"5g": {"rsrp": "--"}}}})
        self.assertEqual(hops.parse_mmcli_signal(js), {})

    def test_proc_net_udp_rows_not_header(self):
        text = ("  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode ref pointer drops\n"
                " 1: 00000000:14E9 00000000:0000 07 00000000:00034000 00:00000000 00000000  1000        0 1 2 0 5\n"
                " 2: 00000000:14EA 00000000:0000 07 00000000:00000010 00:00000000 00000000  1000        0 1 2 0 3\n")
        self.assertEqual(hops.parse_proc_net_udp(text), (0x34000, 8))

    def test_tc(self):
        t = ("qdisc fq_codel 0: root refcnt 2 limit 10240p\n Sent 123 bytes 45 pkt (dropped 1, overlimits 2 requeues 3)\n"
             " backlog 100b 2p requeues 3\n")
        self.assertEqual(hops.parse_tc_qdisc(t), {"qdisc_sent_pkts": "45", "qdisc_dropped": "1",
                                                  "qdisc_overlimits": "2", "qdisc_requeues": "3",
                                                  "qdisc_backlog_bytes": "100", "qdisc_backlog_pkts": "2"})

    def test_resolve_modem(self):
        self.assertEqual(hops.resolve_modem(0, MMCLI_L)[0], 0)
        self.assertEqual(hops.resolve_modem(1, MMCLI_L)[0], 0)
        two = MMCLI_L + "    /org/freedesktop/ModemManager1/Modem/3 [x] y\n"
        self.assertIsNone(hops.resolve_modem(1, two)[0])

    def test_rf_band_prefers_extended_channel(self):
        """The legacy channel field is 16-bit: 520110 reads as 61038 there."""
        self.assertEqual(preflight.parse_rf_band_info(RF), [{"band": "n41", "arfcn": 520110}])
        old = "Band Information:\n\tRadio Interface:   '5gnr'\n\tActive Band Class: 'nr5g-band-41'\n\tActive Channel:    '520110'\n"
        self.assertEqual(preflight.parse_rf_band_info(old), [{"band": "n41", "arfcn": 520110}])


class HostCfg(unittest.TestCase):
    def test_validate(self):
        raw = {k: v for k, v in CFG_B.items() if k != "peer_repo"}
        cfg = hostcfg.validate(dict(raw, results_root="~/teleop-runs"))
        self.assertTrue(cfg["results_root"].startswith("/"))
        self.assertEqual(cfg["peer_repo"], cfg["repo"])
        with self.assertRaisesRegex(hostcfg.HostConfigError, "missing key.*wwan_iface"):
            hostcfg.validate({k: v for k, v in raw.items() if k != "wwan_iface"})
        with self.assertRaisesRegex(hostcfg.HostConfigError, "unknown key"):
            hostcfg.validate(dict(raw, sfu="x"))
        with self.assertRaisesRegex(hostcfg.HostConfigError, "display"):
            hostcfg.validate(dict(raw, display=""))
        self.assertEqual(hostcfg.validate(dict(raw, role="a", display=None))["display"], "")

    def test_b_keep_after_pull(self):
        raw = {k: v for k, v in CFG_B.items() if k != "peer_repo"}
        self.assertIs(hostcfg.validate(dict(raw))["b_keep_after_pull"], False)
        for v, want in ((True, True), ("yes", True), (1, True), (False, False), ("off", False), (None, False)):
            self.assertIs(hostcfg.validate(dict(raw, b_keep_after_pull=v))["b_keep_after_pull"], want, v)
        with self.assertRaisesRegex(hostcfg.HostConfigError, "b_keep_after_pull must be true or false"):
            hostcfg.validate(dict(raw, b_keep_after_pull="maybe"))

    def test_sfu_env(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "sfu.env"
            p.write_text("# c\nexport TELEOP_SFU_HOST='sfu.example.test'\n")
            self.assertEqual(hostcfg.sfu_host(p), "sfu.example.test")
            p.write_text("OTHER=1\n")
            with self.assertRaisesRegex(hostcfg.HostConfigError, "not set"):
                hostcfg.sfu_host(p)


class GridDisk(unittest.TestCase):
    """The whole-grid disk projection on Host A (control plane v2)."""

    @staticmethod
    def cells(n=2, kbps=8800, lead=45, dur=180, repeats=(1, 2)):
        return [{"repeat": repeats[i % len(repeats)],
                 "variables": {"kbps": kbps, "lead_s": lead, "duration_s": dur}} for i in range(n)]

    @staticmethod
    def st(free):
        class St:
            f_bavail, f_frsize = free, 1
        return lambda p: St

    def test_formula(self):
        # span 255 s; DLF 2 hosts x 4.5 MB/s x 255 s x 1.1; pcap per host: 8800 kbps = 1.1 MB/s
        # = 1000 video pkt/s + 500 other = 1500 pkt/s x 144 B x 255 s
        dlf_cell = 2 * 4.5e6 * 255 * 1.1
        pcap_host = 1500 * 144 * 255
        self.assertAlmostEqual(preflight.pcap_bytes_estimate(self.cells()[0]), pcap_host)
        p = preflight.grid_disk_projection(self.cells(), compress_raw=False)
        self.assertAlmostEqual(p["dlf_bytes"], 2 * dlf_cell)
        self.assertAlmostEqual(p["pcap_bytes"], 2 * 2 * pcap_host)
        self.assertAlmostEqual(p["uncompressed_bytes"], 2 * (dlf_cell + 2 * pcap_host))
        self.assertAlmostEqual(p["projected_bytes"], p["uncompressed_bytes"])
        self.assertAlmostEqual(p["uncompressed_bytes"], 5_269_320_000)
        q = preflight.grid_disk_projection(self.cells(), compress_raw=True)
        self.assertAlmostEqual(q["projected_bytes"], 0.75 * 5_269_320_000)
        self.assertAlmostEqual(q["uncompressed_bytes"], 5_269_320_000)
        self.assertAlmostEqual(q["last_repeat_round_bytes"], 0.75 * (dlf_cell + 2 * pcap_host))

    def test_gate_with_and_without_compression(self):
        free = 24e9                       # budget = free - 20 GB = 4 GB
        on = preflight.gate_grid_disk(self.cells(), "/nonexistent/x", True, statvfs=self.st(free))
        self.assertTrue(on["pass"], on["detail"])                 # 3.95 GB <= 4 GB
        self.assertIn("project 4.0 GB", on["detail"])
        self.assertIn("5.3 GB uncompressed", on["detail"])
        self.assertIn("free 24.0 GB", on["detail"])
        self.assertIn("budget free - 20 GB = 4.0 GB", on["detail"])
        off = preflight.gate_grid_disk(self.cells(), "/nonexistent/x", False, statvfs=self.st(free))
        self.assertFalse(off["pass"])                              # 5.27 GB > 4 GB
        self.assertIn("SHORT by 1.3 GB", off["detail"])
        for hint in ("fewer repeats", "shorter lead_s", "compress_raw: true", "free space under /"):
            self.assertIn(hint, off["detail"])
        self.assertEqual(off["data"]["budget_bytes"], free - 20e9)
        # 20 GB of headroom is not negotiable: just enough free space for the bytes still fails
        tight = preflight.gate_grid_disk(self.cells(), "/", True, statvfs=self.st(4e9))
        self.assertFalse(tight["pass"])

    def test_tonight(self):
        from teleop.grid import grid as G
        cells = G.load(G.TELEOP_DIR / "config" / "grids" / "tonight.yaml", seed=1).expand()
        p = preflight.grid_disk_projection(cells, compress_raw=True)
        self.assertEqual(p["cells"], 60)
        self.assertAlmostEqual(p["dlf_bytes"], 60 * 2 * 4.5e6 * 255 * 1.1)
        pcap = sum(2 * (c.values["kbps"] * 125 / 1100 + 500) * 144 * 255 for c in cells)
        self.assertAlmostEqual(p["pcap_bytes"], pcap)
        self.assertAlmostEqual(p["projected_bytes"], 0.75 * (p["dlf_bytes"] + pcap))
        self.assertAlmostEqual(p["lead_10s_bytes"], 60 * 2 * 4.5e6 * 10 * 1.1 * 0.75)


class Checksums(unittest.TestCase):
    def test_roundtrip_and_tamper(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "a.csv").write_text("x,y\n1,2\n")
            (d / "sub").mkdir()
            (d / "sub" / "b.bin").write_bytes(b"\0" * 1000)
            info = capture.write_checksums(d)
            self.assertEqual(info["files"], 2)
            line = (d / "SHA256SUMS").read_text().splitlines()[0]
            digest, size, rel = line.split("  ")
            self.assertEqual((size, rel), ("8", "a.csv"))
            self.assertTrue(capture.verify_checksums(d)["ok"])
            (d / "a.csv").write_text("x,y\n1,3\n")
            (d / "extra").write_text("")
            v = capture.verify_checksums(d)
            self.assertFalse(v["ok"])
            self.assertTrue(any("sha256 differs" in p for p in v["problems"]))
            self.assertTrue(any("not in SHA256SUMS" in p for p in v["problems"]))

    def test_verify_pid_detects_reuse_and_mismatch(self):
        me = os.getpid()
        rec = {"pid": me, "start_ticks": capture.start_ticks(me), "signature": [["python"]]}
        self.assertTrue(capture.verify_pid(rec)[0])
        self.assertFalse(capture.verify_pid(dict(rec, start_ticks=rec["start_ticks"] + 1))[0])
        self.assertFalse(capture.verify_pid(dict(rec, signature=[["teleop-harness", "--room-name", "zz"]]))[0])
        self.assertFalse(capture.verify_pid({"pid": 2 ** 22 + 12345})[0])


class AgentReply(unittest.TestCase):
    def test_parse_reply_takes_json_after_banner(self):
        from teleop.grid.orchestrator import parse_reply
        r = parse_reply("Welcome banner\n{\"ok\": true, \"x\": 1}\n", "", 0, "b:identity")
        self.assertEqual(r["x"], 1)
        r = parse_reply("", "ssh: connect to host timed out", 255, "b:identity")
        self.assertFalse(r["ok"])
        self.assertIn("timed out", r["error"])

    def test_agent_prints_exactly_one_json_object(self):
        import subprocess
        repo = Path(__file__).resolve().parents[3]
        with tempfile.TemporaryDirectory() as d:
            hy = Path(d) / "host.yaml"
            import yaml
            hy.write_text(yaml.safe_dump({k: v for k, v in CFG_A.items() if k != "peer_repo"} | {"repo": str(repo)}))
            env = dict(os.environ, TELEOP_HOST_YAML=str(hy))
            r = subprocess.run([sys.executable, "-m", "teleop.grid.agent", "identity"], cwd=repo, env=env,
                               capture_output=True, text=True, timeout=120)
            lines = [ln for ln in r.stdout.splitlines() if ln.strip()]
            self.assertEqual(len(lines), 1, r.stdout)
            out = json.loads(lines[0])
            self.assertTrue(out["ok"], out)
            self.assertEqual(out["role"], "a")
            self.assertEqual(len(out["package_sha256"]), 64)
            r = subprocess.run([sys.executable, "-m", "teleop.grid.agent", "status"], cwd=repo, env=env,
                               capture_output=True, text=True, timeout=60)
            out = json.loads(r.stdout.strip())
            self.assertFalse(out["ok"])
            self.assertIn("--cell-dir", out["error"])
            self.assertEqual(r.returncode, 1)


if __name__ == "__main__":
    unittest.main()
