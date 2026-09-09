#!/usr/bin/env python3
"""Unprivileged UDP receiver that reports the IP TOS/DSCP byte that ACTUALLY ARRIVED.

WHY THIS EXISTS
---------------
Media in this project traverses an SFU, which re-originates every packet: any DSCP
mark the publisher applies is gone by the time the subscriber sees it, so DSCP is
unobservable end-to-end on the media path.  This tool is therefore for the DIRECT
sender->receiver UDP flows only (the "control" and "telemetry" classes), which
cannot be WebRTC data channels anyway -- in this SDK DSCP is applied only on the
RTP path in MediaChannelUtil, there is no DSCP handling in the SCTP/data path, and
every data channel shares one SCTP association on one DTLS transport, so
per-channel marking is structurally impossible.

The whole point is to read the byte that arrived, never the byte we intended to
send.  Two failure modes are specifically guarded against below, because both make
a naive implementation print a confident, wrong answer:

  1. Reporting DSCP 0 / "DF" when no ancillary data was delivered at all.  A missing
     cmsg means we learned NOTHING; it does not mean the packet was unmarked.  Those
     packets are counted under an explicit "NO TOS DATA" bucket, never folded into DF.
  2. Averaging a stream that was remarked mid-path.  Counts are kept PER DISTINCT
     DSCP VALUE, so a flow that starts EF and gets bleached to DF halfway shows up as
     two buckets rather than one meaningless mean.

Root is not required: IP_RECVTOS is an ordinary per-socket option.

Usage:
  python3 dscp_listener.py [port]
  python3 dscp_listener.py --port 5010 --bind 0.0.0.0 --summary-every 5
"""

from __future__ import annotations

import argparse
import signal
import socket
import struct
import sys
import time
from collections import OrderedDict

# --- Constants -------------------------------------------------------------
# socket.IP_RECVTOS only exists in the socket module on newer Pythons/platforms,
# so fall back to the Linux ABI numbers.  These are stable kernel UAPI values.
SOL_IP = getattr(socket, "SOL_IP", 0)
IP_TOS = getattr(socket, "IP_TOS", 1)
IP_RECVTOS = getattr(socket, "IP_RECVTOS", 13)
IPV6_RECVTCLASS = getattr(socket, "IPV6_RECVTCLASS", 66)
IPV6_TCLASS = getattr(socket, "IPV6_TCLASS", 67)

# Deliberately exactly the standard code points asked for; anything else is
# reported numerically as "(unnamed)" rather than being guessed at.
DSCP_NAMES = {
    0: "DF/CS0", 8: "CS1", 10: "AF11", 16: "CS2", 18: "AF21",
    24: "CS3", 26: "AF31", 32: "CS4", 34: "AF41", 36: "AF42",
    38: "AF43", 40: "CS5", 46: "EF", 48: "CS6", 56: "CS7",
}

ECN_NAMES = {0: "Not-ECT", 1: "ECT(1)", 2: "ECT(0)", 3: "CE"}


def dscp_label(dscp: int) -> str:
    name = DSCP_NAMES.get(dscp)
    return f"{dscp} ({name})" if name else f"{dscp} (unnamed)"


def extract_tos(ancdata) -> tuple[int | None, str]:
    """Return (tos_byte, how) from recvmsg ancillary data, or (None, reason).

    IMPORTANT SUBTLETY: you enable delivery with IP_RECVTOS (13), but the kernel
    tags the control message it hands back with cmsg_type = IP_TOS (1).  Matching
    only on IP_RECVTOS -- the obvious reading of "parse the IP_RECVTOS cmsg" --
    finds nothing and makes the tool silently report "no TOS data" on a path that
    is working fine.  Both types are accepted, and IPv6's IPV6_TCLASS too.

    Payload width also varies: IPv4 IP_TOS is a single byte, IPv6 IPV6_TCLASS is a
    native int.  Both widths are handled instead of assuming one.
    """
    for level, ctype, data in ancdata:
        v4 = level in (socket.IPPROTO_IP, SOL_IP) and ctype in (IP_TOS, IP_RECVTOS)
        v6 = level == socket.IPPROTO_IPV6 and ctype in (IPV6_TCLASS, IPV6_RECVTCLASS)
        if not (v4 or v6):
            continue
        if len(data) >= 4:
            value = struct.unpack("=I", data[:4])[0] & 0xFF
        elif len(data) >= 1:
            value = data[0]
        else:
            continue
        return value, ("IPV6_TCLASS" if v6 else "IP_TOS")
    return None, "no TOS cmsg in ancillary data"


