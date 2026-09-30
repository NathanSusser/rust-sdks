"""Screenshots and per-frame QP from B's subscriber (Host A only; numpy + matplotlib).

Inputs, all optional (an older subscriber writes neither):
  hostb/frames/index.csv       frame_id,capture_timestamp_us,width,height,stride_y,stride_u,stride_v,bytes_written
  hostb/frames/<id:08>.i420    raw decoded I420, planes back to back (Y, U, V) at the index strides
  hostb/frames-qp.csv          the decoder's per-frame log (LK_DECODER_FRAME_LOG):
                               rtp_timestamp,frame_id,capture_timestamp_us,qp,width,height,decode_ms,codec,implementation
                               qp is empty where the decoder does not expose it (h265)

Outputs:
  reduced/screens/<frame_id>.png   BT.601 limited-range RGB
  reduced/screens.csv              SCREEN_COLS, one row per PNG written
  reduced/frames.csv `qp`          via attach_qp(), called by reduce_cell before frames.csv is written

The per-frame QP is the bitstream's (what the decoder parsed), so it is the encoder's QP for
that frame. H.264/H.265 are 0-51; AV1 is the 0-255 q-index. Compare only within a codec.
"""
from __future__ import annotations

import bisect
import csv
import math
from pathlib import Path

INDEX_NAME = "index.csv"
QP_LOG = "frames-qp.csv"
SCREEN_COLS = ("frame_id", "t_s", "png", "width", "height", "qp", "qp_join", "bytes", "owd", "e2e")
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


# ---------------------------------------------------------------- the decoder's per-frame log
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

    def lookup(self, frame_id=None, capture_us=None, rtp_ts=None) -> tuple[float | None, str]:
        """(qp, how): by frame_id, else the nearest capture timestamp within CAPTURE_TOL_US,
        else an exact RTP timestamp. ('', None) when nothing matches."""
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
    """None when the file is absent or has no header (a subscriber that ignores LK_DECODER_FRAME_LOG)."""
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


# ---------------------------------------------------------------- I420 -> RGB
def plane_layout(width: int, height: int, stride_y: int | None, stride_u: int | None, stride_v: int | None,
                 nbytes: int) -> tuple[int, int, int]:
    """Strides that account for exactly `nbytes`. The index's strides first; if they do not add up
    (the writer records w, w/2, w/2 while the decoder's planes may be padded), infer one luma
    stride with chroma at half of it. Raises ValueError when nothing fits."""
    ch = (height + 1) // 2
    cw = (width + 1) // 2
    sy, su, sv = stride_y or width, stride_u or cw, stride_v or cw
    if sy * height + (su + sv) * ch == nbytes and sy >= width and min(su, sv) >= cw:
        return sy, su, sv
    # luma stride s, chroma stride s/2 (rounded up): s*h + 2*ceil(s/2)*ch == nbytes
    for s in range(width, width + 257):
        c = (s + 1) // 2
        if s * height + 2 * c * ch == nbytes:
            return s, c, c
    raise ValueError(f"{nbytes} bytes do not fit {width}x{height} I420 at strides {stride_y}/{stride_u}/{stride_v}")


def i420_to_rgb(buf: bytes, width: int, height: int, stride_y=None, stride_u=None, stride_v=None):
    """Raw I420 -> HxWx3 uint8, BT.601 limited range (Y 16..235, C 16..240), chroma
    upsampled by repetition."""
    import numpy as np  # noqa: PLC0415

    sy, su, sv = plane_layout(width, height, stride_y, stride_u, stride_v, len(buf))
    ch, cw = (height + 1) // 2, (width + 1) // 2
    a = np.frombuffer(buf, dtype=np.uint8)
    o_u = sy * height
    o_v = o_u + su * ch
    y = a[:o_u].reshape(height, sy)[:, :width].astype(np.float32)
    u = a[o_u:o_v].reshape(ch, su)[:, :cw].astype(np.float32)
    v = a[o_v:o_v + sv * ch].reshape(ch, sv)[:, :cw].astype(np.float32)
    u = u.repeat(2, axis=0).repeat(2, axis=1)[:height, :width] - 128.0
    v = v.repeat(2, axis=0).repeat(2, axis=1)[:height, :width] - 128.0
    yy = 1.164383 * (y - 16.0)
    r = yy + 1.596027 * v
    g = yy - 0.391762 * u - 0.812968 * v
    b = yy + 2.017232 * u
    return np.clip(np.stack([r, g, b], axis=-1) + 0.5, 0, 255).astype(np.uint8)


