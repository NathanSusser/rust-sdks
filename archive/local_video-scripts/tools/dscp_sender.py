#!/usr/bin/env python3
"""Unprivileged UDP sender that marks packets with a chosen DSCP code point.

Companion to dscp_listener.py.  Together they answer the only question that
matters for the "control" and "telemetry" classes: does the DSCP we set survive
the path to the receiver, or does something in between bleach or remap it?
(The media path cannot be tested this way -- the SFU re-originates packets, so
the publisher's mark never reaches the subscriber regardless of what we set.)

Root is NOT required.  setsockopt(IPPROTO_IP, IP_TOS, ...) is an ordinary socket
option on Linux for essentially all values; historic kernels demanded CAP_NET_ADMIN
for precedence >= 6 (CS6/CS7).  Rather than dying on such a kernel, a refusal is
caught, reported by name, and the run continues so the remaining values still get
tested.

Two details a naive version gets wrong:
  * TOS is DSCP<<2 -- the DSCP occupies the TOP six bits.  Passing the DSCP value
    straight to IP_TOS marks a completely different (and quieter) code point.
  * The kernel does not necessarily store what you set: it masks the ECN bits, and
    may clamp.  The value is therefore read back with getsockopt and the readback,
    not the request, is what gets printed.

Usage:
  python3 dscp_sender.py 127.0.0.1 5010 EF --rate 50 --duration 5
  python3 dscp_sender.py 127.0.0.1 5010 0,8,34,46,40 --rate 100 --duration 10
"""

from __future__ import annotations

import argparse
import socket
import struct
import sys
import time

IP_TOS = getattr(socket, "IP_TOS", 1)
IPV6_TCLASS = getattr(socket, "IPV6_TCLASS", 67)

# Same deliberate table as the listener.
DSCP_NAMES = {
    0: "DF/CS0", 8: "CS1", 10: "AF11", 16: "CS2", 18: "AF21",
    24: "CS3", 26: "AF31", 32: "CS4", 34: "AF41", 36: "AF42",
    38: "AF43", 40: "CS5", 46: "EF", 48: "CS6", 56: "CS7",
}
# Accept "DF", "CS0", "EF", ... case-insensitively.  DF and CS0 are the same point.
NAME_TO_DSCP = {"DF": 0, "CS0": 0}
for _v, _n in DSCP_NAMES.items():
    for _part in _n.split("/"):
        NAME_TO_DSCP[_part] = _v

MAGIC = b"DSCPPROBE"


def parse_dscp(token: str) -> int:
    """Accept a standard name (EF, AF41, CS5, DF) or a raw 0-63 value."""
    token = token.strip()
    upper = token.upper()
    if upper in NAME_TO_DSCP:
        return NAME_TO_DSCP[upper]
    try:
        value = int(token, 0)
    except ValueError:
        raise SystemExit(
            f"unrecognised DSCP '{token}'; use 0-63 or one of: "
            + ", ".join(sorted(NAME_TO_DSCP))
        )
    if not 0 <= value <= 63:
        raise SystemExit(f"DSCP out of range 0-63: {value}")
    return value


def label(dscp: int) -> str:
    name = DSCP_NAMES.get(dscp)
    return f"{dscp} ({name})" if name else f"{dscp} (unnamed)"


