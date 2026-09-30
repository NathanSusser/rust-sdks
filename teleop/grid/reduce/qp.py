"""Per-frame QP from B's decoder log (Host A only; standard library).

Input, optional (an older subscriber writes none):
  hostb/frames-qp.csv   the decoder's per-frame log (LK_DECODER_FRAME_LOG):
                        rtp_timestamp,frame_id,capture_timestamp_us,qp,width,height,decode_ms,codec,implementation
                        qp is empty where the decoder does not expose it

Output:
  reduced/frames.csv `qp`   via attach_qp(), called by reduce_cell before frames.csv is written
  reduce.json["qp_log"]      summary() of the whole log, so metrics never re-reads the raw file

The per-frame QP is the bitstream's (what the decoder parsed), so it is the encoder's QP for
that frame. H.264/H.265 are 0-51; AV1 is the 0-255 q-index. Compare only within a codec.
"""
from __future__ import annotations

import bisect
import csv
import math
from collections import Counter
from pathlib import Path

from ..stats import summ

QP_LOG = "frames-qp.csv"
# A frames-qp.csv row with no matching frame_id is matched by capture time, within this
# (half a frame at 30 fps: never the neighbouring frame).
CAPTURE_TOL_US = 15_000


def _int(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _float(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _qp(v):
    """QP is an integer on every codec; keep it one so the tables do not print 31.000."""
    f = _float(v)
    return int(f) if f is not None and f.is_integer() else f


class QpLog:
    """frames-qp.csv, indexed three ways. Rows whose qp is empty are kept (they still carry
    size and decode time) but never answer a qp lookup."""

    def __init__(self, rows: list[dict]):
        self.rows = rows
        self.by_id = {r["frame_id"]: r for r in rows if r["frame_id"] is not None and r["qp"] is not None}
        self.by_rtp = {r["rtp_timestamp"]: r for r in rows if r["rtp_timestamp"] is not None and r["qp"] is not None}
        cap = sorted((r["capture_us"], i) for i, r in enumerate(rows)
                     if r["capture_us"] is not None and r["qp"] is not None)
        self._cap_t = [c for c, _ in cap]
        self._cap_i = [i for _, i in cap]

    def __len__(self) -> int:
        return len(self.rows)

    def qps(self) -> list[float]:
        return [r["qp"] for r in self.rows if r["qp"] is not None]

    def summary(self) -> dict:
        """What metrics needs from the log (encoder.qp_per_frame), computed in the one read."""
        codecs = Counter(r["codec"] for r in self.rows if r["codec"])
        impls = Counter(r["implementation"] for r in self.rows if r["implementation"])
        return {"rows": len(self.rows), "with_qp": len(self.qps()), "qp": summ(self.qps()),
                "codec": codecs.most_common(1)[0][0] if codecs else None,
                "implementation": impls.most_common(1)[0][0] if impls else None}

    def lookup(self, frame_id=None, capture_us=None, rtp_ts=None) -> tuple[float | None, str]:
        """(qp, how): by frame_id, else the nearest capture timestamp within CAPTURE_TOL_US,
        else an exact RTP timestamp. (None, '') when nothing matches."""
        if frame_id is not None:
            r = self.by_id.get(int(frame_id))
            if r is not None:
                return r["qp"], "frame_id"
        if capture_us is not None and self._cap_t:
            j = bisect.bisect_left(self._cap_t, capture_us)
            best = None
            for k in (j - 1, j):
                if 0 <= k < len(self._cap_t):
                    d = abs(self._cap_t[k] - capture_us)
                    if d <= CAPTURE_TOL_US and (best is None or d < best[0]):
                        best = (d, self._cap_i[k])
            if best is not None:
                return self.rows[best[1]]["qp"], "capture"
        if rtp_ts is not None:
            r = self.by_rtp.get(int(rtp_ts))
            if r is not None:
                return r["qp"], "rtp"
        return None, ""


def read_qp_log(path: Path) -> QpLog | None:
    """None when the file is absent or has no header (a subscriber that ignores LK_DECODER_FRAME_LOG).
    Streams the file once, line by line."""
    p = Path(path)
    if not p.is_file():
        return None
    rows = []
    with p.open(newline="", encoding="utf-8", errors="replace") as f:
        rd = csv.DictReader(ln for ln in f if not ln.startswith("#"))
        if not rd.fieldnames:
            return None
        for r in rd:
            rows.append({"rtp_timestamp": _int(r.get("rtp_timestamp")), "frame_id": _int(r.get("frame_id")),
                         "capture_us": _int(r.get("capture_timestamp_us")), "qp": _qp(r.get("qp")),
                         "width": _int(r.get("width")), "height": _int(r.get("height")),
                         "decode_ms": _float(r.get("decode_ms")), "codec": (r.get("codec") or "").strip(),
                         "implementation": (r.get("implementation") or "").strip()})
    return QpLog(rows)


def attach_qp(rows: list[dict], log: QpLog | None) -> int:
    """Set row["qp"] on reduce's frame rows (keys frame_id, capture_us, rtp_ts); returns how many
    matched. The RTP fallback assumes the SFU forwards timestamps unchanged; frame_id and
    capture time come first because they do not depend on that."""
    if log is None:
        return 0
    n = 0
    for r in rows:
        qp, _ = log.lookup(r.get("frame_id"), r.get("capture_us"), r.get("rtp_ts"))
        r["qp"] = qp
        n += qp is not None
    return n
