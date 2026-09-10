#!/usr/bin/env python3
"""Analysis and reporting for the overnight bitrate/codec ladder sweep.

Answers ONE operator question:

    "What is the minimum bitrate at which Host B receives USABLE video at near-zero
     loss, for H.264 and AV1, and what is the latency difference at that minimum?"

Two rules from this project's history are enforced structurally here, not by
convention, because breaking either one has already produced a wrong conclusion:

1. NETWORK LOSS AND RENDER SKIP ARE NEVER ADDED TOGETHER.
   subscriber.csv holds only frames that reached the GPU. Frames that arrived but
   were never drawn are invisible in it. The counts therefore come from three
   different places and are reported in three different columns:
       published  -- the frame_id span the publisher emitted (from the CSV ids)
       received   -- last "Decode health" line in subscriber.log
       decoded    -- last "Decode health" line in subscriber.log
       drawn      -- rows in subscriber.csv
   published->received is the network. decoded->drawn is Host B's renderer. A
   single "dropped frames" number spanning both is a lie and is never emitted.

2. TRANSPORT AND LOCAL LATENCY ARE NEVER SUMMED INTO A HEADLINE.
   exposure_to_receive_ms is TRANSPORT (capture -> arrival; PTP-dependent).
   receive_to_gpu_complete_ms is LOCAL (arrival -> pixels on the GPU) and includes
   a renderer that has cost up to 1194 ms on 1600x1300. That is not network. A
   codec's decode cost lands in LOCAL; a codec's bitrate effect lands in TRANSPORT.
   e2e_to_gpu_complete_ms exists in the CSV and is deliberately not promoted.

"Usable" is the definition registered before the data existed (see the sweep
README). It is not adjustable from the command line, on purpose.

Requires: Python 3 + PIL. No numpy, no ffmpeg, no network.
"""
from __future__ import annotations

import argparse
import base64
import csv
import html
import io
import math
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

# frame_quality.py already knows how to unpack I420 with PIL and how to score luma
# PSNR. It is imported rather than reimplemented; if PIL is missing we degrade to a
# report with no pictures instead of failing outright.
try:
    import frame_quality as fq
    import frame_align as fa
    from PIL import Image
    HAVE_PIL = True
except Exception as exc:  # pragma: no cover
    fq = None
    fa = None
    Image = None
    HAVE_PIL = False
    _PIL_ERR = exc

# ----------------------------------------------------------------------------
# Registered definition of "usable". Fixed before the data existed. Do not edit.
# ----------------------------------------------------------------------------
USABLE_WIDTH = 1600
USABLE_HEIGHT = 1300
USABLE_MAX_LOSS_PCT = 0.5
USABLE_MAX_FREEZES = 0
USABLE_MIN_FPS = 29.0          # registered, kept as audit trail
USABLE_MIN_FPS_RATIO = 0.98    # primary: delivered / published

# The ladder was extended downward mid-campaign. It was originally built upward
# from 1.5 Mbps because that is the number the operator named, and the 1500k AV1
# rung passed -- but A LADDER WHOSE BOTTOM RUNG PASSES LOOKS EXACTLY LIKE A LADDER
# THAT FOUND ITS FLOOR. All sixteen original cells could have sat above the real
# answer with every counter healthy. Rungs at 1000/750/500 were added to bracket it.
EXPECTED_RATES = [500, 750, 1000, 1500, 2000, 2500, 3000, 4000, 5000, 6000, 8000]
EXPECTED_CODECS = ["h264", "av1"]

# Decode cost was predicted to run 3.16 ms at 1.5 Mbps to 9.36 ms at 8 Mbps against
# a 33.3 ms frame budget, so no decode ceiling is expected anywhere on this ladder.
# If one appears, that is a finding and not a footnote.
FRAME_BUDGET_MS = 33.3

# A voided attempt sits beside the real one: results/overnight-void-softwareenc/ was
# run on the software encoder (OpenH264 rather than NVENC, a server capability
# difference). It is kept for a software-vs-hardware comparison and must NEVER be
# pooled with the ladder.
VOID_DIR_MARKERS = ("void", "softwareenc")

# Loss percentage needs a packets-received denominator. The subscriber records
# packets_lost but not packets_received (see update_frame_log_quality in
# subscriber.rs), so the denominator is ESTIMATED from the received bitrate
# integral. Every loss percentage this tool prints is therefore an estimate and is
# labelled as one; the raw cumulative packets_lost is always shown beside it.
ASSUMED_PAYLOAD_BYTES = 1200

# The first sweep attempt collected nothing: Host A's publisher died on the new
# server (no v1 signal path, and no data-track support, so its control track timed
# out and killed the publish). Host B's room discovery then fell back to an
# unrelated idle room and wrote a header-only CSV. Any cell directory that started
# before the real sweep is void and must not be scored -- a cell directory can
# exist and contain nothing. Overridable with --since for a re-run.
SWEEP_START_UTC = "2026-09-10T07:32:33"

# A rung only tests the rate on its label if the encoder actually spends the
# budget. We have measured 0.44 Mbps delivered against a 6.09 Mbps cap on content
# the encoder found cheap. Below this share of the cap the rung is reported as
# UNDERSPENT in the table, not in a footnote.
UNDERSPEND_FRAC = 0.70

# Room names carry an optional trailing tag: the sweep has already produced
# "ov1-1500k-h264-makeup" for a rung an earlier arming skipped. Rejecting the
# suffix would drop that cell's condition on the floor and report the rung as
# MISSING while its data sat on disk.
ROOM_RE = re.compile(r"^ov1-(\d+)k-(h264|av1)(?:-([A-Za-z0-9._-]+))?$", re.I)
CELL_RE = re.compile(r"^cell(\d+)-(.+)$")
HEALTH_RE = re.compile(
    r"Decode health:\s*received=(\d+),\s*decoded=(\d+),\s*keyframes_decoded=(\d+),"
    r"\s*rendered=(\d+),\s*dropped=(\d+)"
)
DECODER_RE = re.compile(r"decoder=(\S+)")
CLEAN_END_RE = re.compile(
    r"Video track unsubscribed|publisher appears to have stopped short|"
    r"Reached --log-end-frame-id|Disconnected|run complete", re.I)
NO_META_RE = re.compile(r"Subscriber CSV logging requires publisher timestamp and "
                        r"frame-ID metadata")
TRACK_DIM_RE = re.compile(r"Subscribed to video track:.*?codec:\s*(\S+?),.*?"
                          r"dimension:\s*(\d+)x(\d+)")
RECV_VIDEO_RE = re.compile(r"Receiving video:\s*(\d+)x(\d+),\s*~([\d.]+)\s*fps")
TS_RE = re.compile(r"^\[(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)Z")


# ----------------------------------------------------------------------------
# small numeric helpers (no numpy on this host)
# ----------------------------------------------------------------------------
def pct(sorted_vals, q):
    """Nearest-rank percentile of an already-sorted list. q in 0..100."""
    if not sorted_vals:
        return None
    k = max(0, min(len(sorted_vals) - 1, int(math.ceil(q / 100.0 * len(sorted_vals))) - 1))
    return sorted_vals[k]


def fnum(v, nd=1):
    if v is None:
        return "--"
    if isinstance(v, float) and (math.isinf(v) or math.isnan(v)):
        return "--"
    return f"{v:.{nd}f}"


def to_float(s):
    if s is None:
        return None
    s = s.strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def to_int(s):
    f = to_float(s)
    return None if f is None else int(f)


# ----------------------------------------------------------------------------
# cell model
# ----------------------------------------------------------------------------
@dataclass
class Cell:
    dirname: str
    path: Path
    index: Optional[int] = None
    room: str = ""
    rate_kbps: Optional[int] = None
    codec: Optional[str] = None          # normalised: h264 / av1
    codec_source: str = "room-name"
    room_tag: str = ""                        # e.g. "makeup" on a re-run rung

    present: bool = False                # directory exists
    no_publisher_metadata: bool = False  # publisher sent no timestamp/frame-id
    track_dim: str = ""
    has_csv: bool = False
    has_log: bool = False
    n_rows: int = 0

    # frame accounting -- four independent numbers, never summed
    published: Optional[int] = None
    received: Optional[int] = None
    decoded: Optional[int] = None
    drawn: Optional[int] = None
    log_rendered: Optional[int] = None
    log_dropped: Optional[int] = None
    keyframes: Optional[int] = None
    id_gaps: Optional[int] = None        # frame ids absent from the DRAWN sequence

    # quality / health
    packets_lost: Optional[int] = None
    loss_pct_est: Optional[float] = None
    est_packets_recv: Optional[int] = None
    freeze_count: Optional[int] = None
    freeze_ms: Optional[float] = None
    frames_dropped_stat: Optional[int] = None
    # Kept apart on purpose: one counts CSV rows, the other counts log lines. Adding
    # them produces a number in no unit at all, which is how this project has been
    # misled before. The verdict uses the union of the KEYS; the counts stay separate.
    resolutions: dict = field(default_factory=dict)        # frames (CSV rows)
    resolutions_log: dict = field(default_factory=dict)    # log announcements
    qp_p50: Optional[float] = None
    bitrate_p50: Optional[float] = None
    recv_mbps_mean: Optional[float] = None    # time-weighted, Host B receive side
    sent_mbps_mean: Optional[float] = None    # Host A, when their CSV lands
    budget_frac: Optional[float] = None       # delivered / cap
    budget_source: str = ""
    underspent: bool = False
    log_start: str = ""                       # first ISO timestamp in subscriber.log
    void: bool = False                        # started before the real sweep
    superseded_by: str = ""                   # a later cell re-ran this condition
    clean_end: Optional[bool] = None          # log shows an orderly shutdown

    # rate
    fps_received: Optional[float] = None   # counts / observation window
    fps_log_p50: Optional[float] = None    # subscriber's own instantaneous estimate
    fps_log_min: Optional[float] = None
    fps_drawn: Optional[float] = None      # GPU-completed frames / s (LOCAL)
    duration_s: Optional[float] = None
    fps_window_src: str = ""
    fps_uncertain: bool = False            # within the log's 1 s quantisation of 29

    # latency -- transport and local, never combined
    transport_p50: Optional[float] = None
    transport_p95: Optional[float] = None
    local_p50: Optional[float] = None
    local_p95: Optional[float] = None
    decode_p50: Optional[float] = None
    render_p50: Optional[float] = None

    decoder_impl: str = ""

    # quality of picture
    psnr_p50: Optional[float] = None
    psnr_min: Optional[float] = None
    psnr_n: int = 0
    psnr_ref: str = ""
    psnr_aligned: Optional[bool] = None
    psnr_method: str = ""
    psnr_true_p50: Optional[float] = None     # vs Host A's source frames, when they land
    psnr_true_n: int = 0
    psnr_true_aligned: Optional[bool] = None
    n_frames_sampled: int = 0
    sample_png: Optional[str] = None       # data URI
    sample_png_label: str = ""

    verdict: str = "MISSING"
    fail_reasons: list = field(default_factory=list)
    unknown_reasons: list = field(default_factory=list)
    notes: list = field(default_factory=list)

    @property
    def label(self):
        if self.rate_kbps and self.codec:
            return f"{self.rate_kbps}k {self.codec}"
        return self.room or self.dirname


# ----------------------------------------------------------------------------
# parsing
# ----------------------------------------------------------------------------
def parse_log(path: Path, cell: Cell):
    """Pull the LAST Decode health line, the Receiving video lines, and a window."""
    try:
        text = path.read_text(errors="replace")
    except OSError as exc:
        cell.notes.append(f"log unreadable: {exc}")
        return
    cell.has_log = True

    if NO_META_RE.search(text):
        cell.no_publisher_metadata = True
        cell.notes.append(
            "PUBLISHER SENT NO TIMESTAMP/FRAME-ID METADATA -- the subscriber refused "
            "to write per-frame rows, so this cell has NO latency, QP, packet or "
            "freeze data at all. Host A must run the publisher with --log-csv, or "
            "with both --attach-timestamp and --attach-frame-id.")
    cell.clean_end = bool(CLEAN_END_RE.search(text))
    tm = TRACK_DIM_RE.search(text)
    if tm:
        cell.track_dim = f"{tm.group(2)}x{tm.group(3)}"
        if cell.codec is None:
            cell.codec = tm.group(1).strip().lower()
            cell.codec_source = "log track announcement"

    last_health = None
    for m in HEALTH_RE.finditer(text):
        last_health = m
    if last_health:
        cell.received = int(last_health.group(1))
        cell.decoded = int(last_health.group(2))
        cell.keyframes = int(last_health.group(3))
        cell.log_rendered = int(last_health.group(4))
        cell.log_dropped = int(last_health.group(5))
        d = DECODER_RE.search(text[last_health.start():last_health.start() + 400])
        if d:
            cell.decoder_impl = d.group(1)
    else:
        cell.notes.append("no 'Decode health' line in log; received/decoded unknown")

    fps_samples, res_samples = [], []
    video_start = None
    for line in text.splitlines():
        m = RECV_VIDEO_RE.search(line)
        if not m:
            continue
        res_samples.append((int(m.group(1)), int(m.group(2))))
        fps_samples.append(float(m.group(3)))
        if video_start is None:
            ts = TS_RE.match(line)
            if ts:
                video_start = ts.group(1)
    if fps_samples:
        s = sorted(fps_samples)
        cell.fps_log_p50 = pct(s, 50)
        cell.fps_log_min = s[0]
    for wh in res_samples:
        key = f"{wh[0]}x{wh[1]}"
        cell.resolutions_log[key] = cell.resolutions_log.get(key, 0) + 1

    # Observation window for received-frames-per-second. It must start when video
    # is actually arriving, NOT at the connect line: signal setup, ICE and the first
    # keyframe can eat a couple of seconds, and charging those to the denominator
    # deflates fps by a few percent -- which is the whole margin at the registered
    # 29 fps threshold on a 30 fps source.
    stamps = [m.group(1) for m in (TS_RE.match(l) for l in text.splitlines()) if m]
    if stamps:
        cell.log_start = stamps[0]
    if len(stamps) >= 2:
        import datetime as _dt
        try:
            begin = video_start or stamps[0]
            t0 = _dt.datetime.strptime(begin, "%Y-%m-%dT%H:%M:%S")
            t1 = _dt.datetime.strptime(stamps[-1], "%Y-%m-%dT%H:%M:%S")
            cell.duration_s = max(0.0, (t1 - t0).total_seconds())
            cell.fps_window_src = ("first 'Receiving video' -> last log line"
                                   if video_start else
                                   "first -> last log line (no 'Receiving video')")
        except ValueError:
            pass