def apply_tos(sock: socket.socket, dscp: int, is_v6: bool) -> tuple[bool, int | None, str]:
    """Set the TOS/traffic-class byte; return (ok, readback_tos, message).

    The readback exists because the kernel silently normalises this field -- ECN
    bits get masked off.  Trusting the requested value would let the sender claim
    it marked something it did not.
    """
    tos = dscp << 2  # DSCP is the top 6 bits of the TOS byte
    try:
        if is_v6:
            sock.setsockopt(socket.IPPROTO_IPV6, IPV6_TCLASS, tos)
        else:
            sock.setsockopt(socket.IPPROTO_IP, IP_TOS, tos)
    except PermissionError as exc:
        return False, None, f"REFUSED without privileges: {exc}"
    except OSError as exc:
        return False, None, f"REFUSED: {exc}"
    try:
        if is_v6:
            back = sock.getsockopt(socket.IPPROTO_IPV6, IPV6_TCLASS)
        else:
            back = sock.getsockopt(socket.IPPROTO_IP, IP_TOS)
    except OSError as exc:
        return True, None, f"set ok, readback failed: {exc}"
    return True, back, "ok"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Paced UDP sender that marks packets with a chosen DSCP.",
    )
    parser.add_argument("host", help="destination host")
    parser.add_argument("port", type=int, help="destination UDP port")
    parser.add_argument("dscp", help="DSCP name or 0-63 value; comma-separated list "
                                     "cycles through them, splitting the duration")
    parser.add_argument("--rate", type=float, default=50.0, help="packets/sec (default 50)")
    parser.add_argument("--duration", type=float, default=5.0,
                        help="total seconds across all DSCP values (default 5)")
    parser.add_argument("--size", type=int, default=200, help="payload bytes (default 200)")
    parser.add_argument("--ipv6", action="store_true", help="use AF_INET6/IPV6_TCLASS")
    args = parser.parse_args(argv)

    if args.rate <= 0:
        raise SystemExit("--rate must be > 0")
    dscps = [parse_dscp(tok) for tok in args.dscp.split(",") if tok.strip()]
    if not dscps:
        raise SystemExit("no DSCP values given")

    family = socket.AF_INET6 if args.ipv6 else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_DGRAM)
    dest = (args.host, args.port)

    interval = 1.0 / args.rate
    per_value = args.duration / len(dscps)
    header = struct.Struct("!9sIBd")  # magic, seq, dscp we asked for, send time
    pad = b"\x00" * max(0, args.size - header.size)

    print(f"dscp_sender: -> {args.host}:{args.port}  rate={args.rate:g} pps  "
          f"size={args.size} B  duration={args.duration:g}s "
          f"({per_value:g}s per DSCP value)")

    seq = 0
    refused: list[str] = []
    sent_by_dscp: dict[int, int] = {}
    start = time.monotonic()

    for index, dscp in enumerate(dscps):
        ok, back, msg = apply_tos(sock, dscp, args.ipv6)
        if not ok:
            print(f"dscp_sender: DSCP {label(dscp)} -- {msg}; skipping this value")
            refused.append(label(dscp))
            continue
        if back is None:
            print(f"dscp_sender: DSCP {label(dscp)} set (TOS 0x{dscp << 2:02x}); {msg}")
        else:
            back_dscp = back >> 2
            note = "" if back_dscp == dscp else "  <-- KERNEL CHANGED IT"
            print(f"dscp_sender: DSCP {label(dscp)}  requested TOS=0x{dscp << 2:02x}  "
                  f"kernel readback TOS=0x{back:02x} (DSCP {back_dscp}, "
                  f"ECN {back & 3}){note}")

        # Absolute-deadline pacing: the next send time is computed from a fixed
        # origin, so scheduler jitter and send() cost do not accumulate into drift
        # and turn a steady flow into a burst.
        phase_start = start + index * per_value
        deadline = phase_start
        end = phase_start + per_value
        count = 0
        while True:
            now = time.monotonic()
            if deadline >= end:
                break
            if deadline > now:
                time.sleep(deadline - now)
            payload = header.pack(MAGIC, seq, dscp, time.time()) + pad
            try:
                sock.sendto(payload, dest)
            except OSError as exc:
                print(f"dscp_sender: send failed at seq {seq}: {exc}")
                break
            seq += 1
            count += 1
            deadline += interval
        sent_by_dscp[dscp] = sent_by_dscp.get(dscp, 0) + count
        print(f"dscp_sender:   sent {count} packets marked DSCP {label(dscp)}")

    print(f"dscp_sender: done, {seq} packets total in "
          f"{time.monotonic() - start:.2f}s")
    for dscp, count in sent_by_dscp.items():
        print(f"dscp_sender:   SET DSCP {label(dscp):<16} packets={count}")
    if refused:
        print("dscp_sender: values REFUSED without privileges: " + ", ".join(refused))
    else:
        print("dscp_sender: no DSCP value was refused (no root needed)")
    sock.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
