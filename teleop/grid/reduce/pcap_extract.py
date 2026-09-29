"""wwan0 pcap -> per-packet rows via tshark, and the video flow found in them.

Field list is the one the reference *.pkts dumps used (segjoin.py / proof.py / vbv-report.py):
    time, src, dst, sport, dport, udplen, pt, seq, rtpts, marker, ssrc, rtcp.pt, rtpfb.fmt
with rtp.heuristic_rtp TRUE. Separator is TAB and every occurrence is kept (aggregator ';'),
because a compound RTCP packet carries several rtcp.pt values and a comma-separated dump
would silently shift columns.

Nothing about the SFU is hard-coded: the SFU media address is the peer of the local wwan
address on the flow that carries the most RTP packets, and the video payload type is the
dominant RTP pt on that flow in the media direction (A: local -> SFU, B: SFU -> local).
"""
from __future__ import annotations

import shutil
import subprocess
from collections import Counter
from dataclasses import dataclass, field

FIELDS = ("frame.time_epoch", "ip.src", "ip.dst", "udp.srcport", "udp.dstport", "udp.length",
          "rtp.p_type", "rtp.seq", "rtp.timestamp", "rtp.marker", "rtp.ssrc", "rtcp.pt", "rtcp.rtpfb.fmt")


@dataclass
class Pkt:
    t: float
    src: str
    dst: str
    udplen: int
    pt: int | None
    seq: int | None
    rtpts: int | None
    marker: bool
    ssrc: str
    rtcp_pt: tuple[str, ...]
    fb_fmt: tuple[str, ...]

    @property
    def is_nack(self) -> bool:
        """RTPFB (205) generic NACK (fmt 1). TWCC is 205 fmt 15 and is not a NACK."""
        return "205" in self.rtcp_pt and "1" in self.fb_fmt


def tshark_cmd(pcap) -> list[str]:
    cmd = ["tshark", "-n", "-r", str(pcap), "-o", "rtp.heuristic_rtp:TRUE", "-Y", "udp",
           "-T", "fields", "-E", "separator=/t", "-E", "occurrence=a", "-E", "aggregator=;"]
    for f in FIELDS:
        cmd += ["-e", f]
    return cmd


def _first_int(s: str) -> int | None:
    if not s:
        return None
    return int(s.split(";")[0])


def parse_lines(lines) -> list[Pkt]:
    out = []
    for line in lines:
        f = line.rstrip("\n").split("\t")
        if len(f) < 13 or not f[0] or not f[5]:
            continue
        pt = _first_int(f[6])
        out.append(Pkt(t=float(f[0]), src=f[1], dst=f[2], udplen=int(f[5].split(";")[0]), pt=pt,
                       seq=_first_int(f[7]) if pt is not None else None,
                       rtpts=_first_int(f[8]) if pt is not None else None,
                       marker=f[9].split(";")[0] in ("1", "True"), ssrc=f[10].split(";")[0],
                       rtcp_pt=tuple(x for x in f[11].split(";") if x),
                       fb_fmt=tuple(x for x in f[12].split(";") if x)))
    return out


def extract(pcap) -> list[Pkt]:
    if shutil.which("tshark") is None:
        raise RuntimeError("tshark not found on PATH")
    r = subprocess.run(tshark_cmd(pcap), capture_output=True, text=True)
    if r.returncode != 0 and not r.stdout:
        raise RuntimeError(f"tshark failed on {pcap}: {r.stderr.strip()[:300]}")
    return parse_lines(r.stdout.splitlines())


@dataclass
class Flow:
    local: str | None = None
    sfu: str | None = None
    video_pt: int | None = None
    pt_counts: dict = field(default_factory=dict)


def find_flow(pkts: list[Pkt], role: str) -> Flow:
    """role 'a' = sender (video local -> SFU); 'b' = receiver (video SFU -> local)."""
    pairs = Counter((p.src, p.dst) for p in pkts if p.pt is not None)
    if not pairs:
        return Flow()
    (src, dst), _ = pairs.most_common(1)[0]
    local, sfu = (src, dst) if role == "a" else (dst, src)
    fwd = (local, sfu) if role == "a" else (sfu, local)
    ptc = Counter(p.pt for p in pkts if p.pt is not None and (p.src, p.dst) == fwd)
    vpt = ptc.most_common(1)[0][0] if ptc else None
    return Flow(local=local, sfu=sfu, video_pt=vpt, pt_counts={str(k): v for k, v in ptc.items()})