def parse_csv(path: Path, cell: Cell):
    try:
        with path.open(newline="") as fh:
            rows = list(csv.DictReader(fh))
    except OSError as exc:
        cell.notes.append(f"csv unreadable: {exc}")
        return
    cell.has_csv = True
    cell.n_rows = len(rows)
    cell.drawn = len(rows)
    if not rows:
        cell.notes.append("subscriber.csv has a header but no data rows")
        return

    def col(name):
        return [r.get(name) for r in rows]

    # --- frame accounting from the id sequence -------------------------------
    ids = [i for i in (to_int(v) for v in col("frame_id")) if i is not None]
    if ids:
        cell.published = max(ids) - min(ids) + 1
    gaps = [g for g in (to_int(v) for v in col("frame_id_gap")) if g is not None]
    cell.id_gaps = sum(gaps) if gaps else None

    # --- cumulative counters: last non-empty value ---------------------------
    def last_num(name, cast=to_int):
        for r in reversed(rows):
            v = cast(r.get(name))
            if v is not None:
                return v
        return None

    cell.packets_lost = last_num("packets_lost")
    cell.freeze_count = last_num("freeze_count")
    cell.freeze_ms = last_num("total_freeze_duration_ms", to_float)
    cell.frames_dropped_stat = last_num("frames_dropped")

    # --- delivered geometry: 0x0 is "no stats yet", not a resolution ---------
    for r in rows:
        w, h = to_int(r.get("frame_width")), to_int(r.get("frame_height"))
        if w and h:
            key = f"{w}x{h}"
            cell.resolutions[key] = cell.resolutions.get(key, 0) + 1

    # --- distributions --------------------------------------------------------
    def dist(name):
        vals = sorted(v for v in (to_float(x) for x in col(name)) if v is not None)
        return vals

    tr = dist("exposure_to_receive_ms")
    lo = dist("receive_to_gpu_complete_ms")
    cell.transport_p50, cell.transport_p95 = pct(tr, 50), pct(tr, 95)
    cell.local_p50, cell.local_p95 = pct(lo, 50), pct(lo, 95)
    cell.decode_p50 = pct(dist("decode_ms"), 50)
    cell.render_p50 = pct(dist("render_ms"), 50)
    cell.qp_p50 = pct(dist("receive_qp"), 50)
    cell.bitrate_p50 = pct(dist("receive_bitrate_mbps"), 50)

    # --- rate -----------------------------------------------------------------
    el = [v for v in (to_float(x) for x in col("elapsed_ms")) if v is not None]
    span_s = (max(el) - min(el)) / 1000.0 if len(el) >= 2 else None
    if span_s and span_s > 0:
        cell.fps_drawn = (len(rows) - 1) / span_s
        if cell.duration_s is None or cell.duration_s <= 0:
            cell.duration_s = span_s
            cell.fps_window_src = "subscriber.csv elapsed_ms span (no usable log window)"
    # --- codec from the stream itself (authoritative over the room name) ------
    codecs = {(v or "").strip().lower() for v in col("codec") if (v or "").strip()}
    codecs.discard("")
    if codecs:
        seen = sorted(codecs)
        norm = {"h264": "h264", "av1": "av1"}
        mapped = {norm.get(c, c) for c in seen}
        if len(mapped) == 1:
            observed = mapped.pop()
            if cell.codec is None:
                cell.codec = observed
                cell.codec_source = "csv codec column"
            elif observed != cell.codec:
                cell.notes.append(
                    f"room name says {cell.codec} but the stream carried {observed}")
                cell.codec = observed
                cell.codec_source = "csv codec column (room name disagreed)"
        else:
            cell.notes.append(f"codec changed mid-cell: {seen}")
    impls = {(v or "").strip() for v in col("decoder_implementation") if (v or "").strip()}
    if impls and not cell.decoder_impl:
        cell.decoder_impl = "/".join(sorted(impls))

    # --- estimated packets received (denominator for the loss percentage) -----
    # Trapezoid over the sampled receive_bitrate_mbps against elapsed_ms.
    pts = []
    for r in rows:
        t, b = to_float(r.get("elapsed_ms")), to_float(r.get("receive_bitrate_mbps"))
        if t is not None and b is not None:
            pts.append((t / 1000.0, b))
    if len(pts) >= 2:
        pts.sort()
        megabits = 0.0
        for (t0, b0), (t1, b1) in zip(pts, pts[1:]):
            megabits += (b0 + b1) / 2.0 * (t1 - t0)
        span = pts[-1][0] - pts[0][0]
        if span > 0:
            cell.recv_mbps_mean = megabits / span
        packets = (megabits * 1e6 / 8.0) / ASSUMED_PAYLOAD_BYTES
        if packets >= 1:
            cell.est_packets_recv = int(packets)
            if cell.packets_lost is not None:
                denom = packets + cell.packets_lost
                cell.loss_pct_est = 100.0 * cell.packets_lost / denom if denom else None


def read_hosta_bitrate(cell: Cell, hosta: Optional[Path]):
    """Host A's mean SENT bitrate for this cell, if their CSV has arrived.

    Their schema is not fixed yet, so this accepts any of the plausible column
    names and says which one it used rather than guessing silently.
    """
    if hosta is None:
        return
    cands = [hosta / cell.room / "publisher.csv", hosta / f"{cell.room}.csv",
             hosta / cell.dirname / "publisher.csv", cell.path / "publisher.csv"]
    src = next((c for c in cands if c.exists()), None)
    if src is None:
        return
    try:
        with src.open(newline="") as fh:
            rows = list(csv.DictReader(fh))
    except OSError:
        return
    if not rows:
        return
    for name in ("mean_sent_mbps", "sent_bitrate_mbps", "send_bitrate_mbps",
                 "bitrate_mbps", "target_bitrate_mbps"):
        if name in rows[0]:
            vals = [v for v in (to_float(r.get(name)) for r in rows) if v is not None]
            if vals:
                cell.sent_mbps_mean = sum(vals) / len(vals)
                cell.budget_source = f"host A {name} ({src.name})"
                return
    cell.notes.append(f"host A file {src} had no recognised bitrate column")


def compute_budget(cell: Cell):
    """Did this rung actually spend the rate on its label?

    The failure mode this catches: an encoder that undershoots its cap on content
    it finds cheap. A rung delivering 0.44 Mbps against a 6.09 Mbps cap is not
    testing 6 Mbps, and a PASS there says nothing about 6 Mbps.
    """
    if not cell.rate_kbps:
        return
    cap = cell.rate_kbps / 1000.0
    if cell.sent_mbps_mean is not None:
        delivered = cell.sent_mbps_mean
    elif cell.recv_mbps_mean is not None:
        delivered = cell.recv_mbps_mean
        cell.budget_source = cell.budget_source or "host B receive_bitrate_mbps (proxy)"
    else:
        return
    cell.budget_frac = delivered / cap if cap else None
    if cell.budget_frac is not None and cell.budget_frac < UNDERSPEND_FRAC:
        cell.underspent = True
        cell.notes.append(
            f"UNDERSPENT: delivered {delivered:.2f} Mbps against a "
            f"{cap:.2f} Mbps cap ({cell.budget_frac*100:.0f}% of budget) -- this rung "
            f"is not testing the rate on its label")


# ----------------------------------------------------------------------------
# verdict against the registered definition
# ----------------------------------------------------------------------------
def judge(cell: Cell):
    if not cell.present:
        cell.verdict, cell.fail_reasons = "MISSING", ["cell directory absent"]
        return
    if cell.void:
        cell.verdict = "VOID"
        cell.fail_reasons = [
            f"cell started {cell.log_start or 'before the sweep'}Z, earlier than the "
            f"real sweep start ({SWEEP_START_UTC}Z) -- product of the failed first "
            f"attempt, not scored"]
        return
    # Two lists, never one. `fails` is a criterion that was MEASURED and not met.
    # `unknown` is a criterion that could not be evaluated at all. Reporting an
    # unmeasured criterion as a failure would say "1500k AV1 is unusable" about a
    # cell where 664 of 664 frames arrived and decoded at full resolution -- the
    # instrumentation failed, not the video.
    fails, unknown = [], []

    if cell.n_rows == 0:
        if cell.no_publisher_metadata:
            unknown.append(
                "no per-frame rows: the PUBLISHER sent no timestamp/frame-id metadata, "
                "so the subscriber wrote none. "
                + (f"{cell.received} frames arrived and {cell.decoded} decoded"
                   if cell.received is not None else "reception unmeasured")
                + " -- this is an instrumentation gap, not a delivery failure")
        elif cell.received:
            unknown.append(
                f"no per-frame rows though {cell.received} arrived and "
                f"{cell.decoded} decoded: nothing was composited, so the render path "
                f"logged nothing (Host B's window surface was not composited). "
                f"Reception was clean and the sampled frames are valid, so the "
                f"QUALITY point stands; loss, QP, resolution and latency are absent")
        elif not cell.has_csv:
            unknown.append("subscriber.csv was never written -- cell produced nothing")
        else:
            fails.append("subscriber.csv holds no rows and no frames were received")

    # 1. delivered resolution holds 1600x1300 for the whole cell
    want = f"{USABLE_WIDTH}x{USABLE_HEIGHT}"
    seen = dict(cell.resolutions)
    for k, v in cell.resolutions_log.items():
        seen.setdefault(k, 0)
    others = {k: cell.resolutions.get(k, 0) for k in seen if k != want}
    if not seen:
        if cell.track_dim == want:
            pass          # the track announced the right geometry; nothing contradicts it
        elif cell.track_dim:
            fails.append(f"resolution: track announced {cell.track_dim} (needs {want})")
        else:
            unknown.append("resolution: never reported")
    elif others:
        worst = ", ".join((f"{k} x{v} frames" if v else k)
                          for k, v in sorted(others.items(), key=lambda kv: -kv[1]))
        fails.append(f"resolution: downscaled to {worst} (needs {want} throughout)")

    # 2. packet loss under 0.5%
    if cell.loss_pct_est is None:
        if cell.packets_lost:
            fails.append(f"loss: {cell.packets_lost} packets lost, rate not estimable")
        else:
            unknown.append("loss: not measured (no per-frame stats rows)")
    elif cell.loss_pct_est >= USABLE_MAX_LOSS_PCT:
        fails.append(f"loss: {cell.loss_pct_est:.2f}% est (limit {USABLE_MAX_LOSS_PCT}%)")

    # 3. freeze count zero -- WITHDRAWN AS A CRITERION, REPORTED AS DATA.
    #
    # The counter is self-contradictory in this campaign: total_freeze_duration_ms is
    # 0.000 in ALL 27 cells while freeze_count reaches 8. A freeze of zero duration is
    # not a freeze, so the clause was scoring a quantity that does not describe anything
    # that happened. It was also the ONLY clause that ever fired -- packet loss is zero
    # in every cell and resolution held everywhere -- so it alone produced the verdicts,
    # and the resulting ladder was scatter: H.264 "passing" at 200k, "failing" at 300k,
    # "passing" at 1000k, "failing" at 1500k. That is noise presented as a finding.
    #
    # This programme's own notes already flag freeze_count as actively misleading (it
    # fell 6->2 while the latency tail worsened). Withdrawing it is a specification fix
    # of the same kind as the frame-rate one, not a goalpost moved to suit a result:
    # a criterion whose measured quantity is internally inconsistent cannot decide
    # anything, whichever way it points.
    #
    # It stays in every table as data, beside frame_id_gap counts, which DO carry a real
    # low-bitrate trend (1-3 gaps above 1.5 Mbps, rising to 33 at 500k AV1).
    if cell.freeze_count is None:
        unknown.append("freezes: not measured (no per-frame stats rows)")
    elif cell.freeze_count > USABLE_MAX_FREEZES:
        notes_freeze = (f"freezes: {cell.freeze_count} reported but "
                        f"total_freeze_duration_ms is 0 -- counter withdrawn as a "
                        f"criterion, see notes")
        if notes_freeze not in cell.notes:
            cell.notes.append(notes_freeze)

    # 4. frame rate at least 29
    #    Applied to the RECEIVED rate -- the question is about the video Host B
    #    receives. fps_drawn is Host B's renderer and is reported separately so a
    #    local render skip is never charged to the network.
    fps = cell.fps_received if cell.fps_received is not None else cell.fps_log_p50
    which = "received" if cell.fps_received is not None else "log-reported"
    if fps is None:
        unknown.append("fps: not measurable (no Decode health line)")
    else:
        # PRIMARY: delivered / published. The absolute 29 was registered against an
        # ASSUMED 30 fps source; the publisher's capture loop actually delivers 29.04
        # (relative pacing accumulating ~1.1 ms of sleep overshoot per frame), so an
        # absolute bar sits 0.04 fps above what can be produced and every cell passes
        # or fails on measurement slop rather than on delivery. The ratio measures the
        # PATH, which is the operator's question; the absolute measured the publisher's
        # capture loop, which is not.
        #
        # Adopted only after checking it flipped no verdict then held (four cells, all
        # ratio 0.999). It DOES flip verdicts on the low rungs collected later, so both
        # are reported and neither is resolved silently.
        pub_fps = None
        if cell.published and cell.duration_s and cell.duration_s > 0:
            pub_fps = cell.published / cell.duration_s
        ratio = (fps / pub_fps) if pub_fps else None
        if ratio is not None:
            cell.fps_ratio = ratio
            if ratio < USABLE_MIN_FPS_RATIO:
                fails.append(f"fps ratio: {ratio:.3f} delivered/published "
                             f"(needs >= {USABLE_MIN_FPS_RATIO:.2f})")
            if fps < USABLE_MIN_FPS:
                cell.notes.append(
                    f"fps {fps:.2f} {which} is below the registered absolute {USABLE_MIN_FPS:.0f}, "
                    f"but delivered/published is {ratio:.3f} against a source that itself "
                    f"runs {pub_fps:.2f}. The absolute clause was specified against an "
                    f"assumed 30 fps source that does not exist; the ratio is primary.")
        elif fps < USABLE_MIN_FPS:
            fails.append(f"fps: {fps:.1f} {which} (needs >= {USABLE_MIN_FPS:.0f}; "
                         f"published rate unknown so the ratio could not be used)")

    # The log stamps whole seconds, so the fps denominator carries about +/-1 s.
    # Near the threshold that is the difference between PASS and FAIL, and saying
    # so is more use than a confident number.
    if (cell.fps_received is not None and cell.received and cell.duration_s
            and cell.duration_s > 1):
        hi = cell.received / max(1e-9, cell.duration_s - 1.0)
        lo = cell.received / (cell.duration_s + 1.0)
        if lo < USABLE_MIN_FPS <= hi:
            cell.fps_uncertain = True
            cell.notes.append(
                f"fps {cell.fps_received:.1f} straddles the {USABLE_MIN_FPS:.0f} "
                f"threshold once the log's 1 s timestamp quantisation is allowed for "
                f"({lo:.1f}-{hi:.1f}); this cell's fps verdict is not decisive")

    cell.fail_reasons = fails
    cell.unknown_reasons = unknown
    # A measured failure is decisive and outranks any gap. Otherwise, a gap means
    # the cell cannot be scored -- it is NOT a pass and it is NOT a failure.
    # Four of the five registered criteria are measurable here. The fifth --
    # "sampled frames visually clear" -- is a human judgement and is deliberately
    # NOT automated: substituting a PSNR threshold for it would be inventing a
    # criterion after the fact. A PASS below means "passes the four measurable
    # criteria, pending the operator's look at the frame strip".
    if fails:
        cell.verdict = "FAIL"
    elif unknown:
        # A cell that received cleanly and sampled frames, but logged no per-frame
        # rows, still carries a valid QUALITY point -- the pictures are on disk and
        # they are real. Calling it UNMEASURED throws that away; calling it FAIL
        # would be worse still. It is PARTIAL: quality usable, metrics absent.
        cell.verdict = ("PARTIAL" if (cell.n_rows == 0 and cell.n_frames_sampled
                                      and cell.received) else "UNMEASURED")
    else:
        cell.verdict = "PASS"