class Stats:
    """Per-DSCP counters.  Never collapses distinct marks into an average."""

    def __init__(self) -> None:
        self.by_key: dict = OrderedDict()   # key -> [packets, bytes, ecn counter]
        self.packets = 0
        self.bytes = 0
        self.no_ancillary = 0
        self.ctrunc = 0
        self.started = time.monotonic()

    def add(self, tos: int | None, nbytes: int) -> None:
        self.packets += 1
        self.bytes += nbytes
        key = None if tos is None else (tos >> 2)
        if tos is None:
            self.no_ancillary += 1
        entry = self.by_key.get(key)
        if entry is None:
            entry = [0, 0, {}]
            self.by_key[key] = entry
        entry[0] += 1
        entry[1] += nbytes
        if tos is not None:
            ecn = tos & 0x03
            entry[2][ecn] = entry[2].get(ecn, 0) + 1

    def render(self, title: str) -> str:
        elapsed = time.monotonic() - self.started
        lines = [
            "",
            f"--- {title}  (t={elapsed:6.1f}s  packets={self.packets}  bytes={self.bytes}) ---",
        ]
        if not self.by_key:
            lines.append("  no packets received")
            return "\n".join(lines)
        lines.append(f"  {'DSCP':<16} {'packets':>9} {'bytes':>11}  ECN breakdown")
        for key, (pkts, nbytes, ecns) in self.by_key.items():
            if key is None:
                # Loud on purpose.  "We learned nothing" must never look like "DF".
                lines.append(
                    f"  {'NO TOS DATA':<16} {pkts:>9} {nbytes:>11}  "
                    "(kernel delivered no TOS cmsg -- value UNKNOWN, NOT 0/DF)"
                )
                continue
            ecn_txt = ", ".join(
                f"{ECN_NAMES.get(e, e)}={c}" for e, c in sorted(ecns.items())
            )
            lines.append(f"  {dscp_label(key):<16} {pkts:>9} {nbytes:>11}  {ecn_txt}")
        if self.ctrunc:
            lines.append(
                f"  WARNING: {self.ctrunc} packet(s) had MSG_CTRUNC -- the ancillary "
                "buffer was too small and TOS may have been dropped."
            )
        if self.no_ancillary and self.no_ancillary != self.packets:
            lines.append(
                f"  NOTE: {self.no_ancillary}/{self.packets} packets carried no TOS "
                "cmsg; the DSCP rows above cover only the rest."
            )
        return "\n".join(lines)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Unprivileged UDP listener that reports the arrived IP TOS/DSCP byte.",
    )
    parser.add_argument("port_positional", nargs="?", type=int, default=None,
                        help="UDP port to bind (default 5010)")
    parser.add_argument("--port", type=int, default=None, help="UDP port to bind")
    parser.add_argument("--bind", default="0.0.0.0", help="bind address (default 0.0.0.0)")
    parser.add_argument("--ipv6", action="store_true", help="bind an AF_INET6 socket instead")
    parser.add_argument("--summary-every", type=float, default=5.0,
                        help="seconds between periodic summaries (default 5)")
    parser.add_argument("--per-packet", type=int, default=10,
                        help="print this many per-packet lines before going quiet (default 10)")
    parser.add_argument("--no-recvtos", action="store_true",
                        help="SELF-TEST: deliberately skip enabling IP_RECVTOS, to prove "
                             "the tool says UNKNOWN rather than silently reporting DF")
    args = parser.parse_args(argv)

    port = args.port if args.port is not None else (
        args.port_positional if args.port_positional is not None else 5010)

    family = socket.AF_INET6 if args.ipv6 else socket.AF_INET
    bind_addr = args.bind
    if args.ipv6 and bind_addr == "0.0.0.0":
        bind_addr = "::"

    sock = socket.socket(family, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    # Ask the kernel to attach the received TOS/traffic-class byte as ancillary
    # data.  Without this recvmsg() returns an empty ancdata list and there is no
    # unprivileged way to see the arrived byte at all.
    recvtos_ok = True
    recvtos_err = ""
    if args.no_recvtos:
        # Self-test path: this is what a kernel/path that refuses to hand back the
        # TOS byte looks like.  Exercising it on demand is how we prove the tool
        # distinguishes "arrived unmarked" from "we could not tell".
        recvtos_ok = False
        recvtos_err = "--no-recvtos self-test, option deliberately not set"
    else:
        try:
            if args.ipv6:
                sock.setsockopt(socket.IPPROTO_IPV6, IPV6_RECVTCLASS, 1)
            else:
                sock.setsockopt(socket.IPPROTO_IP, IP_RECVTOS, 1)
        except OSError as exc:
            # Report rather than continue pretending; every packet would otherwise
            # be attributed to the "no TOS data" bucket without explaining why.
            recvtos_ok = False
            recvtos_err = str(exc)

    sock.bind((bind_addr, port))
    sock.settimeout(0.5)  # so the SIGINT flag below is checked promptly

    opt_name = "IPV6_RECVTCLASS" if args.ipv6 else "IP_RECVTOS"
    print(f"dscp_listener: bound {bind_addr}:{port} ({'IPv6' if args.ipv6 else 'IPv4'})")
    if recvtos_ok:
        print(f"dscp_listener: {opt_name} enabled; TOS will be read from ancillary data")
    else:
        print(f"dscp_listener: WARNING {opt_name} could NOT be enabled ({recvtos_err});")
        print("dscp_listener: every packet will be reported as UNKNOWN, not as DF.")
    print("dscp_listener: Ctrl-C for final summary")

    stats = Stats()
    stop = {"flag": False}

    def on_sigint(_signum, _frame):
        stop["flag"] = True

    signal.signal(signal.SIGINT, on_sigint)
    signal.signal(signal.SIGTERM, on_sigint)

    # Big enough for several cmsgs; MSG_CTRUNC is still checked in case it is not.
    ancbufsize = socket.CMSG_SPACE(4) * 8
    printed = 0
    next_summary = time.monotonic() + args.summary_every

    while not stop["flag"]:
        try:
            data, ancdata, flags, addr = sock.recvmsg(65535, ancbufsize)
        except socket.timeout:
            if time.monotonic() >= next_summary:
                print(stats.render("periodic summary"), flush=True)
                next_summary = time.monotonic() + args.summary_every
            continue
        except InterruptedError:
            continue

        if flags & getattr(socket, "MSG_CTRUNC", 0):
            stats.ctrunc += 1

        tos, how = extract_tos(ancdata)
        stats.add(tos, len(data))

        if printed < args.per_packet:
            printed += 1
            src = f"{addr[0]}:{addr[1]}"
            if tos is None:
                print(f"[pkt {stats.packets:>5}] from {src:<24} len={len(data):<5} "
                      f"TOS=UNKNOWN ({how}) -- NOT the same as DF", flush=True)
            else:
                print(f"[pkt {stats.packets:>5}] from {src:<24} len={len(data):<5} "
                      f"TOS=0x{tos:02x} DSCP={dscp_label(tos >> 2):<16} "
                      f"ECN={ECN_NAMES.get(tos & 3)} via {how}", flush=True)
            if printed == args.per_packet:
                print(f"[pkt ...] per-packet output capped at {args.per_packet}; "
                      "summaries continue below", flush=True)

        if time.monotonic() >= next_summary:
            print(stats.render("periodic summary"), flush=True)
            next_summary = time.monotonic() + args.summary_every

    print(stats.render("FINAL summary"), flush=True)
    if stats.packets and stats.no_ancillary == stats.packets:
        print("\ndscp_listener: RESULT INCONCLUSIVE -- packets arrived but the kernel")
        print("dscp_listener: never supplied a TOS byte, so nothing was learned about")
        print("dscp_listener: the marking.  Do not read this as DF.")
    sock.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