def write_png(path: Path, rgb) -> None:
    import matplotlib  # noqa: PLC0415
    matplotlib.use("Agg")
    import matplotlib.image as mimg  # noqa: PLC0415
    tmp = Path(path).with_suffix(".png.tmp")
    mimg.imsave(tmp, rgb, format="png")
    tmp.replace(path)


# ---------------------------------------------------------------- the step reduce_cell calls
def _read_index(frames_dir: Path) -> dict[int, dict]:
    p = frames_dir / INDEX_NAME
    out: dict[int, dict] = {}
    if not p.is_file():
        return out
    with p.open(newline="", encoding="utf-8", errors="replace") as f:
        for r in csv.DictReader(f):
            fid = _int(r.get("frame_id"))
            if fid is None:
                continue
            out[fid] = {k: _int(r.get(k)) for k in ("capture_timestamp_us", "width", "height", "stride_y",
                                                    "stride_u", "stride_v", "bytes_written")}
    return out


def _read_frames_csv(path: Path) -> dict[int, dict]:
    out: dict[int, dict] = {}
    if not path.is_file():
        return out
    with path.open(newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            fid = _int(r.get("frame_id"))
            if fid is not None:
                out[fid] = r
    return out


def reduce_screens(cell: Path, epoch: float, manifest: dict | None = None, notes: list | None = None) -> int:
    """hostb/frames/*.i420 -> reduced/screens/*.png + reduced/screens.csv. Returns the number of
    PNGs written (0, and no screens.csv, when hostb/frames/ is absent or empty). A frame that
    cannot be converted is skipped with a note; the rest still are."""
    cell = Path(cell)
    notes = notes if notes is not None else []
    fdir = cell / "hostb" / "frames"
    files = sorted(fdir.glob("*.i420")) if fdir.is_dir() else []
    if not files:
        return 0
    out = cell / "reduced"
    sdir = out / "screens"
    sdir.mkdir(parents=True, exist_ok=True)
    index = _read_index(fdir)
    qlog = read_qp_log(cell / "hostb" / QP_LOG)
    frames = _read_frames_csv(out / "frames.csv")
    req = (manifest or {}).get("requested") or {}
    rows = []
    for f in files:
        fid = _int(f.stem)
        if fid is None:
            continue
        ix = index.get(fid) or {}
        q = (qlog.by_id.get(fid) if qlog else None) or {}
        fr = frames.get(fid) or {}
        w = ix.get("width") or q.get("width") or _int(req.get("width"))
        h = ix.get("height") or q.get("height") or _int(req.get("height"))
        if not (w and h):
            notes.append(f"screens: {f.name} has no index row and no known size; skipped")
            continue
        try:
            rgb = i420_to_rgb(f.read_bytes(), w, h, ix.get("stride_y"), ix.get("stride_u"), ix.get("stride_v"))
        except ValueError as e:
            notes.append(f"screens: {f.name}: {e}; skipped")
            continue
        png = sdir / f"{fid}.png"
        write_png(png, rgb)
        cap = ix.get("capture_timestamp_us") or q.get("capture_us") or _int(fr.get("capture_us"))
        qp, how = qlog.lookup(fid, cap, _int(fr.get("rtp_ts"))) if qlog else (None, "")
        rows.append({"frame_id": fid, "t_s": (cap / 1e6 - epoch) if cap else None,
                     "png": png.relative_to(out).as_posix(), "width": w, "height": h, "qp": qp, "qp_join": how,
                     "bytes": _int(fr.get("bytes")), "owd": _float(fr.get("owd")), "e2e": _float(fr.get("e2e"))})
    from .frames import write_csv  # noqa: PLC0415
    write_csv(out / "screens.csv", SCREEN_COLS, rows)
    return len(rows)