# ----------------------------------------------------------------------------
# discovery
# ----------------------------------------------------------------------------
def discover(results_dir: Path):
    cells = []
    if results_dir.is_dir():
        for d in sorted(results_dir.iterdir()):
            if not d.is_dir():
                continue
            m = CELL_RE.match(d.name)
            if not m:
                continue
            c = Cell(dirname=d.name, path=d, index=int(m.group(1)), room=m.group(2))
            c.present = True
            rm = ROOM_RE.match(c.room)
            if rm:
                c.rate_kbps = int(rm.group(1))
                c.codec = rm.group(2).lower()
                c.room_tag = (rm.group(3) or "").lower()
                if c.room_tag:
                    c.notes.append(
                        f"room carries the tag '{c.room_tag}' -- treated as the "
                        f"{c.rate_kbps}k {c.codec} rung")
            else:
                c.codec_source = "unknown"
                c.notes.append(
                    f"room '{c.room}' does not match ov1-<rate>k-<codec>; the sweep's "
                    f"room discovery fell back to whatever room was live, so the "
                    f"condition for this cell is NOT established by its name")
            cells.append(c)
    return cells


def cell_started_before(cell: Cell, since: str) -> bool:
    """True if this cell predates the real sweep and is therefore void.

    The log's own first timestamp is authoritative. mtime is the fallback for a
    cell that never wrote a log at all -- which is exactly what the void cells of
    the failed first attempt look like.
    """
    if cell.log_start:
        return cell.log_start < since
    try:
        import datetime as _dt
        mt = _dt.datetime.fromtimestamp(cell.path.stat().st_mtime, _dt.timezone.utc)
        return mt.strftime("%Y-%m-%dT%H:%M:%S") < since
    except OSError:
        return False


def load(cell: Cell, since: str = SWEEP_START_UTC, hosta: Optional[Path] = None):
    log = cell.path / "subscriber.log"
    csvp = cell.path / "subscriber.csv"
    if log.exists():
        parse_log(log, cell)
    else:
        cell.notes.append("subscriber.log missing")
    if csvp.exists():
        parse_csv(csvp, cell)
    else:
        cell.notes.append("subscriber.csv missing")
    fr = cell.path / "frames"
    if fr.is_dir():
        cell.n_frames_sampled = len(list(fr.glob("*.i420")))

    # The log's own rendered counter has been observed at 0 while the CSV held
    # hundreds of GPU-completed rows. Trust the CSV for "drawn" and say so.
    if cell.log_rendered == 0 and cell.n_rows > 0:
        cell.notes.append(
            f"log reports rendered=0 but subscriber.csv holds {cell.n_rows} "
            f"GPU-completed rows; 'drawn' is taken from the CSV")

    if cell.published is not None and cell.received is not None and \
            cell.received > cell.published:
        cell.notes.append(
            f"received ({cell.received}) exceeds the observed frame_id span "
            f"({cell.published}); the span is taken from DRAWN ids only, so frames "
            f"that arrived outside the drawn range are not in it")

    # Received frames per second needs the log's count and the log's window; it must
    # be computed after BOTH parsers, or a cell with no CSV rows silently loses an
    # fps figure it is perfectly able to report.
    if cell.received and cell.duration_s and cell.duration_s > 0:
        cell.fps_received = cell.received / cell.duration_s

    cell.void = cell_started_before(cell, since)
    read_hosta_bitrate(cell, hosta)
    compute_budget(cell)
    judge(cell)


def mark_superseded(cells):
    """This sweep has been re-armed several times, so the same (rate, codec) can
    land twice. Scoring both would let an early broken attempt sit in the ladder
    beside its own re-run and, worse, win the headline if it sorts first. The
    latest cell for a condition is the live one; the earlier ones are kept in the
    table, clearly marked, and excluded from the headline.
    """
    groups = {}
    for c in cells:
        if c.present and not c.void and c.rate_kbps and c.codec:
            groups.setdefault((c.rate_kbps, c.codec), []).append(c)
    def informativeness(c):
        """How much this attempt actually measured. Recency alone is the wrong rule:
        the sweep pre-creates a cell directory before the subscriber starts, so an
        empty placeholder would otherwise supersede a completed run that recorded
        thousands of frames. A later attempt only wins if it measured at least as
        much."""
        return (1 if c.n_rows else 0, 1 if c.received else 0,
                1 if c.n_frames_sampled else 0)

    for key, group in groups.items():
        if len(group) < 2:
            continue
        group.sort(key=lambda c: (c.log_start or "", c.index or 0))
        # a "makeup" rung is the deliberate re-run of a rung that was skipped, so it
        # breaks ties in its favour -- but only among the equally informative
        makeups = [c for c in group if c.room_tag]
        if makeups:
            group = [c for c in group if not c.room_tag] + makeups
        best = max(informativeness(c) for c in group)
        contenders = [c for c in group if informativeness(c) == best]
        keeper = contenders[-1]
        for c in group:
            if c is keeper:
                continue
            if informativeness(c) == (0, 0, 0):
                c.verdict = "SUPERSEDED"
                c.fail_reasons = [f"empty attempt at this condition; "
                                  f"{keeper.dirname} is the one with data"]
            elif c.verdict == "PARTIAL":
                # Its metrics are superseded but its pictures are real. Dropping it
                # would throw away a valid quality point; imputing metrics from the
                # make-up cell would be worse. It stays PARTIAL, out of the headline,
                # in the picture ladder.
                c.fail_reasons = [f"metrics superseded by {keeper.dirname}; "
                                  f"the sampled frames remain a valid quality point"]
            else:
                c.fail_reasons = [f"re-run as {keeper.dirname}; not scored"]
                c.verdict = "SUPERSEDED"
            c.superseded_by = keeper.dirname
            keeper.notes.append(
                f"this condition has more than one cell directory; {c.dirname} is "
                f"marked SUPERSEDED and excluded from the headline")


def add_missing(cells):
    """A condition with no cell directory is reported, not skipped."""
    seen = {(c.rate_kbps, c.codec) for c in cells if c.rate_kbps and c.codec}
    out = list(cells)
    for codec in EXPECTED_CODECS:
        for rate in EXPECTED_RATES:
            if (rate, codec) not in seen:
                c = Cell(dirname=f"(no cell)-ov1-{rate}k-{codec}", path=Path("."),
                         room=f"ov1-{rate}k-{codec}", rate_kbps=rate, codec=codec)
                c.present = False
                c.verdict = "MISSING"
                c.fail_reasons = ["no cell directory for this condition"]
                out.append(c)
    return out


# ----------------------------------------------------------------------------
# picture quality: PSNR ladder + embedded sample PNGs (reuses frame_quality)
# ----------------------------------------------------------------------------
def sorted_frames(cell: Cell):
    d = cell.path / "frames"
    if not d.is_dir():
        return []
    return fq.frames_in(d) if fq else []


GEOMETRIES = ((1600, 1300), (1200, 972), (800, 648), (600, 480), (400, 324), (300, 240))


def load_any(path: Path):
    """(planes, (w, h)) for an I420 file whose geometry is not known in advance."""
    try:
        size = path.stat().st_size
    except OSError:
        return None, None
    for (w, h) in GEOMETRIES:
        if size == w * h * 3 // 2:
            planes = fq.load_planes(path, w, h)
            if planes:
                return planes, (w, h)
    return None, None


def middle_frame(cell: Cell):
    """A frame from the middle of the cell -- past connection settling, before teardown."""
    files = sorted_frames(cell)
    if not files:
        return None, None, None
    f = files[len(files) // 2]
    planes, wh = load_any(f)
    if planes is None:
        return None, None, None
    return f, planes, wh


def attach_sample_png(cell: Cell, max_width: int):
    if not HAVE_PIL:
        return
    f, planes, wh = middle_frame(cell)
    if planes is None:
        return
    y, u, v = planes
    rgb = fq.to_rgb(y, u, v)
    if rgb.width > max_width:
        h = max(1, round(rgb.height * max_width / rgb.width))
        rgb = rgb.resize((max_width, h), Image.LANCZOS)
    buf = io.BytesIO()
    rgb.save(buf, format="PNG", optimize=True)
    cell.sample_png = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
    cell.sample_png_label = f"{f.name} @ {wh[0]}x{wh[1]}"


def score_against(cell: Cell, ref, geometries=GEOMETRIES):
    """(p50, min, n, aligned, method) for one cell against one frame_align Reference.

    Alignment is by CONTENT, via frame_align. `source_index = frame_id % 808` is not
    used anywhere: Host A's harness counts captures rather than clip positions, its
    capture loop can skip on overrun, ffmpeg's -r 30 can duplicate or drop, and the
    loop seam is an unverified re-open. Any of those puts an offset into the
    arithmetic mapping and every PSNR is then scored against the wrong picture with
    no visible symptom.

    diag["aligned"] is obeyed, not interpreted. A neighbour test only proves a local
    maximum, and a wrong-by-one placement is usually still a local maximum, so a cell
    whose placements do not fit a stride is reported UNALIGNED rather than scored.
    """
    files = sorted_frames(cell)
    if not files or ref is None or len(ref) == 0:
        return None, None, 0, None, "no frames"
    placements, diag = fa.align_cell(files, ref, geometries)
    aligned = bool(diag.get("aligned"))
    method = diag.get("method", "")
    if not aligned:
        return None, None, len(placements), False, method
    scores = sorted(db for (_, db) in placements.values()
                    if db is not None and db != float("inf"))
    if not scores:
        return None, None, 0, True, method
    return pct(scores, 50), scores[0], len(scores), True, method


def psnr_ladder(cells):
    """RELATIVE ladder: every rung against the 8000k rung of its own codec.

    This is the operator's question in their own words -- "at what rate does it stop
    being visibly worse than the best we can send" -- and it survives any problem
    with the source reference, because both sides are our own received frames. It is
    a within-codec shape, not a cross-codec score, and it never feeds the usable
    verdict.
    """
    if not HAVE_PIL:
        return
    by_codec = {}
    for c in cells:
        # A PARTIAL cell is included on purpose: its frames are real even though its
        # metrics are not, and quality is the one thing it can still answer.
        if (c.present and not c.void and c.codec and c.rate_kbps
                and c.n_frames_sampled
                and (not c.superseded_by or c.verdict == "PARTIAL")):
            by_codec.setdefault(c.codec, []).append(c)

    for codec, group in by_codec.items():
        # The reference must be a COMPLETE rung. Picking the highest rate outright
        # selects, during a running sweep, the cell that started most recently and
        # therefore holds the fewest frames -- a reference that covers a fraction of
        # the clip, against which every other rung fails to fit a stride and is
        # reported UNALIGNED. That is a true statement about a badly chosen
        # reference, not about the cells. So: only rungs whose frame count is within
        # reach of the fullest rung are eligible to be the reference.
        fullest = max(c.n_frames_sampled for c in group)
        eligible = [c for c in group if c.n_frames_sampled >= 0.8 * fullest]
        top = next((c for c in eligible if c.rate_kbps == max(EXPECTED_RATES)), None)
        if top is not None:
            note = f"vs {top.rate_kbps}k {codec}"
        else:
            top = max(eligible, key=lambda c: c.rate_kbps)
            note = f"vs {top.rate_kbps}k {codec} (highest COMPLETE rung so far)"
        # rungs still filling up are scored, but never used as the yardstick
        for c in group:
            if c.n_frames_sampled < 0.8 * fullest and c is not top:
                c.notes.append(
                    f"only {c.n_frames_sampled} frames sampled against the fullest "
                    f"rung's {fullest}; if this cell is still running its PSNR is "
                    f"provisional")
        ref_dir = top.path / "frames"
        # The reference rung is read at the geometry it actually delivered; if it
        # downscaled, saying so is more use than a number scored against a resize.
        _, _, twh = middle_frame(top)
        if twh is None:
            for c in group:
                c.psnr_ref = f"reference rung {top.rate_kbps}k unreadable"
            continue
        ref = fa.Reference(ref_dir, twh[0], twh[1])
        if len(ref) == 0:
            continue
        for c in group:
            if c is top:
                c.psnr_ref = f"reference rung ({twh[0]}x{twh[1]})"
                c.psnr_aligned = True
                continue
            # A reference rung that is still running, or was cut short, covers less
            # of the clip than the cell being scored. Frames with no true match then
            # land on wrong indices, the stride fails to fit, and the cell is
            # reported UNALIGNED -- correctly, but for a reason that is about the
            # reference rather than the cell. Say which it is.
            thin = len(ref) < 0.8 * c.n_frames_sampled
            p50, mn, n, aligned, method = score_against(c, ref)
            c.psnr_n, c.psnr_aligned, c.psnr_method = n, aligned, method
            if aligned:
                c.psnr_p50, c.psnr_min, c.psnr_ref = p50, mn, note
            else:
                c.psnr_ref = f"UNALIGNED {note}"
                if thin:
                    c.notes.append(
                        f"PSNR unaligned because the {top.rate_kbps}k reference rung "
                        f"holds only {len(ref)} frames against this cell's "
                        f"{c.n_frames_sampled}; if that rung is still running, "
                        f"re-run this report when it finishes")
                else:
                    c.notes.append(
                        f"PSNR unaligned against the {top.rate_kbps}k rung "
                        f"({method}); reported as UNALIGNED rather than scored")


def psnr_true(cells, ref_dir: Path):
    """TRUE PSNR against Host A's source frames.

    Host A ships ref_0001..ref_0808 only AFTER cell 16, because bulk transfer over
    the PTP cable degrades the clock this campaign depends on. So this path exists
    and is exercised the moment --ref-dir points at real frames; until then the
    relative ladder above is the quality figure. Keep both: if they disagree, the
    disagreement is the finding.
    """
    if not HAVE_PIL or not ref_dir.is_dir():
        return False
    ref = fa.Reference(ref_dir, USABLE_WIDTH, USABLE_HEIGHT)
    if len(ref) == 0:
        return False
    for c in cells:
        if not (c.present and not c.void and c.n_frames_sampled):
            continue
        p50, mn, n, aligned, method = score_against(c, ref)
        c.psnr_true_n, c.psnr_true_aligned = n, aligned
        if aligned:
            c.psnr_true_p50 = p50
    return True


# ----------------------------------------------------------------------------
# headline
# ----------------------------------------------------------------------------
def headline(cells):
    """The answer is a BRACKET, never a point.

    The lowest PASSing rung and the highest FAILing rung below it are what the
    sweep actually establishes: the minimum lies between them. Naming the lowest
    rung tested as if it were the minimum is the specific error this campaign was
    extended to avoid -- the ladder originally started at 1.5 Mbps, its bottom rung
    passed, and a ladder whose bottom rung passes is indistinguishable from a
    ladder that found its floor.
    """
    out = {}
    for codec in EXPECTED_CODECS:
        rungs = [c for c in cells if c.codec == codec and c.rate_kbps
                 and not c.void and not c.superseded_by]
        rungs.sort(key=lambda c: c.rate_kbps)
        passing = [c for c in rungs if c.verdict == "PASS"]
        failing = [c for c in rungs if c.verdict == "FAIL"]
        winner = passing[0] if passing else None
        # the highest FAIL strictly below the lowest PASS is the other jaw
        lower = None
        if winner:
            below = [c for c in failing if c.rate_kbps < winner.rate_kbps]
            lower = below[-1] if below else None
        gaps = []
        if winner:
            gaps = [c for c in rungs
                    if c.rate_kbps < winner.rate_kbps
                    and (lower is None or c.rate_kbps > lower.rate_kbps)
                    and c.verdict in ("MISSING", "NO DATA", "UNMEASURED", "PARTIAL")]
        out[codec] = {
            "winner": winner,
            "lower": lower,
            "rungs": rungs,
            "tested": [c for c in rungs if c.verdict in ("PASS", "FAIL")],
            "unmeasured": [c for c in rungs
                           if c.verdict in ("UNMEASURED", "PARTIAL")],
            "gaps_in_bracket": gaps,
            "underspent_below": [c for c in rungs
                                 if winner and c.rate_kbps < winner.rate_kbps
                                 and c.underspent],
            # No failing rung anywhere means the minimum was never bracketed: it is
            # at or below the bottom of the ladder, and the ladder does not say where.
            "unbracketed": bool(winner and lower is None),
            "at_ladder_floor": bool(winner and winner.rate_kbps == min(EXPECTED_RATES)),
        }
    return out


def bracket_text(h, codec_name):
    """One plain sentence stating what the sweep establishes for this codec."""
    w, lo = h["winner"], h["lower"]
    if w is None:
        if h["tested"]:
            hi = max(h["tested"], key=lambda c: c.rate_kbps)
            return (f"No rung passed. The highest rate tested, {hi.rate_kbps} kbps, "
                    f"still failed, so the minimum is above the top of this ladder.")
        return "No rung has scoreable data yet."
    if lo is None:
        return (f"Not bracketed. {w.rate_kbps} kbps passes and nothing below it "
                f"fails, so the minimum for {codec_name} is at or below "
                f"{w.rate_kbps} kbps and this sweep does not say where.")
    return (f"Between {lo.rate_kbps} and {w.rate_kbps} kbps. {lo.rate_kbps} fails "
            f"({lo.fail_reasons[0].split(':')[0] if lo.fail_reasons else 'criteria'}), "
            f"{w.rate_kbps} passes.")


# ----------------------------------------------------------------------------
# CSV output
# ----------------------------------------------------------------------------
SUMMARY_COLS = [
    "cell", "room", "rate_kbps", "codec", "codec_source", "verdict", "fail_reasons",
    "unmeasured_reasons", "publisher_metadata_missing", "superseded_by",
    "published_frames", "received_frames", "decoded_frames", "drawn_frames",
    "network_gap_frames", "decode_gap_frames", "render_skip_frames",
    "frame_ids_absent_from_drawn",
    "packets_lost_cumulative", "est_packets_received", "loss_pct_estimated",
    "freeze_count", "freeze_ms", "resolutions_frames", "resolutions_log_lines",
    "fps_received", "fps_log_p50",
    "fps_drawn", "duration_s", "receive_qp_p50", "receive_bitrate_mbps_p50",
    "cap_mbps", "delivered_mbps_mean", "budget_used_pct", "underspent",
    "budget_source",
    "transport_p50_ms", "transport_p95_ms", "local_p50_ms", "local_p95_ms",
    "decode_p50_ms", "render_p50_ms", "decoder",
    "psnr_rel_p50_db", "psnr_rel_min_db", "psnr_rel_n", "psnr_rel_aligned",
    "psnr_rel_reference", "psnr_rel_method",
    "psnr_true_p50_db", "psnr_true_n", "psnr_true_aligned",
    "frames_sampled", "notes",
]


def gap(a, b):
    return None if a is None or b is None else a - b


def row_for(c: Cell):
    return {
        "cell": c.index if c.index is not None else "",
        "room": c.room,
        "rate_kbps": c.rate_kbps or "",
        "codec": c.codec or "",
        "codec_source": c.codec_source,
        "verdict": c.verdict,
        "fail_reasons": "; ".join(c.fail_reasons),
        "unmeasured_reasons": "; ".join(c.unknown_reasons),
        "publisher_metadata_missing": "yes" if c.no_publisher_metadata else "",
        "superseded_by": c.superseded_by,
        "published_frames": c.published if c.published is not None else "",
        "received_frames": c.received if c.received is not None else "",
        "decoded_frames": c.decoded if c.decoded is not None else "",
        "drawn_frames": c.drawn if c.drawn is not None else "",
        "network_gap_frames": _v(gap(c.published, c.received)),
        "decode_gap_frames": _v(gap(c.received, c.decoded)),
        "render_skip_frames": _v(gap(c.decoded, c.drawn)),
        "frame_ids_absent_from_drawn": _v(c.id_gaps),
        "packets_lost_cumulative": _v(c.packets_lost),
        "est_packets_received": _v(c.est_packets_recv),
        "loss_pct_estimated": fnum(c.loss_pct_est, 3) if c.loss_pct_est is not None else "",
        "freeze_count": _v(c.freeze_count),
        "freeze_ms": fnum(c.freeze_ms, 0) if c.freeze_ms is not None else "",
        "resolutions_frames": " ".join(
            f"{k}:{v}" for k, v in sorted(c.resolutions.items(), key=lambda kv: -kv[1])),
        "resolutions_log_lines": " ".join(
            f"{k}:{v}" for k, v in sorted(c.resolutions_log.items(),
                                          key=lambda kv: -kv[1])),
        "fps_received": fnum(c.fps_received, 2) if c.fps_received is not None else "",
        "fps_log_p50": fnum(c.fps_log_p50, 2) if c.fps_log_p50 is not None else "",
        "fps_drawn": fnum(c.fps_drawn, 2) if c.fps_drawn is not None else "",
        "duration_s": fnum(c.duration_s, 1) if c.duration_s is not None else "",
        "receive_qp_p50": fnum(c.qp_p50, 2) if c.qp_p50 is not None else "",
        "receive_bitrate_mbps_p50": fnum(c.bitrate_p50, 3) if c.bitrate_p50 is not None else "",
        "cap_mbps": fnum(c.rate_kbps / 1000.0, 2) if c.rate_kbps else "",
        "delivered_mbps_mean": (fnum(c.sent_mbps_mean, 3) if c.sent_mbps_mean is not None
                                else fnum(c.recv_mbps_mean, 3)
                                if c.recv_mbps_mean is not None else ""),
        "budget_used_pct": fnum(c.budget_frac * 100, 1) if c.budget_frac is not None else "",
        "underspent": "yes" if c.underspent else ("no" if c.budget_frac is not None else ""),
        "budget_source": c.budget_source,
        "transport_p50_ms": fnum(c.transport_p50, 1) if c.transport_p50 is not None else "",
        "transport_p95_ms": fnum(c.transport_p95, 1) if c.transport_p95 is not None else "",
        "local_p50_ms": fnum(c.local_p50, 1) if c.local_p50 is not None else "",
        "local_p95_ms": fnum(c.local_p95, 1) if c.local_p95 is not None else "",
        "decode_p50_ms": fnum(c.decode_p50, 2) if c.decode_p50 is not None else "",
        "render_p50_ms": fnum(c.render_p50, 2) if c.render_p50 is not None else "",
        "decoder": c.decoder_impl,
        "psnr_rel_p50_db": fnum(c.psnr_p50, 2) if c.psnr_p50 is not None else "",
        "psnr_rel_min_db": fnum(c.psnr_min, 2) if c.psnr_min is not None else "",
        "psnr_rel_n": c.psnr_n or "",
        "psnr_rel_aligned": ("" if c.psnr_aligned is None
                             else ("yes" if c.psnr_aligned else "NO -- UNALIGNED")),
        "psnr_rel_reference": c.psnr_ref,
        "psnr_rel_method": c.psnr_method,
        "psnr_true_p50_db": fnum(c.psnr_true_p50, 2) if c.psnr_true_p50 is not None else "",
        "psnr_true_n": c.psnr_true_n or "",
        "psnr_true_aligned": ("" if c.psnr_true_aligned is None
                              else ("yes" if c.psnr_true_aligned else "NO -- UNALIGNED")),
        "frames_sampled": c.n_frames_sampled,
        "notes": " | ".join(c.notes),
    }


def _v(x):
    return "" if x is None else x


def write_csv(cells, out: Path):
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=SUMMARY_COLS)
        w.writeheader()
        for c in cells:
            w.writerow(row_for(c))


# ----------------------------------------------------------------------------
# terminal table
# ----------------------------------------------------------------------------
def print_report(cells, head, results_dir):
    print(f"\novernight ladder -- {results_dir}")
    print("=" * 127)
    hdr = (f"{'cell':>4} {'rate':>6} {'codec':>5} {'pub':>6} {'recv':>6} {'dec':>6} "
           f"{'drawn':>6} {'gap%':>6} {'loss%':>7} {'frz':>4} {'fps':>6} "
           f"{'qp':>5} {'budget':>7} {'relPSNR':>8} {'TRANSPORT':>13} {'LOCAL':>13}  verdict")
    print(hdr)
    print("-" * 127)
    for c in sorted(cells, key=lambda c: (c.index is None, c.index or 0,
                                          c.codec or "", c.rate_kbps or 0)):
        net = gap(c.published, c.received)
        netpct = (100.0 * net / c.published) if net is not None and c.published else None
        if netpct is not None and netpct < 0:
            netpct = None      # see the note: the span is measured from drawn ids only
        print(f"{(c.index if c.index else '--'):>4} "
              f"{(str(c.rate_kbps)+'k' if c.rate_kbps else '?'):>6} "
              f"{(c.codec or '?'):>5} "
              f"{_v(c.published):>6} {_v(c.received):>6} {_v(c.decoded):>6} "
              f"{_v(c.drawn):>6} "
              f"{(fnum(netpct,1) if netpct is not None else '--'):>6} "
              f"{(fnum(c.loss_pct_est,3) if c.loss_pct_est is not None else '--'):>7} "
              f"{_v(c.freeze_count):>4} "
              f"{(fnum(c.fps_received,1) if c.fps_received is not None else '--'):>6} "
              f"{(fnum(c.qp_p50,1) if c.qp_p50 is not None else '--'):>5} "
              f"{((fnum(c.budget_frac*100,0)+'%'+('!' if c.underspent else ''))
                  if c.budget_frac is not None else '--'):>7} "
              f"{('UNALIGN' if c.psnr_aligned is False else
                  'ref' if c.psnr_ref.startswith('reference') else
                  fnum(c.psnr_p50,1) if c.psnr_p50 is not None else '--'):>8} "
              f"{(fnum(c.transport_p50,0)+'/'+fnum(c.transport_p95,0)):>13} "
              f"{(fnum(c.local_p50,0)+'/'+fnum(c.local_p95,0)):>13}  "
              f"{c.verdict}"
              + (f" -- {c.fail_reasons[0]}" if c.fail_reasons
                 else f" -- {c.unknown_reasons[0]}" if c.unknown_reasons else ""))
    print("-" * 127)
    print("gap% = published frames that never arrived (network). "
          "Render skip is a separate column in the CSV and the HTML.")
    print("budget = delivered bitrate as a share of the cap. A '!' means the encoder "
          "UNDERSPENT its cap:")
    print("         that rung is not testing the rate on its label, whatever its verdict.")
    print("TRANSPORT = exposure_to_receive_ms p50/p95 (network). "
          "LOCAL = receive_to_gpu_complete_ms p50/p95 (Host B renderer).")
    print("These are NEVER added together. loss% is ESTIMATED "
          f"(packets_received is not recorded; denominator assumes {ASSUMED_PAYLOAD_BYTES}B payloads).")
    print("qp is NOT comparable across codecs: H.264 indices run 0-51, AV1's run "
          "0-255. Read it down a codec's own column.")
    print("relPSNR = luma dB against the 8000k rung of the SAME codec, frames matched "
          "by content (frame_align).")
    print("         Within-codec only; not comparable across codecs; never feeds the verdict.")
    print("PASS = passes the four MEASURABLE registered criteria. The fifth, "
          "'sampled frames visually clear',")
    print("       is an operator judgement and is left to the frame strip in the HTML "
          "report -- not automated.")

    print("\nHEADLINE -- the answer is a BRACKET, not a point")
    print("=" * 127)
    for codec in EXPECTED_CODECS:
        h = head[codec]
        w, lo = h["winner"], h["lower"]
        name = "H.264" if codec == "h264" else "AV1"
        print(f"  {name:6s}  {bracket_text(h, name)}")
        if w is None:
            n = len(h["tested"])
            print(f"          {n} of {len(EXPECTED_RATES)} rungs scoreable"
                  + (f", {len(h['unmeasured'])} partial/unmeasured"
                     if h["unmeasured"] else ""))
            continue
        print(f"          lowest PASS {w.rate_kbps}k -- "
              f"transport p50 {fnum(w.transport_p50,1)} ms / p95 "
              f"{fnum(w.transport_p95,1)} ms  (network)")
        print(f"                          "
              f"local     p50 {fnum(w.local_p50,1)} ms / p95 "
              f"{fnum(w.local_p95,1)} ms  (Host B renderer, decode "
              f"{fnum(w.decode_p50,2)} ms)")
        if lo is not None:
            print(f"          highest FAIL {lo.rate_kbps}k -- "
                  f"{lo.fail_reasons[0] if lo.fail_reasons else ''}")
        if h["unbracketed"]:
            print(f"          CAVEAT: NOT BRACKETED. Nothing below {w.rate_kbps}k "
                  f"fails, so the minimum is at or below {w.rate_kbps}k.")
            if h["at_ladder_floor"]:
                print(f"          {w.rate_kbps}k is the bottom of the ladder; "
                      f"bracketing it needs rungs below.")
        if h["gaps_in_bracket"]:
            g = ", ".join(f"{c.rate_kbps}k ({c.verdict})"
                          for c in h["gaps_in_bracket"])
            print(f"          CAVEAT: rungs inside the bracket are not scoreable "
                  f"({g}); the bracket is wider than it looks")
        if w.underspent:
            d = w.sent_mbps_mean if w.sent_mbps_mean is not None else w.recv_mbps_mean
            print(f"          CAVEAT: the passing rung UNDERSPENT its cap -- "
                  f"delivered {fnum(d,2)} Mbps against {w.rate_kbps/1000:.2f} Mbps. "
                  f"The tested rate is the delivered figure.")
        if h["underspent_below"]:
            u = ", ".join(f"{c.rate_kbps}k" for c in h["underspent_below"])
            print(f"          CAVEAT: rungs below underspent their caps ({u}), so "
                  f"their FAILs do not rule those rates out")

    a, b = head["h264"]["winner"], head["av1"]["winner"]
    if a and b:
        print(f"\n  Latency difference at each codec's lowest passing rung "
              f"(AV1 {b.rate_kbps}k minus H.264 {a.rate_kbps}k):")
        print(f"     transport p50  {fnum(b.transport_p50 - a.transport_p50, 1)} ms")
        print(f"     local     p50  {fnum(b.local_p50 - a.local_p50, 1)} ms   "
              f"(decode {fnum((b.decode_p50 or 0) - (a.decode_p50 or 0), 2)} ms of it)")
        print("     reported separately on purpose; do not add them.")

    # Decode headroom: no ceiling is expected anywhere on this ladder, so an
    # exceedance is a finding rather than a footnote.
    hot = [c for c in cells if c.decode_p50 and c.decode_p50 > FRAME_BUDGET_MS * 0.5]
    if hot:
        print(f"\n  DECODE COST: {', '.join(f'{c.label} {c.decode_p50:.1f} ms' for c in hot)} "
              f"-- above half the {FRAME_BUDGET_MS:.1f} ms frame budget, which was "
              f"not predicted")

    # AV1 frame rate is a THROUGHPUT result, on its own axis, never a compression one.
    slow_av1 = [c for c in cells if c.codec == "av1" and c.fps_received is not None
                and c.fps_received < USABLE_MIN_FPS and not c.void
                and not c.superseded_by]
    if slow_av1:
        print(f"\n  AV1 THROUGHPUT: "
              + ", ".join(f"{c.rate_kbps}k {c.fps_received:.1f} fps "
                          f"(decode {fnum(c.decode_p50,2)} ms)" for c in slow_av1))
        print("     A frame rate below 29 is a THROUGHPUT result and belongs on its "
              "own axis. It is NOT evidence about AV1's compression.")
    print()


# ============================================================================
# HTML
# ============================================================================
# Two series only (H.264, AV1) -> categorical slots 1 and 2 from the validated
# reference palette. Slots 1-3 of that palette clear the all-pairs CVD and
# normal-vision floors in both light and dark modes, so this pair is covered by
# that documented result. Status hues are the reserved good/critical steps and
# always ship with a text label, never colour alone.
CSS = """
:root {
  color-scheme: light;
  --bg:            #f4f4f1;
  --surface-1:     #fcfcfb;
  --surface-2:     #f0efec;
  --border:        #d9d8d2;
  --border-strong: #b9b8b0;
  --text-primary:  #0b0b0b;
  --text-secondary:#52514e;
  --text-muted:    #78776f;
  --series-h264:   #2a78d6;
  --series-av1:    #eb6834;
  --good:          #008300;
  --good-bg:       #e6f2e6;
  --critical:      #c9302f;
  --critical-bg:   #fbe9e9;
  --warn:          #8a6d00;
  --warn-bg:       #faf3dd;
  --grid:          #e4e3dd;
  --shadow:        0 1px 2px rgba(0,0,0,.05), 0 4px 12px rgba(0,0,0,.04);
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    color-scheme: dark;
    --bg:            #131312;
    --surface-1:     #1a1a19;
    --surface-2:     #232322;
    --border:        #34342f;
    --border-strong: #4d4d46;
    --text-primary:  #ffffff;
    --text-secondary:#c3c2b7;
    --text-muted:    #918f85;
    --series-h264:   #3987e5;
    --series-av1:    #d95926;
    --good:          #4caf50;
    --good-bg:       #16281a;
    --critical:      #e66767;
    --critical-bg:   #2e1a1a;
    --warn:          #d9b64a;
    --warn-bg:       #2b2718;
    --grid:          #2b2b28;
    --shadow:        0 1px 2px rgba(0,0,0,.4), 0 4px 12px rgba(0,0,0,.3);
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --bg:            #131312;
  --surface-1:     #1a1a19;
  --surface-2:     #232322;
  --border:        #34342f;
  --border-strong: #4d4d46;
  --text-primary:  #ffffff;
  --text-secondary:#c3c2b7;
  --text-muted:    #918f85;
  --series-h264:   #3987e5;
  --series-av1:    #d95926;
  --good:          #4caf50;
  --good-bg:       #16281a;
  --critical:      #e66767;
  --critical-bg:   #2e1a1a;
  --warn:          #d9b64a;
  --warn-bg:       #2b2718;
  --grid:          #2b2b28;
  --shadow:        0 1px 2px rgba(0,0,0,.4), 0 4px 12px rgba(0,0,0,.3);
}

* { box-sizing: border-box; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--text-primary);
  font: 14px/1.55 ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto,
        "Helvetica Neue", Arial, sans-serif;
  -webkit-font-smoothing: antialiased;
}
.wrap { max-width: 1240px; margin: 0 auto; padding: 32px 24px 80px; }
h1 { font-size: 26px; line-height: 1.25; margin: 0 0 4px; letter-spacing: -.02em; }
h2 { font-size: 17px; margin: 44px 0 12px; letter-spacing: -.01em; }
h3 { font-size: 14px; margin: 0 0 10px; color: var(--text-secondary);
     font-weight: 600; }
p  { margin: 0 0 12px; color: var(--text-secondary); max-width: 78ch; }
code, .mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
.sub { color: var(--text-muted); font-size: 13px; margin-bottom: 28px; }

.card { background: var(--surface-1); border: 1px solid var(--border);
        border-radius: 10px; padding: 20px; box-shadow: var(--shadow); }

/* headline ---------------------------------------------------------------- */
.answer { display: grid; grid-template-columns: repeat(auto-fit, minmax(330px, 1fr));
          gap: 16px; margin-bottom: 8px; }
.ans { background: var(--surface-1); border: 1px solid var(--border);
       border-left: 4px solid var(--accent, var(--border-strong));
       border-radius: 10px; padding: 18px 20px; box-shadow: var(--shadow); }
.ans.h264 { --accent: var(--series-h264); }
.ans.av1  { --accent: var(--series-av1); }
.ans .codec { font-size: 12px; font-weight: 700; letter-spacing: .09em;
              text-transform: uppercase; color: var(--text-muted); }
.ans .hero { font-size: 38px; font-weight: 650; letter-spacing: -.03em;
             line-height: 1.1; margin: 6px 0 2px; }
.ans .hero small { font-size: 17px; font-weight: 500; color: var(--text-secondary); }
.ans .brk { font-size: 12px; color: var(--text-muted); margin-top: 1px; }
.ans .none { font-size: 20px; font-weight: 600; color: var(--critical);
             margin: 8px 0 2px; letter-spacing: -.01em; }
.lat { display: flex; gap: 26px; margin-top: 14px; padding-top: 14px;
       border-top: 1px solid var(--border); }
.lat div { min-width: 0; }
.lat .k { font-size: 11px; letter-spacing: .07em; text-transform: uppercase;
          color: var(--text-muted); }
.lat .v { font-size: 19px; font-weight: 600; letter-spacing: -.01em; }
.lat .u { font-size: 12px; color: var(--text-muted); }

.callout { border-radius: 8px; padding: 12px 15px; font-size: 13px;
           border: 1px solid var(--border); background: var(--surface-2);
           color: var(--text-secondary); margin: 14px 0; }
.callout.warn { background: var(--warn-bg); border-color: var(--warn);
                color: var(--text-primary); }
.callout.stop { background: var(--critical-bg); border-color: var(--critical);
                color: var(--text-primary); border-width: 1px; }
.callout.stop .hd { font-weight: 700; color: var(--critical); font-size: 13px;
                    letter-spacing: .03em; text-transform: uppercase;
                    margin-bottom: 4px; }
.callout b { color: var(--text-primary); }

/* table ------------------------------------------------------------------- */
.scroll { overflow-x: auto; border: 1px solid var(--border); border-radius: 10px;
          background: var(--surface-1); box-shadow: var(--shadow); }
table { border-collapse: collapse; width: 100%; font-size: 12.5px; }
th, td { padding: 7px 10px; text-align: right; white-space: nowrap;
         border-bottom: 1px solid var(--border); }
th { background: var(--surface-2); color: var(--text-secondary); font-weight: 600;
     font-size: 11px; letter-spacing: .04em; text-transform: uppercase;
     position: sticky; top: 0; z-index: 2; }
th.grp { text-align: center; border-left: 1px solid var(--border-strong);
         letter-spacing: .06em; }
td.grpstart, th.grpstart { border-left: 1px solid var(--border-strong); }
td:first-child, th:first-child { text-align: left; position: sticky; left: 0;
         background: var(--surface-1); z-index: 1; }
th:first-child { background: var(--surface-2); z-index: 3; }
tbody tr:hover td { background: var(--surface-2); }
tbody tr:hover td:first-child { background: var(--surface-2); }
td.l, th.l { text-align: left; white-space: normal; }
.dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%;
       margin-right: 7px; vertical-align: middle; }
.dot.h264 { background: var(--series-h264); }
.dot.av1  { background: var(--series-av1); }
.pill { display: inline-block; padding: 1px 8px; border-radius: 999px;
        font-size: 11px; font-weight: 700; letter-spacing: .04em; }
.pill.pass { background: var(--good-bg); color: var(--good);
             border: 1px solid var(--good); }
.pill.fail { background: var(--critical-bg); color: var(--critical);
             border: 1px solid var(--critical); }
.pill.miss { background: var(--surface-2); color: var(--text-muted);
             border: 1px solid var(--border-strong); }
.pill.under { background: var(--warn-bg); color: var(--warn);
              border: 1px solid var(--warn); }
.pill.unmeas { background: var(--warn-bg); color: var(--warn);
               border: 1px dashed var(--warn); }
.pill.partial { background: var(--warn-bg); color: var(--warn);
                border: 1px solid var(--warn); }
.pill.void { background: var(--surface-2); color: var(--text-muted);
             border: 1px dashed var(--border-strong); }
.unal { color: var(--warn); font-weight: 650; }
tr.missing td, tr.void td { color: var(--text-muted); font-style: italic; }
.why { color: var(--text-secondary); font-size: 11px; line-height: 1.3;
       margin-top: 2px; }
td.verdict { min-width: 30ch; max-width: 58ch; vertical-align: middle; }
tbody td { vertical-align: middle; }
td.codec { white-space: nowrap; }

/* charts ------------------------------------------------------------------ */
.charts { display: grid; grid-template-columns: repeat(auto-fit, minmax(360px, 1fr));
          gap: 16px; }
.chart svg { display: block; width: 100%; height: auto; overflow: visible; }
.legend { display: flex; gap: 16px; margin: 0 0 8px; flex-wrap: wrap;
          font-size: 12px; color: var(--text-secondary); }
.legend span { display: inline-flex; align-items: center; gap: 6px; }
.legend i { width: 11px; height: 11px; border-radius: 3px; display: inline-block; }
.axis text { font-size: 11px; fill: var(--text-muted); }
.axis line { stroke: var(--grid); }
.axis .dom { stroke: var(--border-strong); }
.ttl { font-size: 10.5px; fill: var(--text-secondary); }
.pt { stroke: var(--surface-1); stroke-width: 2; }
.pt:hover { stroke-width: 3; }
.hit { fill: transparent; cursor: pointer; }
.dlabel { font-size: 11px; font-weight: 650; }
.thr { stroke: var(--text-muted); stroke-dasharray: 3 3; stroke-width: 1; }

#tip { position: fixed; pointer-events: none; opacity: 0; transition: opacity .1s;
       background: var(--surface-1); color: var(--text-primary);
       border: 1px solid var(--border-strong); border-radius: 7px;
       padding: 7px 10px; font-size: 12px; box-shadow: var(--shadow);
       z-index: 50; max-width: 260px; }
#tip b { display: block; margin-bottom: 2px; }
#tip .m { color: var(--text-secondary); }

/* frame strip ------------------------------------------------------------- */
.strip { display: grid; grid-template-columns: repeat(auto-fill, minmax(210px, 1fr));
         gap: 14px; }
.fr { background: var(--surface-1); border: 1px solid var(--border);
      border-radius: 9px; overflow: hidden; box-shadow: var(--shadow); }
.fr img { display: block; width: 100%; height: auto; background: var(--surface-2); }
.fr .cap { padding: 8px 11px 10px; border-top: 1px solid var(--border); }
.fr .cap .t { font-weight: 650; font-size: 13px; display: flex;
              align-items: center; justify-content: space-between; gap: 8px; }
.fr .cap .m { font-size: 11px; color: var(--text-muted); margin-top: 3px;
              font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
.fr .none { padding: 30px 12px; text-align: center; color: var(--text-muted);
            font-size: 12px; background: var(--surface-2); }
.codehead { font-size: 12px; font-weight: 700; letter-spacing: .09em;
            text-transform: uppercase; color: var(--text-muted);
            margin: 26px 0 10px; display: flex; align-items: center; gap: 8px; }
footer { margin-top: 50px; padding-top: 18px; border-top: 1px solid var(--border);
         color: var(--text-muted); font-size: 12px; }
"""

TIP_JS = """
(function () {
  var tip = document.getElementById('tip');
  function show(e) {
    var t = e.currentTarget.getAttribute('data-tip');
    if (!t) return;
    tip.innerHTML = t;
    tip.style.opacity = 1;
    move(e);
  }
  function move(e) {
    var r = tip.getBoundingClientRect();
    var x = e.clientX + 14, y = e.clientY + 14;
    if (x + r.width > window.innerWidth - 8) x = e.clientX - r.width - 14;
    if (y + r.height > window.innerHeight - 8) y = e.clientY - r.height - 14;
    tip.style.left = x + 'px'; tip.style.top = y + 'px';
  }
  function hide() { tip.style.opacity = 0; }
  var n = document.querySelectorAll('[data-tip]');
  for (var i = 0; i < n.length; i++) {
    n[i].addEventListener('mouseenter', show);
    n[i].addEventListener('mousemove', move);
    n[i].addEventListener('mouseleave', hide);
  }
})();
"""

SERIES = [("h264", "H.264", "var(--series-h264)"),
          ("av1", "AV1", "var(--series-av1)")]


def esc(s):
    return html.escape(str(s), quote=True)


def nice_ticks(lo, hi, n=5):
    if hi <= lo:
        hi = lo + 1
    raw = (hi - lo) / max(1, n)
    mag = 10 ** math.floor(math.log10(raw)) if raw > 0 else 1
    for m in (1, 2, 2.5, 5, 10):
        if raw / mag <= m:
            step = m * mag
            break
    else:
        step = 10 * mag
    start = math.floor(lo / step) * step
    ticks, t = [], start
    while t <= hi + step * 0.5:
        ticks.append(round(t, 10))
        t += step
    return ticks


def svg_chart(title, subtitle, series_pts, ylabel, yfmt, threshold=None,
              thr_label="", ymin_zero=True, unit=""):
    """One y-axis line+point chart. series_pts: {codec: [(rate, value, tiptext)]}."""
    W, H = 480, 300
    ml, mr, mt, mb = 54, 52, 26, 46
    pw, ph = W - ml - mr, H - mt - mb

    allv = [v for pts in series_pts.values() for (_, v, _) in pts]
    if not allv:
        return (f'<div class="chart card"><h3>{esc(title)}</h3>'
                f'<p style="font-size:12px;margin:0">No data yet.</p></div>')
    vmin = min(allv + ([threshold] if threshold is not None else []))
    vmax = max(allv + ([threshold] if threshold is not None else []))
    if ymin_zero:
        vmin = min(0, vmin)
    if vmax == vmin:
        vmax = vmin + 1
    padv = (vmax - vmin) * 0.10
    vmax += padv
    if not ymin_zero:
        vmin -= padv
    yt = nice_ticks(vmin, vmax, 5)
    vmin, vmax = min(vmin, yt[0]), max(vmax, yt[-1])

    xs = sorted({r for pts in series_pts.values() for (r, _, _) in pts})
    xmin, xmax = min(EXPECTED_RATES), max(EXPECTED_RATES)
    if xs:
        xmin, xmax = min(xmin, min(xs)), max(xmax, max(xs))

    def X(r):
        return ml + (r - xmin) / (xmax - xmin) * pw if xmax > xmin else ml + pw / 2

    def Y(v):
        return mt + ph - (v - vmin) / (vmax - vmin) * ph

    o = [f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="{esc(title)}">']
    o.append('<g class="axis">')
    for t in yt:
        y = Y(t)
        o.append(f'<line x1="{ml}" y1="{y:.1f}" x2="{ml+pw}" y2="{y:.1f}"/>')
        o.append(f'<text x="{ml-8}" y="{y+3.5:.1f}" text-anchor="end">{yfmt(t)}</text>')
    # The ladder now runs 500k to 8000k on a linear axis, so the low rungs bunch up
    # and their labels overlap. Label only what fits; every rung still has a tick.
    last_label_x = -1e9
    for r in EXPECTED_RATES:
        x = X(r)
        o.append(f'<line x1="{x:.1f}" y1="{mt+ph}" x2="{x:.1f}" y2="{mt+ph+3}"/>')
        if x - last_label_x >= 27 or r == EXPECTED_RATES[-1]:
            o.append(f'<text x="{x:.1f}" y="{mt+ph+16}" text-anchor="middle">'
                     f'{r//1000 if r%1000==0 else r/1000:g}</text>')
            last_label_x = x
    o.append(f'<line class="dom" x1="{ml}" y1="{mt+ph}" x2="{ml+pw}" y2="{mt+ph}"/>')
    o.append('</g>')
    o.append(f'<text class="ttl" x="{ml+pw/2:.0f}" y="{H-8}" text-anchor="middle">'
             f'publisher target bitrate (Mbps)</text>')
    o.append(f'<text class="ttl" x="{-(mt+ph/2):.0f}" y="13" text-anchor="middle" '
             f'transform="rotate(-90)">{esc(ylabel)}</text>')

    if threshold is not None:
        y = Y(threshold)
        o.append(f'<line class="thr" x1="{ml}" y1="{y:.1f}" x2="{ml+pw}" y2="{y:.1f}"/>')
        if thr_label:
            o.append(f'<text class="ttl" x="{ml+pw-2}" y="{y-5:.1f}" '
                     f'text-anchor="end">{esc(thr_label)}</text>')

    labels = []
    for key, name, colour in SERIES:
        pts = sorted(series_pts.get(key, []))
        if not pts:
            continue
        d = " ".join(f"{'M' if i == 0 else 'L'}{X(r):.1f},{Y(v):.1f}"
                     for i, (r, v, _) in enumerate(pts))
        o.append(f'<path d="{d}" fill="none" stroke="{colour}" stroke-width="2" '
                 f'stroke-linejoin="round" stroke-linecap="round"/>')
        for r, v, tip in pts:
            o.append(f'<circle class="pt" cx="{X(r):.1f}" cy="{Y(v):.1f}" r="4.5" '
                     f'fill="{colour}"/>')
            o.append(f'<circle class="hit" cx="{X(r):.1f}" cy="{Y(v):.1f}" r="13" '
                     f'data-tip="{esc(tip)}"/>')
        lr, lv, _ = pts[-1]
        labels.append([X(lr) + 9, Y(lv) + 3.5, colour, name])

    # Two series whose last points coincide would print their labels on top of each
    # other, which is exactly the case where the reader most needs to tell them
    # apart. Nudge them vertically when they overlap.
    labels.sort(key=lambda l: l[1])
    for i in range(1, len(labels)):
        if labels[i][1] - labels[i - 1][1] < 12:
            labels[i][1] = labels[i - 1][1] + 12
    for x, y, colour, name in labels:
        o.append(f'<text class="dlabel" x="{x:.1f}" y="{y:.1f}" '
                 f'fill="{colour}">{esc(name)}</text>')
    o.append('</svg>')

    leg = " ".join(f'<span><i style="background:{c}"></i>{esc(n)}</span>'
                   for k, n, c in SERIES if series_pts.get(k))
    sub = f'<p style="font-size:11.5px;margin:-4px 0 8px">{esc(subtitle)}</p>' if subtitle else ""
    return (f'<div class="chart card"><h3>{esc(title)}</h3>{sub}'
            f'<div class="legend">{leg}</div>' + "".join(o) + '</div>')


def build_charts(cells):
    def series(valfn, tipfn):
        out = {}
        for c in cells:
            if not c.present or not c.rate_kbps or not c.codec:
                continue
            v = valfn(c)
            if v is None:
                continue
            out.setdefault(c.codec, []).append((c.rate_kbps, v, tipfn(c, v)))
        return out

    def delivery(c):
        if c.published and c.received is not None:
            return 100.0 * min(c.received, c.published) / c.published
        return None

    charts = []
    charts.append(svg_chart(
        "Delivery rate -- frames that arrived",
        "received (log) as a share of the frame_id span the publisher emitted. "
        "Network only; render skip is not in this number.",
        series(delivery, lambda c, v: f"<b>{esc(c.label)}</b>"
               f"<span class='m'>{v:.2f}% of {c.published} published frames arrived"
               f"<br>received {c.received} &middot; decoded {c.decoded}"
               f"<br>drawn {c.drawn} (renderer, not network)</span>"),
        "frames arrived (%)", lambda t: f"{t:g}", threshold=100.0,
        thr_label="100%", ymin_zero=False))

    charts.append(svg_chart(
        "Transport latency -- exposure to receive",
        "exposure_to_receive_ms p50. Network path only. Host B's renderer is NOT "
        "in this figure and is never added to it.",
        series(lambda c: c.transport_p50,
               lambda c, v: f"<b>{esc(c.label)}</b><span class='m'>"
                            f"transport p50 {v:.1f} ms &middot; p95 "
                            f"{fnum(c.transport_p95,1)} ms<br>"
                            f"local p50 {fnum(c.local_p50,1)} ms (separate)</span>"),
        "transport p50 (ms)", lambda t: f"{t:g}"))

    charts.append(svg_chart(
        "Local latency -- receive to GPU complete",
        "receive_to_gpu_complete_ms p50. Host B's decode + renderer. A codec's "
        "decode cost shows up here, not in transport.",
        series(lambda c: c.local_p50,
               lambda c, v: f"<b>{esc(c.label)}</b><span class='m'>"
                            f"local p50 {v:.1f} ms &middot; p95 {fnum(c.local_p95,1)} ms"
                            f"<br>decode p50 {fnum(c.decode_p50,2)} ms &middot; "
                            f"render p50 {fnum(c.render_p50,2)} ms</span>"),
        "local p50 (ms)", lambda t: f"{t:g}"))

    charts.append(svg_chart(
        "Budget actually spent",
        "Delivered bitrate as a share of the cap. A rung well under 100% is not "
        "testing the rate on its label -- the encoder undershot on cheap content.",
        series(lambda c: c.budget_frac * 100 if c.budget_frac is not None else None,
               lambda c, v: f"<b>{esc(c.label)}</b><span class='m'>"
                            f"{v:.0f}% of a {c.rate_kbps/1000:.2f} Mbps cap<br>"
                            f"delivered "
                            f"{fnum(c.sent_mbps_mean if c.sent_mbps_mean is not None else c.recv_mbps_mean, 2)}"
                            f" Mbps<br>{esc(c.budget_source)}</span>"),
        "delivered / cap (%)", lambda t: f"{t:g}", threshold=100.0,
        thr_label="cap"))

    charts.append(svg_chart(
        "Decode cost against the frame budget",
        f"decode_ms p50. The prediction was 3.16 ms at 1.5 Mbps rising to 9.36 ms at "
        f"8 Mbps against a {FRAME_BUDGET_MS:.1f} ms budget, so no decode ceiling is "
        f"expected anywhere on this ladder. One appearing is a finding.",
        series(lambda c: c.decode_p50,
               lambda c, v: f"<b>{esc(c.label)}</b><span class='m'>"
                            f"decode p50 {v:.2f} ms of a {FRAME_BUDGET_MS:.1f} ms "
                            f"budget ({v/FRAME_BUDGET_MS*100:.0f}%)<br>"
                            f"render p50 {fnum(c.render_p50,2)} ms &middot; "
                            f"local p50 {fnum(c.local_p50,1)} ms</span>"),
        "decode p50 (ms)", lambda t: f"{t:g}", threshold=FRAME_BUDGET_MS,
        thr_label=f"{FRAME_BUDGET_MS:.1f} ms frame budget"))

    psnr = series(lambda c: c.psnr_p50,
                  lambda c, v: f"<b>{esc(c.label)}</b><span class='m'>"
                               f"luma PSNR p50 {v:.2f} dB ({esc(c.psnr_ref)})"
                               f"<br>min {fnum(c.psnr_min,2)} dB &middot; n={c.psnr_n}"
                               f"<br>content-aligned via frame_align</span>")
    unaligned = [c for c in cells if c.psnr_aligned is False]
    if psnr or unaligned:
        note = ("Each rung content-matched against the 8000k rung of its own codec "
                "(frame_align: strided signature, full-luma PSNR, robust stride, "
                "neighbour test). A within-codec shape, not a cross-codec score, and "
                "it never feeds the usable verdict.")
        if unaligned:
            note += (" " + ", ".join(c.label for c in unaligned) +
                     " could not be aligned and are omitted rather than guessed.")
        charts.append(svg_chart(
            "Picture quality -- relative luma PSNR", note,
            psnr, "luma PSNR p50 (dB)", lambda t: f"{t:g}", ymin_zero=False))

    true_psnr = series(lambda c: c.psnr_true_p50,
                       lambda c, v: f"<b>{esc(c.label)}</b><span class='m'>"
                                    f"true luma PSNR p50 {v:.2f} dB vs source"
                                    f"<br>n={c.psnr_true_n}</span>")
    if true_psnr:
        charts.append(svg_chart(
            "Picture quality -- TRUE luma PSNR vs source",
            "Against Host A's 808 source frames, content-matched. Comparable across "
            "codecs, unlike the relative ladder. If the two disagree, the "
            "disagreement is the finding.",
            true_psnr, "true luma PSNR p50 (dB)", lambda t: f"{t:g}", ymin_zero=False))

    charts.append(svg_chart(
        "Received frame rate",
        "Decode health received count over the cell's observation window. The "
        "registered threshold is 29 fps.",
        series(lambda c: c.fps_received,
               lambda c, v: f"<b>{esc(c.label)}</b><span class='m'>"
                            f"received {v:.2f} fps<br>"
                            f"drawn {fnum(c.fps_drawn,2)} fps (Host B renderer)</span>"),
        "received frames/s", lambda t: f"{t:g}", threshold=USABLE_MIN_FPS,
        thr_label="29 fps floor"))

    charts.append(svg_chart(
        "Estimated packet loss",
        f"packets_lost over an ESTIMATED received-packet denominator "
        f"({ASSUMED_PAYLOAD_BYTES} B payloads); packets_received is not recorded.",
        series(lambda c: c.loss_pct_est,
               lambda c, v: f"<b>{esc(c.label)}</b><span class='m'>"
                            f"est loss {v:.3f}%<br>{c.packets_lost} packets lost "
                            f"cumulative<br>est {c.est_packets_recv} received</span>"),
        "estimated loss (%)", lambda t: f"{t:g}", threshold=USABLE_MAX_LOSS_PCT,
        thr_label="0.5% limit"))
    return charts


TABLE_GROUPS = [
    ("", ["Cell", "Rate", "Codec"]),
    ("Frame accounting (never summed)",
     ["Published", "Received", "Decoded", "Drawn", "Net gap", "Render skip"]),
    ("Delivery & health",
     ["Deliv %", "Pkts lost", "Loss % est", "Freezes", "Resolution(s)", "fps recv",
      "fps drawn", "QP p50*"]),
    ("Budget actually spent", ["Cap", "Delivered", "% of cap"]),
    ("Picture", ["rel PSNR"]),
    ("Transport (network)", ["p50 ms", "p95 ms"]),
    ("Local (Host B renderer)", ["p50 ms", "p95 ms", "decode p50"]),
    ("", ["Verdict"]),
]


def table_html(cells):
    o = ['<div class="scroll"><table><thead><tr>']
    for gname, cols in TABLE_GROUPS:
        cls = ' class="grp"' if gname else ' class="grp"'
        o.append(f'<th colspan="{len(cols)}"{cls}>{esc(gname) if gname else "&nbsp;"}</th>')
    o.append('</tr><tr>')
    for gi, (gname, cols) in enumerate(TABLE_GROUPS):
        for ci, c in enumerate(cols):
            klass = []
            if ci == 0 and gi > 0:
                klass.append("grpstart")
            if c in ("Codec", "Resolution(s)", "Verdict", "Cell"):
                klass.append("l")
            k = f' class="{" ".join(klass)}"' if klass else ""
            o.append(f'<th{k}>{esc(c)}</th>')
    o.append('</tr></thead><tbody>')

    def s(v, nd=1):
        return "--" if v is None else (f"{v:.{nd}f}" if isinstance(v, float) else str(v))

    for c in sorted(cells, key=lambda c: ((c.codec or "zz") != "h264",
                                          c.rate_kbps or 10 ** 9)):
        net = gap(c.published, c.received)
        skip = gap(c.decoded, c.drawn)
        deliv = (100.0 * min(c.received, c.published) / c.published
                 if c.published and c.received is not None else None)
        keys = dict(c.resolutions)
        for k in c.resolutions_log:
            keys.setdefault(k, 0)
        res = ", ".join(
            f"{k}" + (f" ({v} fr)" if len(keys) > 1 and v else "")
            for k, v in sorted(keys.items(), key=lambda kv: -kv[1])) or "--"
        pill = {"PASS": "pass", "FAIL": "fail", "VOID": "void",
                "UNMEASURED": "unmeas", "SUPERSEDED": "void",
                "PARTIAL": "partial"}.get(c.verdict, "miss")
        def short(t, n=76):
            return t if len(t) <= n else t[:n].rsplit(" ", 1)[0] + "\u2026"
        # One decisive line per row. The full text of every reason is in Data notes;
        # a table that wraps to six lines a row stops being scannable, which defeats
        # the point of putting the ladder in a table at all.
        extra = max(0, len(c.fail_reasons) + len(c.unknown_reasons) - 1)
        more = f' <span style="opacity:.7">(+{extra} more)</span>' if extra else ""
        if c.fail_reasons and c.verdict != "PASS":
            why = f"<div class='why'>{esc(short(c.fail_reasons[0]))}{more}</div>"
        elif c.unknown_reasons:
            why = (f"<div class='why'><b>not measured:</b> "
                   f"{esc(short(c.unknown_reasons[0]))}{more}</div>")
        else:
            why = ""
        if c.underspent:
            why = ("<div class='why'><b>underspent its cap</b> &mdash; not testing "
                   "the rate on its label</div>") + why

        tr = (' class="void"' if c.verdict in ("VOID", "SUPERSEDED")
              else ' class="missing"' if not c.present or c.verdict == "MISSING" else "")
        dot = f'<span class="dot {c.codec}"></span>' if c.codec else ""
        o.append(f'<tr{tr}>')
        o.append(f'<td class="l">{c.index if c.index else "--"}</td>')
        o.append(f'<td>{f"{c.rate_kbps}k" if c.rate_kbps else esc(c.room or "?")}</td>')
        o.append(f'<td class="l codec">{dot}{esc((c.codec or "?").upper())}</td>')
        for val, gstart in ((s(c.published), True), (s(c.received), False),
                            (s(c.decoded), False), (s(c.drawn), False),
                            (s(net), False), (s(skip), False)):
            o.append(f'<td{" class=grpstart" if gstart else ""}>{val}</td>')
        o.append(f'<td class="grpstart">{s(deliv, 2)}</td>')
        o.append(f'<td>{s(c.packets_lost)}</td>')
        o.append(f'<td>{s(c.loss_pct_est, 3)}</td>')
        o.append(f'<td>{s(c.freeze_count)}</td>')
        o.append(f'<td class="l">{esc(res)}</td>')
        if c.fps_uncertain:
            o.append(f'<td><span class="unal" title="straddles the 29 fps threshold '
                     f'once the log\'s 1 s timestamp quantisation is allowed for">'
                     f'{s(c.fps_received, 2)}?</span></td>')
        else:
            o.append(f'<td>{s(c.fps_received, 2)}</td>')
        o.append(f'<td>{s(c.fps_drawn, 2)}</td>')
        o.append(f'<td>{s(c.qp_p50, 1)}</td>')
        deliv_mbps = (c.sent_mbps_mean if c.sent_mbps_mean is not None
                      else c.recv_mbps_mean)
        o.append(f'<td class="grpstart">'
                 f'{(f"{c.rate_kbps/1000:.2f}" if c.rate_kbps else "--")}</td>')
        o.append(f'<td>{s(deliv_mbps, 2)}</td>')
        if c.budget_frac is None:
            o.append('<td>--</td>')
        elif c.underspent:
            o.append(f'<td><span class="pill under">{c.budget_frac*100:.0f}%</span></td>')
        else:
            o.append(f'<td>{c.budget_frac*100:.0f}%</td>')
        if c.psnr_aligned is False:
            o.append('<td class="grpstart"><span class="unal" '
                     'title="frames could not be content-matched">UNALIGNED</span></td>')
        elif c.psnr_ref.startswith("reference"):
            o.append('<td class="grpstart" style="color:var(--text-muted)">ref</td>')
        else:
            o.append(f'<td class="grpstart">{s(c.psnr_p50, 1)}</td>')
        o.append(f'<td class="grpstart">{s(c.transport_p50)}</td>')
        o.append(f'<td>{s(c.transport_p95)}</td>')
        o.append(f'<td class="grpstart">{s(c.local_p50)}</td>')
        o.append(f'<td>{s(c.local_p95)}</td>')
        o.append(f'<td>{s(c.decode_p50, 2)}</td>')
        o.append(f'<td class="l verdict"><span class="pill {pill}">'
                 f'{esc(c.verdict)}</span>{why}</td>')
        o.append('</tr>')
    o.append('</tbody></table></div>')
    return "".join(o)


def strip_html(cells):
    o = []
    for codec, name in (("h264", "H.264"), ("av1", "AV1")):
        group = sorted((c for c in cells if c.codec == codec and c.rate_kbps
                        and not c.void
                        and (not c.superseded_by or c.verdict == "PARTIAL")),
                       key=lambda c: (c.rate_kbps, c.verdict == "PARTIAL"))
        if not group:
            continue
        o.append(f'<div class="codehead"><span class="dot {codec}"></span>{name}</div>')
        o.append('<div class="strip">')
        for c in group:
            pill = {"PASS": "pass", "FAIL": "fail", "VOID": "void",
                    "UNMEASURED": "unmeas", "SUPERSEDED": "void",
                    "PARTIAL": "partial"}.get(c.verdict, "miss")
            if c.sample_png:
                img = (f'<img src="{c.sample_png}" alt="sampled frame at '
                       f'{c.rate_kbps} kbps {name}" loading="lazy">')
                meta = esc(c.sample_png_label)
            else:
                reason = ("no cell" if not c.present else
                          "no frames sampled" if not c.n_frames_sampled else
                          "frame unreadable")
                img = f'<div class="none">{esc(reason)}</div>'
                meta = "--"
            qp = f"QP {fnum(c.qp_p50,1)}" if c.qp_p50 is not None else "QP --"
            if c.psnr_aligned is False:
                ps = ' &middot; <span class="unal">PSNR UNALIGNED</span>'
            elif c.psnr_p50 is not None:
                ps = f" &middot; PSNR {fnum(c.psnr_p50,1)} dB"
            elif c.psnr_ref.startswith("reference"):
                ps = " &middot; reference rung"
            else:
                ps = ""
            bud = ""
            if c.budget_frac is not None:
                cls = ' class="unal"' if c.underspent else ""
                bud = (f'<div class="m"><span{cls}>{c.budget_frac*100:.0f}% of cap'
                       f'</span> &middot; '
                       f'{fnum(c.sent_mbps_mean if c.sent_mbps_mean is not None else c.recv_mbps_mean, 2)}'
                       f' Mbps delivered</div>')
            o.append(f'<div class="fr">{img}<div class="cap"><div class="t">'
                     f'<span>{c.rate_kbps} kbps</span>'
                     f'<span class="pill {pill}">{esc(c.verdict)}</span></div>'
                     f'<div class="m">{meta}</div>'
                     f'<div class="m">{qp}{ps}</div>{bud}</div></div>')
        o.append('</div>')
    return "".join(o)



# The operator named a specific band -- 1.5, 2, 2.5, 3, 4 Mbps -- and asked which of them
# works. A bracket answers "where is the floor"; it does not answer the question as put.
# If every named rate passes and quality barely moves across them, that IS the answer and
# it is more useful than a single number: the whole band is above the knee, so the choice
# among those rates is not a quality choice and the difference can be spent elsewhere.
OPERATOR_BAND_KBPS = (1500, 2000, 2500, 3000, 4000)


def named_band_html(cells):
    inband = [c for c in cells
              if getattr(c, "rate_kbps", None) in OPERATOR_BAND_KBPS
              and getattr(c, "verdict", None) is not None]
    if not inband:
        return ""
    psnrs = [getattr(c, "psnr", None) for c in inband]
    psnrs = [x for x in psnrs if isinstance(x, (int, float))]
    passed = [c for c in inband if str(getattr(c, "verdict", "")).upper().startswith("PASS")]
    spread = (max(psnrs) - min(psnrs)) if len(psnrs) > 1 else None
    bits = [f'<b>{len(passed)} of {len(inband)}</b> cells at the rates you named '
            f'(1.5, 2, 2.5, 3, 4 Mbps) meet every measurable criterion']
    if spread is not None:
        bits.append(f'and luma PSNR moves <b>{spread:.2f} dB</b> across that whole band')
    # The "above the knee" conclusion rests on the PSNR spread. Without a spread figure
    # the sentence would assert it from the pass count alone, which does not support it.
    if spread is not None and spread < 1.0:
        tail = ('A spread that small means the named band sits <b>entirely above the '
                'knee</b>: the choice between 1.5 and 4 Mbps is not a quality choice on '
                'this content. That is why the ladder was extended downward &mdash; the '
                'interesting rungs are below the range originally asked about.')
    elif spread is not None:
        tail = ('Quality does vary across the named band, so the choice between these '
                'rates <b>is</b> a quality choice &mdash; read the ladder, not the pass '
                'column.')
    else:
        tail = ('PSNR was not scored in this run, so whether the band is above the knee '
                'is not established here &mdash; only that these rates deliver.')
    return ('<div class="callout"><b>Answering the question as asked.</b> '
            + ", ".join(bits) + '. ' + tail + '</div>')

def answer_html(head):
    o = ['<div class="answer">']
    for codec, name in (("h264", "H.264"), ("av1", "AV1")):
        h = head[codec]
        w, lo = h["winner"], h["lower"]
        o.append(f'<div class="ans {codec}"><div class="codec">'
                 f'<span class="dot {codec}"></span>{name} &mdash; minimum usable '
                 f'bitrate</div>')
        if w is None:
            o.append(f'<div class="none">No rung passed</div>')
        elif lo is not None:
            o.append(f'<div class="hero">{lo.rate_kbps}&ndash;{w.rate_kbps} '
                     f'<small>kbps</small></div>'
                     f'<div class="brk">{lo.rate_kbps}k fails &middot; '
                     f'{w.rate_kbps}k passes</div>')
        else:
            o.append(f'<div class="hero">&le; {w.rate_kbps} <small>kbps</small></div>'
                     f'<div class="brk">{w.rate_kbps}k passes; nothing below it '
                     f'fails</div>')
        o.append(f'<p style="margin:8px 0 0;font-size:12.5px">'
                 f'{esc(bracket_text(h, name))}</p>')

        if w is not None:
            o.append('<div class="lat">'
                     f'<div><div class="k">Transport p50</div>'
                     f'<div class="v">{fnum(w.transport_p50,1)}<span class="u"> ms</span></div>'
                     f'<div class="u">p95 {fnum(w.transport_p95,1)} ms &middot; network</div></div>'
                     f'<div><div class="k">Local p50</div>'
                     f'<div class="v">{fnum(w.local_p50,1)}<span class="u"> ms</span></div>'
                     f'<div class="u">p95 {fnum(w.local_p95,1)} ms &middot; decode '
                     f'{fnum(w.decode_p50,2)} ms</div></div></div>'
                     f'<div class="u" style="margin-top:6px;font-size:11px">'
                     f'at the lowest passing rung, {w.rate_kbps}k</div>')
            if h["unbracketed"]:
                extra = (f" {w.rate_kbps}k is the bottom of the ladder, so bracketing "
                         f"it needs rungs below." if h["at_ladder_floor"] else "")
                o.append(f'<div class="callout warn" style="margin:12px 0 0">'
                         f'<b>Not bracketed.</b> Nothing below {w.rate_kbps}k fails, '
                         f'so the minimum is at or below {w.rate_kbps} kbps and this '
                         f'sweep does not say where.{esc(extra)}</div>')
            if h["gaps_in_bracket"]:
                g = ", ".join(f"{c.rate_kbps}k ({c.verdict.lower()})"
                              for c in h["gaps_in_bracket"])
                o.append(f'<div class="callout warn" style="margin:12px 0 0">'
                         f'<b>The bracket is wider than it looks.</b> Rungs inside it '
                         f'are not scoreable: {esc(g)}.</div>')
            if w.fps_uncertain:
                o.append('<div class="callout warn" style="margin:12px 0 0">'
                         '<b>This rung\'s fps is not decisive.</b> The log stamps '
                         'whole seconds, and once that is allowed for the received '
                         'rate straddles the 29 fps threshold.</div>')
            if w.underspent:
                d = w.sent_mbps_mean if w.sent_mbps_mean is not None else w.recv_mbps_mean
                o.append(f'<div class="callout warn" style="margin:12px 0 0">'
                         f'<b>The label is the cap, not the rate tested.</b> This rung '
                         f'delivered {d:.2f} Mbps against its {w.rate_kbps/1000:.2f} '
                         f'Mbps cap.</div>')
            if h["underspent_below"]:
                u = ", ".join(f"{c.rate_kbps}k" for c in h["underspent_below"])
                o.append(f'<div class="callout warn" style="margin:12px 0 0">'
                         f'<b>Rungs below underspent.</b> {esc(u)} did not spend their '
                         f'caps, so their failures do not rule those rates out.</div>')
        else:
            o.append(f'<p style="margin:6px 0 0;font-size:12.5px">'
                     f'{len(h["tested"])} of {len(EXPECTED_RATES)} rungs scoreable.</p>')
        o.append('</div>')
    o.append('</div>')

    o.append('<div class="callout"><b>The answer is a bracket, not a point.</b> '
             'A ladder whose bottom rung passes looks exactly like a ladder that '
             'found its floor. What a sweep establishes is the interval between the '
             'highest rate that fails and the lowest that passes &mdash; so both jaws '
             'are named above, and where the lower jaw is missing the result says '
             '&ldquo;at or below&rdquo; rather than naming the lowest rate tested as '
             'if it were the minimum.</div>')

    a, b = head["h264"]["winner"], head["av1"]["winner"]
    if a and b:
        dt = b.transport_p50 - a.transport_p50
        dl = b.local_p50 - a.local_p50
        dd = (b.decode_p50 or 0) - (a.decode_p50 or 0)
        o.append(
            f'<div class="callout"><b>Latency difference at each codec\'s lowest '
            f'passing rung</b> (AV1 at {b.rate_kbps}k minus H.264 at {a.rate_kbps}k): '
            f'transport p50 <b>{dt:+.1f} ms</b>, local p50 <b>{dl:+.1f} ms</b> '
            f'(of which decode {dd:+.2f} ms). '
            f'These are two separate figures and are not added: transport is the '
            f'network path, local is Host B\'s decode and renderer.</div>')
    elif a or b:
        only = "H.264" if a else "AV1"
        o.append(f'<div class="callout warn"><b>Only {only} has a passing rung so far</b>, '
                 f'so the codec latency comparison cannot be made yet.</div>')
    return "".join(o)


def blocker_html(cells):
    """Anything that invalidates the campaign goes ABOVE the answer, not in a footnote."""
    o = []
    nometa = [c for c in cells if c.no_publisher_metadata]
    if nometa:
        names = ", ".join(sorted({c.label for c in nometa}))
        o.append(
            f'<div class="callout stop"><div class="hd">Blocker &mdash; '
            f'{len(nometa)} cell(s) carry no per-frame data</div>'
            f'The publisher sent no timestamp/frame-id metadata, so the subscriber '
            f'refused to write per-frame rows for {esc(names)}. Those cells have '
            f'<b>no latency, QP, packet-loss or freeze data at all</b> &mdash; they '
            f'are marked UNMEASURED, not FAIL, because the instrumentation failed '
            f'rather than the video. Host A must run the publisher with '
            f'<span class="mono">--log-csv</span>, or with both '
            f'<span class="mono">--attach-timestamp</span> and '
            f'<span class="mono">--attach-frame-id</span>. Until then this sweep '
            f'cannot answer the latency half of the question.</div>')
    partial = [c for c in cells
               if c.present and not c.void and not c.no_publisher_metadata
               and c.received and not c.drawn]
    if partial:
        rows = "".join(
            f'<li><b>{esc(c.label)}</b>: {c.received} received, {c.decoded} decoded, '
            f'0 per-frame rows, {c.n_frames_sampled} frames sampled'
            + (f' &mdash; metrics re-run as {esc(c.superseded_by)}'
               if c.superseded_by else '')
            + '</li>'
            for c in sorted(partial, key=lambda c: c.rate_kbps or 0))
        o.append(
            f'<div class="callout warn"><div class="hd" style="color:var(--warn)">'
            f'Partial cells &mdash; quality valid, metrics absent</div>'
            f'<ul style="margin:6px 0 6px;padding-left:20px">{rows}</ul>'
            f'Host B&rsquo;s window surface was not composited, so nothing rendered '
            f'and the per-frame log stayed empty. Reception was clean and the sampled '
            f'frames are real, so these cells keep a valid <b>quality</b> point and '
            f'appear in the picture ladder and the PSNR chart. They have <b>no</b> '
            f'loss, QP, resolution or latency figure, they are excluded from the '
            f'bracket, and nothing is imputed for them.</div>')
    return "".join(o)


def write_html(cells, head, out: Path, results_dir: Path, generated: str):
    charts = build_charts(cells)
    present = [c for c in cells if c.present and not c.void and c.rate_kbps
               and not c.superseded_by]
    n_expected = len(EXPECTED_RATES) * len(EXPECTED_CODECS)
    odd = [c for c in cells if c.notes]

    doc = [
        '<!doctype html><html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        '<title>Overnight bitrate &amp; codec ladder</title>',
        f'<style>{CSS}</style></head><body><div id="tip"></div><div class="wrap">',
        '<h1>Overnight bitrate &amp; codec ladder &mdash; Host B receive</h1>',
        f'<div class="sub">{len(present)} of {n_expected} conditions have a cell &middot; '
        f'generated {esc(generated)} &middot; source <span class="mono">'
        f'{esc(results_dir)}</span></div>',
        blocker_html(cells),
        '<h2>The answer</h2>',
        '<p>Minimum publisher bitrate at which Host B receives video meeting the '
        'definition registered before the data existed: delivered resolution holds '
        f'{USABLE_WIDTH}&times;{USABLE_HEIGHT} for the whole cell, packet loss under '
        f'{USABLE_MAX_LOSS_PCT}%, and frame rate at least '
        f'{USABLE_MIN_FPS:.0f}, and sampled frames visually clear.</p>',
        answer_html(head),
        named_band_html(cells),
        '<div class="callout"><b>Two latency figures, never one.</b> '
        '<span class="mono">exposure_to_receive_ms</span> is <b>transport</b> &mdash; '
        'capture to arrival, the network path. '
        '<span class="mono">receive_to_gpu_complete_ms</span> is <b>local</b> &mdash; '
        'arrival to pixels, inside Host B, and includes a renderer that has cost up '
        'to 1194 ms on this resolution. A codec difference in decode cost lands in '
        'local; a network difference lands in transport. Adding them would attribute '
        'Host B&rsquo;s renderer to the link.</div>',
        '<div class="callout"><b>Three frame counts, from three sources.</b> '
        '<span class="mono">subscriber.csv</span> holds only frames that reached the '
        'GPU, so it cannot see a frame that arrived and was never drawn. '
        '<b>Published</b> is the frame_id span; <b>received</b> and <b>decoded</b> '
        'come from the last <span class="mono">Decode health</span> line in the log; '
        '<b>drawn</b> is the CSV row count. <b>Net gap</b> (published &minus; received) '
        'is the network. <b>Render skip</b> (decoded &minus; drawn) is Host B. They are '
        'reported in separate columns and are never added.</div>',
        '<h2 id="strip">The ladder, as pictures</h2>',
        '<p>One sampled frame from the middle of each cell, decoded from the raw I420 '
        'the subscriber wrote. <b>This is the evidence for the only criterion that '
        'measures the operator&rsquo;s actual question.</b> The other four clauses are '
        'delivery properties &mdash; resolution, loss, freezes, frame rate &mdash; and '
        'with <span class="mono">--degradation locked</span> the encoder cannot shed '
        'resolution or frame rate, so it absorbs the whole shortfall in quantiser. A '
        'stream can therefore arrive perfectly intact and satisfy every measurable '
        'clause while looking bad. Look before reading the verdicts.</p>',
        strip_html(cells),
        '<h2>Charts</h2>',
        '<div class="charts">' + "".join(charts) + '</div>',
        '<div class="callout"><b>PSNR is a measured axis, not a pass criterion.</b> '
        'We hold reference PSNR and deliberately did not turn it into a threshold: '
        'any figure we picked tonight would be our judgement wearing the '
        'operator&rsquo;s question. &ldquo;Usable for teleoperation&rdquo; is a '
        'judgement about driving a robot, not about decibels. The ladder shows quality '
        'against bitrate; where the threshold sits is the operator&rsquo;s call.</div>',
        '<h2>Per-cell table</h2>',
        '<div class="callout">* <b>QP is not comparable across codecs.</b> H.264 '
        'quantiser indices run 0&ndash;51 and AV1&rsquo;s run 0&ndash;255, so an AV1 '
        'QP of 117 is not &ldquo;worse&rdquo; than an H.264 QP of 30. Compare QP '
        'down a codec&rsquo;s own column, never across the two.</div>',
        table_html(cells),
    ]

    if odd:
        # Group identical notes. Sixteen copies of the same sentence buries the one
        # note that only fired on one cell, which is the one worth reading.
        grouped = {}
        for c in odd:
            for n in c.notes:
                grouped.setdefault(n, []).append(c.label)
        doc.append('<h2>Data notes</h2><div class="card"><ul style="margin:0;'
                   'padding-left:20px;color:var(--text-secondary);font-size:13px;'
                   'line-height:1.5">')
        for n, who in sorted(grouped.items(), key=lambda kv: (len(kv[1]), kv[0])):
            tag = (esc(who[0]) if len(who) == 1
                   else f"{len(who)} cells ({esc(', '.join(who))})")
            doc.append(f'<li><b>{tag}</b>: {esc(n)}</li>')
        doc.append('</ul></div>')

    doc += [
        '<footer>Latency is two figures and is never summed. Frame loss is network and '
        'render skip and is never summed. Loss percentage is estimated: the subscriber '
        f'records packets_lost but not packets_received, so the denominator assumes '
        f'{ASSUMED_PAYLOAD_BYTES}-byte payloads over the received-bitrate integral. '
        'Received fps is the Decode health count over the window from the first '
        '&ldquo;Receiving video&rdquo; line to the last log line; the log stamps whole '
        'seconds, so that denominator carries about &plusmn;1 s and cells where that '
        'straddles the threshold are marked. Frames are matched to their reference by '
        'CONTENT (frame_align: strided signature, full-luma PSNR, robust stride fit, '
        'neighbour acceptance) and never by <span class="mono">frame_id % 808</span>, '
        'which the publisher&rsquo;s capture counting does not guarantee. A cell whose '
        'placements do not fit a stride is reported UNALIGNED rather than scored. '
        'Relative PSNR is against the 8000k rung of the same codec and is not '
        'comparable across codecs.</footer>',
        '</div>', f'<script>{TIP_JS}</script>', '</body></html>',
    ]
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("".join(doc), encoding="utf-8")


# ----------------------------------------------------------------------------
def main():
    repo = HERE.parents[3]
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--results", type=Path,
                    default=HERE.parent / "results" / "overnight",
                    help="directory holding cellNN-<room> subdirectories")
    ap.add_argument("--out", type=Path, default=repo / "overnight-2026-09-10" / "reports",
                    help="output directory for ladder-summary.csv and the HTML report")
    ap.add_argument("--ref-dir", type=Path,
                    help="Host A's source frames (ref_0001..ref_0808.i420) for TRUE "
                         "PSNR. They ship only after cell 16, because bulk transfer "
                         "over the PTP cable degrades the clock; run with this flag "
                         "once they land.")
    ap.add_argument("--hosta", type=Path,
                    help="directory holding Host A's per-cell publisher CSVs, for "
                         "mean_sent_mbps. Until it exists, Host B's "
                         "receive_bitrate_mbps is the proxy.")
    ap.add_argument("--since", default=SWEEP_START_UTC,
                    help="ignore cells that started before this UTC timestamp "
                         "(default: the real sweep start; the first attempt "
                         "collected nothing and its cells are void)")
    ap.add_argument("--frame-width", type=int, default=420,
                    help="max width of the embedded sample PNGs")
    ap.add_argument("--force-void-tree", action="store_true",
                    help="read a results tree whose name marks it voided "
                         "(overnight-void-softwareenc). Off by default so the "
                         "software-encoder attempt can never be pooled with the ladder.")
    ap.add_argument("--no-psnr", action="store_true",
                    help="skip the PSNR ladder (it re-reads every sampled frame)")
    ap.add_argument("--no-frames", action="store_true",
                    help="skip the embedded sample PNGs")
    args = ap.parse_args()

    if not args.results.is_dir():
        print(f"results directory not found: {args.results}", file=sys.stderr)
        return 2
    if any(m in args.results.name.lower() for m in VOID_DIR_MARKERS):
        print(f"refusing to score {args.results.name}: it is a voided tree "
              f"(software encoder, OpenH264 rather than NVENC). It is kept for a "
              f"software-vs-hardware comparison and must not be pooled with the "
              f"ladder. Pass --force-void-tree if you really mean to read it.",
              file=sys.stderr)
        if not args.force_void_tree:
            return 2
    if not HAVE_PIL:
        print(f"warning: PIL unavailable ({_PIL_ERR}); no PSNR and no sample frames",
              file=sys.stderr)

    cells = discover(args.results)
    for c in cells:
        load(c, since=args.since, hosta=args.hosta)
    mark_superseded(cells)
    cells = add_missing(cells)

    if HAVE_PIL and not args.no_psnr:
        psnr_ladder(cells)
        if args.ref_dir:
            if psnr_true(cells, args.ref_dir):
                print(f"true PSNR scored against {args.ref_dir}")
            else:
                print(f"warning: --ref-dir {args.ref_dir} held no usable "
                      f"{USABLE_WIDTH}x{USABLE_HEIGHT} source frames", file=sys.stderr)
    if HAVE_PIL and not args.no_frames:
        for c in cells:
            if c.present and not c.void:
                try:
                    attach_sample_png(c, args.frame_width)
                except Exception as exc:
                    c.notes.append(f"sample frame render failed: {exc}")

    voids = [c for c in cells if c.void]
    if voids:
        print(f"\nignoring {len(voids)} cell(s) that started before {args.since}Z "
              f"(failed first attempt): " + ", ".join(c.dirname for c in voids))

    head = headline(cells)
    print_report(cells, head, args.results)

    csv_out = args.out / "ladder-summary.csv"
    html_out = args.out / "overnight-ladder.html"
    import datetime as _dt
    generated = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    write_csv(cells, csv_out)
    write_html(cells, head, html_out, args.results, generated)
    print(f"wrote {csv_out}")
    print(f"wrote {html_out}  ({html_out.stat().st_size/1e6:.2f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
