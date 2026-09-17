#!/usr/bin/env python3
"""Generate a concise PDF report from local_video per-frame CSV logs."""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

try:
    from reportlab.lib.colors import HexColor, white
    from reportlab.lib.pagesizes import landscape, letter
    from reportlab.pdfgen import canvas
except ImportError as error:
    raise SystemExit(
        "reportlab is required; install it with: python3 -m pip install reportlab"
    ) from error


NAVY = HexColor("#102A43")
BLUE = HexColor("#147D92")
CYAN = HexColor("#2CB1BC")
INK = HexColor("#243B53")
MUTED = HexColor("#627D98")
GRID = HexColor("#D9E2EC")
PANEL = HexColor("#F0F4F8")
RED = HexColor("#D64545")
ORANGE = HexColor("#E88D14")
PIPELINE_COLORS = (
    HexColor("#F6C344"),
    HexColor("#F29E4C"),
    HexColor("#E76F51"),
    HexColor("#C8553D"),
    HexColor("#8E5BD9"),
    HexColor("#4C78A8"),
    HexColor("#3A86FF"),
    HexColor("#2CB1BC"),
    HexColor("#04b034"),
    HexColor("#4ECDC4"),
    HexColor("#6C63FF"),
)


@dataclass(frozen=True)
class LogData:
    kind: str
    path: Path
    rows: list[dict[str, str]]
    latency_column: str
    interval_column: str

    @property
    def label(self) -> str:
        return "Publisher" if self.kind == "publisher" else "Subscriber"


@dataclass(frozen=True)
class Event:
    elapsed_ms: float
    count: int
    duration_ms: float = 0.0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate a PDF from publisher and/or subscriber --log-csv output."
    )
    parser.add_argument("--publisher", type=Path, help="Publisher CSV log")
    parser.add_argument("--subscriber", type=Path, help="Subscriber CSV log")
    parser.add_argument("-o", "--output", type=Path, help="Output PDF path")
    parser.add_argument("--title", default="Video Metrics")
    parser.add_argument(
        "--publisher-stats",
        type=Path,
        help="Publisher stats .jsonl. Supplies the SEND side -- frames encoded and sent, "
        "retransmissions, PLIs -- so the report can state what fraction of what the "
        "publisher sent actually arrived. Without it a subscriber report can only say "
        "how many frames it saw, never what it was owed.",
    )
    parser.add_argument(
        "--subscriber-log",
        type=Path,
        help="Subscriber stdout log. Supplies the SDK's received/decoded counters, which "
        "the CSV cannot: the CSV holds only frames that reached the GPU, so without this "
        "the report can show frames lost end-to-end but not WHERE they were lost.",
    )
    parser.add_argument(
        "--modem-rates-a",
        type=Path,
        help="Host A's per-second DLF record counts (dlf-rates CSV). Adds the modem page: "
        "what the publisher's modem was doing, second by second, against the same timeline "
        "as the latency chart.",
    )
    parser.add_argument(
        "--modem-rates-b",
        type=Path,
        help="Host B's per-second DLF record counts (dlf-rates CSV). Same, for the subscriber's modem.",
    )
    args = parser.parse_args()
    if args.publisher is None and args.subscriber is None:
        parser.error("at least one of --publisher or --subscriber is required")
    if args.output is None:
        source = args.subscriber or args.publisher
        assert source is not None
        args.output = source.with_suffix(".pdf")
    return args


@dataclass(frozen=True)
class PublisherStats:
    """Send-side totals over the window the subscriber actually observed.

    Read as deltas between the first and last poll inside that window, not as the file's
    final values: the publisher's run and the subscriber's rarely coincide, and taking
    end-of-file totals silently credits the subscriber with frames sent before it joined.
    """

    frames_encoded: int
    frames_sent: int
    packets_sent: int
    retransmitted: int
    key_frames: int
    pli_count: int
    mean_qp: float
    target_bitrate_mbps: float
    sent_mbps: float
    quality_limitation: str


def read_publisher_stats(
    path: Path, window_us: tuple[int, int] | None
) -> PublisherStats | None:
    try:
        records = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines()
            if line.strip()
        ]
    except (OSError, json.JSONDecodeError):
        return None
    polls = [r for r in records if r.get("video_out") and r.get("t_unix_us")]
    if window_us is not None:
        start, end = window_us
        # 2 s of slack before the first capture and 12 s after the last, so the window
        # covers the subscriber's join and the publisher's trailing poll.
        polls = [r for r in polls if start - 2_000_000 <= r["t_unix_us"] <= end + 12_000_000]
    if len(polls) < 2:
        return None
    first, last = polls[0]["video_out"], polls[-1]["video_out"]
    span_s = (polls[-1]["t_unix_us"] - polls[0]["t_unix_us"]) / 1e6
    delta = lambda key: int(last.get(key, 0)) - int(first.get(key, 0))
    encoded = delta("frames_encoded")
    return PublisherStats(
        frames_encoded=encoded,
        frames_sent=delta("frames_sent"),
        packets_sent=delta("packets_sent"),
        retransmitted=delta("retransmitted_packets_sent"),
        key_frames=delta("key_frames_encoded"),
        pli_count=delta("pli_count"),
        mean_qp=delta("qp_sum") / encoded if encoded else 0.0,
        target_bitrate_mbps=float(last.get("target_bitrate_bps", 0.0)) / 1e6,
        sent_mbps=delta("bytes_sent") * 8 / 1e6 / span_s if span_s > 0 else 0.0,
        quality_limitation=str(last.get("quality_limitation_reason", "-")),
    )


DECODE_HEALTH_RE = re.compile(
    r"received=(\d+), decoded=(\d+), keyframes_decoded=(\d+), rendered=(\d+), dropped=(\d+)"
)


@dataclass(frozen=True)
class DecodeCounters:
    received: int
    decoded: int
    keyframes: int
    dropped: int


def read_decode_counters(path: Path) -> DecodeCounters | None:
    """Last decode-health line from the subscriber log.

    These counters are the only way to separate network loss from local loss. The CSV
    has one row per GPU-rendered frame, so a frame that never arrived and a frame that
    arrived, decoded and was never drawn are both simply absent from it.
    """
    try:
        matches = DECODE_HEALTH_RE.findall(path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return None
    if not matches:
        return None
    received, decoded, keyframes, _rendered, dropped = (int(v) for v in matches[-1])
    return DecodeCounters(received, decoded, keyframes, dropped)


def number(value: str | None) -> float | None:
    if value is None or not value.strip():
        return None
    try:
        parsed = float(value)
    except ValueError:
        return None
    return parsed if math.isfinite(parsed) else None


def values(rows: Iterable[dict[str, str]], column: str) -> list[float]:
    return [parsed for row in rows if (parsed := number(row.get(column))) is not None]


def summed_values(
    rows: Iterable[dict[str, str]], columns: Sequence[str]
) -> list[float]:
    samples = []
    for row in rows:
        components = [number(row.get(column)) for column in columns]
        if all(component is not None for component in components):
            samples.append(sum(component for component in components if component is not None))
    return samples


def first_available_column(
    fieldnames: Sequence[str], candidates: Sequence[str]
) -> str | None:
    return next((column for column in candidates if column in fieldnames), None)


def read_log(path: Path, kind: str) -> LogData:
    with path.open(newline="", encoding="utf-8") as source:
        reader = csv.DictReader(source)
        fieldnames = reader.fieldnames or []
        if kind == "publisher":
            latency_column = "capture_to_packetize_ms"
            interval_column = "packetize_interval_ms"
        else:
            latency_column = first_available_column(
                fieldnames, ("e2e_to_gpu_complete_ms", "e2e_latency_ms")
            )
            interval_column = first_available_column(
                fieldnames, ("gpu_complete_interval_ms", "render_interval_ms")
            )
            if latency_column is None:
                latency_column = "e2e_to_gpu_complete_ms"
            if interval_column is None:
                interval_column = "gpu_complete_interval_ms"
        required = {"elapsed_ms", "frame_id", latency_column}
        missing = required.difference(fieldnames)
        if missing:
            raise ValueError(f"{path} is not a {kind} frame log; missing {', '.join(sorted(missing))}")
        rows = [row for row in reader if number(row.get(latency_column)) is not None]
    if not rows:
        raise ValueError(f"{path} contains no completed {kind} frame samples")

    # A frame occasionally reaches the CSV with a zero timestamp -- 17 of 4587 rows in
    # e2s-500k-r1 have capture_timestamp_us=0. Any stage measured from that column then
    # differs by the whole Unix epoch, and because those rows are rare they leave the
    # median untouched while destroying the mean: that run reported a
    # capture-to-packetize mean of 6,629,942,825 ms against a p50 of 1.1 ms. A number
    # that wrong is still a number, and it goes in a table a reader will quote.
    # Percentiles hid it, which is the same trap this programme hit with stall episodes.
    dropped = [row for row in rows if _has_epoch_artifact(row)]
    if dropped:
        rows = [row for row in rows if not _has_epoch_artifact(row)]
        print(
            f"warning: {path.name}: dropped {len(dropped)} of {len(dropped) + len(rows)} "
            f"rows with a zero timestamp (epoch-scale stage durations)",
            file=sys.stderr,
        )
        if not rows:
            raise ValueError(f"{path} contains no rows with usable timestamps")
    return LogData(kind, path, rows, latency_column, interval_column)


def _has_epoch_artifact(row: dict[str, str]) -> bool:
    """True if any absolute timestamp on this row is missing or zero.

    Test the CAUSE, not the symptom. The first version of this guard also rejected any
    `_ms` column above 60 s as non-physical, which is true of a stage duration and false
    of `elapsed_ms` and `total_freeze_duration_ms` -- both cumulative, both legitimately
    past 60 s. That guard would have silently discarded every row after the first minute
    of every run longer than a minute, which is most of them.
    """
    for column, value in row.items():
        if column.endswith("_timestamp_us"):
            parsed = number(value)
            if parsed is None or parsed <= 0:
                return True
    return False


@dataclass(frozen=True)
class ResolutionTrack:
    """Delivered resolution over a run, from the subscriber CSV.

    WebRTC downscales under rate control, so a run's configured resolution is not
    necessarily what arrived. A report that omits this cannot distinguish a faster
    pipeline from a smaller picture -- which is exactly how a load ladder was drawn
    across three nominal resolutions that all delivered the same one.
    """

    spans: list[tuple[str, int]]          # (WxH, frame count), in order of appearance
    modal: str | None
    modal_frames: int
    total: int
    first_change_s: float | None

    @property
    def changed(self) -> bool:
        return len(self.spans) > 1

    def summary(self) -> str:
        if not self.spans:
            return "resolution not recorded"
        if not self.changed:
            return f"{self.modal} held for all {self.total:,} frames"
        pct = 100.0 * self.modal_frames / self.total if self.total else 0.0
        when = f" after {self.first_change_s:.2f}s" if self.first_change_s is not None else ""
        return (
            f"CHANGED{when}: "
            + " -> ".join(f"{res} x{count:,}" for res, count in self.spans[:5])
            + (" ..." if len(self.spans) > 5 else "")
            + f"  (modal {self.modal}, {pct:.1f}% of frames)"
        )


def resolution_track(log: LogData) -> ResolutionTrack:
    """Collapse consecutive equal resolutions into spans."""
    spans: list[list] = []
    counts: dict[str, int] = {}
    first_change: float | None = None
    start_ms: float | None = None
    previous: str | None = None
    for row in log.rows:
        width, height = row.get("frame_width"), row.get("frame_height")
        # 0x0 appears on the first frames of a START_FRAME=0 run: the sink has not yet
        # learned the dimensions. It is not a resolution the stream ever delivered, and
        # counting it puts a fake first step in the header.
        if not width or not height or width == "0" or height == "0":
            continue
        res = f"{width}x{height}"
        counts[res] = counts.get(res, 0) + 1
        elapsed = number(row.get("elapsed_ms"))
        if start_ms is None and elapsed is not None:
            start_ms = elapsed
        if res != previous:
            if previous is not None and first_change is None and elapsed is not None and start_ms is not None:
                first_change = (elapsed - start_ms) / 1000.0
            spans.append([res, 0])
            previous = res
        spans[-1][1] += 1
    total = sum(counts.values())
    modal = max(counts, key=lambda k: counts[k]) if counts else None
    return ResolutionTrack(
        spans=[(res, n) for res, n in spans],
        modal=modal,
        modal_frames=counts.get(modal, 0) if modal else 0,
        total=total,
        first_change_s=first_change,
    )


@dataclass(frozen=True)
class ResolutionPairing:
    """Encoder-side against decoder-side resolution, joined on frame ID.

    The publisher records what the encoder emitted; the subscriber records what the
    decoder produced. Comparing them separates "the encoder shed resolution" from
    "it was lost in transit" without inference -- and the disagreement, if any, is
    the only thing that distinguishes those two stories.
    """

    paired: int
    agreeing: int
    disagreements: list[tuple[int, str, str]]   # (frame_id, encoded, delivered)

    @property
    def available(self) -> bool:
        return self.paired > 0

    def summary(self) -> str:
        if not self.available:
            return "encoder-side resolution not recorded (publisher predates the column)"
        if not self.disagreements:
            return f"encoder and decoder agree on all {self.paired:,} paired frames"
        pct = 100.0 * len(self.disagreements) / self.paired
        first = self.disagreements[0]
        return (
            f"DISAGREE on {len(self.disagreements):,}/{self.paired:,} frames ({pct:.1f}%); "
            f"first at frame {first[0]}: encoder {first[1]}, delivered {first[2]}"
        )


def pair_resolutions(publisher: LogData, subscriber: LogData) -> ResolutionPairing:
    enc: dict[str, str] = {}
    for row in publisher.rows:
        width, height = row.get("encoded_frame_width"), row.get("encoded_frame_height")
        if width and height and row.get("frame_id"):
            enc[row["frame_id"]] = f"{width}x{height}"
    paired = 0
    agreeing = 0
    disagreements: list[tuple[int, str, str]] = []
    for row in subscriber.rows:
        fid = row.get("frame_id")
        width, height = row.get("frame_width"), row.get("frame_height")
        if not fid or not width or not height or width == "0" or height == "0" or fid not in enc:
            continue
        delivered = f"{width}x{height}"
        paired += 1
        if enc[fid] == delivered:
            agreeing += 1
        else:
            disagreements.append((int(fid), enc[fid], delivered))
    return ResolutionPairing(paired, agreeing, disagreements)


def percentile(samples: Sequence[float], percent: float) -> float:
    ordered = sorted(samples)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * percent / 100.0
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def series(log: LogData) -> list[tuple[float, float]]:
    result = []
    for row in log.rows:
        elapsed = number(row.get("elapsed_ms"))
        latency = number(row.get(log.latency_column))
        if elapsed is not None and latency is not None:
            result.append((elapsed, latency))
    return result


def gap_events(log: LogData) -> list[Event]:
    events = []
    for row in log.rows:
        elapsed = number(row.get("elapsed_ms"))
        gap = number(row.get("frame_id_gap"))
        if elapsed is not None and gap is not None and gap > 0:
            events.append(Event(elapsed, round(gap)))
    return events


def inferred_freeze_events(log: LogData) -> list[Event]:
    intervals = [value for value in values(log.rows, log.interval_column) if value > 0]
    if not intervals:
        return []
    expected = statistics.median(intervals)
    threshold = expected * 3.0
    events = []
    for row in log.rows:
        elapsed = number(row.get("elapsed_ms"))
        interval = number(row.get(log.interval_column))
        if elapsed is not None and interval is not None and interval > threshold:
            events.append(Event(elapsed, 1, interval - expected))
    return events


def subscriber_freeze_events(log: LogData) -> list[Event]:
    if log.kind != "subscriber":
        return inferred_freeze_events(log)
    counts = values(log.rows, "freeze_count")
    if not counts:
        return inferred_freeze_events(log)
    events = []
    previous_count = 0
    previous_duration = 0.0
    for row in log.rows:
        elapsed = number(row.get("elapsed_ms"))
        count = number(row.get("freeze_count"))
        duration = number(row.get("total_freeze_duration_ms"))
        if elapsed is None or count is None:
            continue
        rounded_count = round(count)
        if rounded_count > previous_count:
            duration_delta = max(0.0, (duration or previous_duration) - previous_duration)
            events.append(Event(elapsed, rounded_count - previous_count, duration_delta))
        previous_count = max(previous_count, rounded_count)
        if duration is not None:
            previous_duration = max(previous_duration, duration)
    return events


@dataclass(frozen=True)
class PairedAccounting:
    """Frame accounting from the frame-ID join of the two CSVs, over the stretch both cover.

    The funnel used to take "Arrived" and "Decoded" from the subscriber log's decode
    counters. Those are sampled once a second over a different window than the CSV's
    rendered rows, so on 2026-09-15 a run with 0 packets lost showed "lost in network: 30"
    and "lost in renderer: -10". Joining on frame ID and clipping to [first, last] ID the
    subscriber drew makes every stage count the same frames.
    """

    low: int
    high: int
    sent: int             # publisher frame IDs inside [low, high]
    shown: int            # of those, IDs the subscriber drew
    before: int           # publisher IDs before low: sent before the subscriber was drawing
    after: int            # publisher IDs after high: the publisher ran past the recording
    packets_lost: int     # WebRTC cumulative counter at the subscriber's last row
    frames_dropped: int   # WebRTC cumulative frames dropped before decode

    @property
    def missing(self) -> int:
        return self.sent - self.shown

    @property
    def network(self) -> int:
        # Packets are not frames: one lost packet can cost a frame, a burst can cost one
        # frame or several. With 0 lost the network share is exactly 0; otherwise this is
        # an upper bound and is labelled as one.
        return min(self.missing, self.packets_lost)

    @property
    def decoder(self) -> int:
        return min(self.missing - self.network, self.frames_dropped)

    @property
    def renderer(self) -> int:
        return self.missing - self.network - self.decoder


def _last_counter(rows: Sequence[dict[str, str]], column: str) -> int:
    counts = values(rows, column)
    return round(max(counts)) if counts else 0


def paired_frame_accounting(publisher: LogData, subscriber: LogData) -> PairedAccounting | None:
    publisher_ids = {round(value) for value in values(publisher.rows, "frame_id")}
    subscriber_ids = {round(value) for value in values(subscriber.rows, "frame_id")}
    if not publisher_ids or not subscriber_ids:
        return None
    low = max(min(publisher_ids), min(subscriber_ids))
    high = min(max(publisher_ids), max(subscriber_ids))
    inside = {frame_id for frame_id in publisher_ids if low <= frame_id <= high}
    return PairedAccounting(
        low=low,
        high=high,
        sent=len(inside),
        shown=len(inside & subscriber_ids),
        before=sum(1 for frame_id in publisher_ids if frame_id < low),
        after=sum(1 for frame_id in publisher_ids if frame_id > high),
        packets_lost=_last_counter(subscriber.rows, "packets_lost"),
        frames_dropped=_last_counter(subscriber.rows, "frames_dropped"),
    )


def paired_loss_events(publisher: LogData, subscriber: LogData) -> list[Event]:
    publisher_ids = {round(value) for value in values(publisher.rows, "frame_id")}
    subscriber_ids = {round(value) for value in values(subscriber.rows, "frame_id")}
    if not publisher_ids or not subscriber_ids:
        return []
    low = max(min(publisher_ids), min(subscriber_ids))
    high = min(max(publisher_ids), max(subscriber_ids))
    missing_ids = {
        frame_id for frame_id in publisher_ids if low <= frame_id <= high
    } - subscriber_ids
    events = []
    for row in publisher.rows:
        frame_id = number(row.get("frame_id"))
        elapsed = number(row.get("elapsed_ms"))
        if frame_id is not None and elapsed is not None and round(frame_id) in missing_ids:
            events.append(Event(elapsed, 1))
    return events
def draw_header(pdf: canvas.Canvas, title: str, subtitle: str | Sequence[str]) -> None:
    width, height = landscape(letter)
    pdf.setFillColor(white)
    pdf.rect(0, 0, width, height, fill=1, stroke=0)
    pdf.setFillColor(NAVY)
    pdf.rect(0, height - 72, width, 72, fill=1, stroke=0)
    pdf.setFillColor(white)
    # The title is a caller-supplied label and can be long: a 10 Mbps run's title ran off
    # the right edge, taking "Host A -> Host B" with it. Shrink to fit, then truncate.
    available = width - 76
    size = 21.0
    while size > 12 and pdf.stringWidth(title, "Helvetica-Bold", size) > available:
        size -= 0.5
    while title and pdf.stringWidth(title, "Helvetica-Bold", size) > available:
        title = title[:-2] + "…"
    pdf.setFont("Helvetica-Bold", size)
    pdf.drawString(38, height - 33, title)
    pdf.setFillColor(HexColor("#D9F2F4"))
    pdf.setFont("Helvetica", 8.5)
    # One line per fact rather than one long line: a resolution change or an
    # encoder/decoder disagreement is the most important thing in the header, and
    # concatenating them ran the text off the page and clipped exactly that.
    lines = [subtitle] if isinstance(subtitle, str) else list(subtitle)
    for index, line in enumerate(lines[:3]):
        pdf.drawString(39, height - 51 - index * 10, line[:190])


def draw_frame_accounting(
    pdf: canvas.Canvas,
    x: float,
    y: float,
    width: float,
    emitted: int,
    counters: DecodeCounters,
    rendered: int,
    publisher: PublisherStats | None = None,
) -> None:
    """Where frames were lost: network, decoder, or renderer.

    The question this answers is the first one an operator asks about a degraded feed,
    and no single counter answers it. Frames absent from the CSV may never have arrived,
    may have failed to decode, or may have arrived and decoded and never been drawn --
    three different faults with three different owners.
    """
    pdf.setFillColor(INK)
    pdf.setFont("Helvetica-Bold", 10.5)
    pdf.drawString(x, y + 92, "Where frames were lost")

    # With publisher stats the funnel spans both hosts and the top figure is what the
    # ENCODER produced, measured on the far side. Without them the top is the frame-ID
    # span the subscriber observed, which cannot see frames lost before the first arrival
    # and so understates what was owed.
    if publisher is not None and publisher.frames_sent > 0:
        stages = (
            ("Encoded", publisher.frames_encoded, None),
            ("Sent", publisher.frames_sent, "encoder"),
            ("Arrived", counters.received, "network"),
            ("Decoded", counters.decoded, "decoder"),
            ("On screen", rendered, "renderer"),
        )
        emitted = publisher.frames_encoded
    else:
        stages = (
            ("Published", emitted, None),
            ("Arrived", counters.received, "network"),
            ("Decoded", counters.decoded, "decoder"),
            ("On screen", rendered, "renderer"),
        )
    bar_h, top = 15.0, y + 62
    label_w, bar_w = 74.0, width - 74.0 - 132.0
    for index, (label, value, lost_from) in enumerate(stages):
        row_y = top - index * (bar_h + 6)
        pdf.setFillColor(MUTED)
        pdf.setFont("Helvetica", 7.4)
        pdf.drawString(x, row_y + 4, label)
        share = value / emitted if emitted else 0.0
        pdf.setFillColor(PANEL)
        pdf.rect(x + label_w, row_y, bar_w, bar_h, fill=1, stroke=0)
        pdf.setFillColor(NAVY if index == 0 else BLUE)
        pdf.rect(x + label_w, row_y, max(bar_w * share, 1.0), bar_h, fill=1, stroke=0)
        pdf.setFillColor(INK)
        pdf.setFont("Helvetica-Bold", 7.4)
        pdf.drawRightString(x + label_w + bar_w + 34, row_y + 4, f"{value:,}")
        if lost_from is not None:
            previous = stages[index - 1][1]
            lost = previous - value
            pdf.setFillColor(MUTED if lost <= 0 else INK)
            pdf.setFont("Helvetica", 7.0)
            share_lost = (100.0 * lost / previous) if previous else 0.0
            pdf.drawString(
                x + label_w + bar_w + 42,
                row_y + 4,
                f"lost in {lost_from}: {lost:,} ({share_lost:.1f}%)",
            )

    if publisher is not None and publisher.frames_sent > 0:
        delivered = 100.0 * counters.received / publisher.frames_sent
        pdf.setFillColor(MUTED)
        pdf.setFont("Helvetica", 7.0)
        pdf.drawString(
            x,
            top - len(stages) * (bar_h + 6) - 4,
            f"publisher: {publisher.sent_mbps:.2f} Mbps sent, target {publisher.target_bitrate_mbps:.2f}, "
            f"QP {publisher.mean_qp:.1f}, quality_limitation {publisher.quality_limitation}, "
            f"{publisher.retransmitted:,} retransmitted packets "
            f"({100.0 * publisher.retransmitted / max(publisher.packets_sent, 1):.1f}% of sent), "
            f"{publisher.pli_count} PLIs   |   {delivered:.1f}% of sent frames arrived",
        )


def draw_paired_accounting(
    pdf: canvas.Canvas,
    x: float,
    y: float,
    width: float,
    accounting: PairedAccounting,
    publisher: PublisherStats | None = None,
) -> None:
    """Where frames were lost, from the frame-ID join rather than the decode counters.

    Every stage counts the same frames: publisher IDs inside the ID range the subscriber
    drew. Network loss comes from WebRTC's packets_lost (exactly 0 when 0 were lost, an
    upper bound otherwise), frames dropped before decode from frames_dropped, and the
    rest arrived but were never drawn.
    """
    pdf.setFillColor(INK)
    pdf.setFont("Helvetica-Bold", 10.5)
    pdf.drawString(x, y + 92, "Where frames were lost")

    a = accounting
    upper = "at most " if a.packets_lost > 0 else ""
    stages = (
        ("Sent", a.sent, None),
        ("Arrived", a.sent - a.network, f"lost in network: {upper}{a.network:,}"),
        ("Decoded", a.sent - a.network - a.decoder, f"dropped before decode: {a.decoder:,}"),
        ("On screen", a.shown, f"arrived but never drawn: {a.renderer:,}"),
    )
    bar_h, top = 15.0, y + 62
    label_w, bar_w = 74.0, width - 74.0 - 172.0
    for index, (label, value, note) in enumerate(stages):
        row_y = top - index * (bar_h + 6)
        pdf.setFillColor(MUTED)
        pdf.setFont("Helvetica", 7.4)
        pdf.drawString(x, row_y + 4, label)
        share = value / a.sent if a.sent else 0.0
        pdf.setFillColor(PANEL)
        pdf.rect(x + label_w, row_y, bar_w, bar_h, fill=1, stroke=0)
        pdf.setFillColor(NAVY if index == 0 else BLUE)
        pdf.rect(x + label_w, row_y, max(bar_w * share, 1.0), bar_h, fill=1, stroke=0)
        pdf.setFillColor(INK)
        pdf.setFont("Helvetica-Bold", 7.4)
        pdf.drawRightString(x + label_w + bar_w + 34, row_y + 4, f"{value:,}")
        if note is not None:
            lost = stages[index - 1][1] - value
            pdf.setFillColor(MUTED if lost <= 0 else INK)
            pdf.setFont("Helvetica", 7.0)
            pdf.drawString(x + label_w + bar_w + 42, row_y + 4, note)

    lines = [
        f"frame IDs {a.low:,}-{a.high:,}, the stretch both logs cover; excluded: {a.before:,} sent before "
        f"and {a.after:,} after the subscriber's recording   |   subscriber counters: "
        f"{a.packets_lost:,} packets lost, {a.frames_dropped:,} frames dropped"
    ]
    if publisher is not None and publisher.frames_sent > 0:
        lines.append(
            f"publisher: {publisher.sent_mbps:.2f} Mbps sent, target {publisher.target_bitrate_mbps:.2f}, "
            f"QP {publisher.mean_qp:.1f}, quality_limitation {publisher.quality_limitation}, "
            f"{publisher.retransmitted:,} retransmitted packets "
            f"({100.0 * publisher.retransmitted / max(publisher.packets_sent, 1):.1f}% of sent), "
            f"{publisher.pli_count} PLIs"
        )
    pdf.setFillColor(MUTED)
    pdf.setFont("Helvetica", 7.0)
    for index, line in enumerate(lines):
        pdf.drawString(x, top - len(stages) * (bar_h + 6) - 4 - index * 10, line)


def draw_card(pdf: canvas.Canvas, x: float, y: float, width: float, label: str, value: str) -> None:
    pdf.setFillColor(PANEL)
    pdf.roundRect(x, y, width, 48, 5, fill=1, stroke=0)
    pdf.setFillColor(MUTED)
    pdf.setFont("Helvetica-Bold", 6.8)
    pdf.drawString(x + 9, y + 32, label.upper())
    pdf.setFillColor(INK)
    pdf.setFont("Helvetica-Bold", 15)
    pdf.drawString(x + 9, y + 11, value)


@dataclass(frozen=True)
class ModemRates:
    """Per-second DLF record counts for one host, aligned to the video timeline.

    The rows are (second_rel_probe, code, count) where the second is already on the HOST
    clock relative to probe_start -- dlf_rates.py applies the modem-to-host offset before
    binning. So aligning to the subscriber's elapsed_ms needs only probe_start and the
    first capture timestamp; the host-minus-UTC value is metadata for the page, not a term
    in the arithmetic. That matters because the two hosts frame the offset's sign
    differently ("host_minus_utc_s=-8.98" vs "host=modem-8.98s") and this reader must not
    silently adopt either reading.
    """

    label: str
    probe_start_ms: int
    host_minus_utc_s: float | None
    per_second: dict[str, dict[int, int]]      # code -> {second_rel_probe: count}
    seconds: tuple[int, int]

    def total_per_second(self) -> dict[int, int]:
        totals: dict[int, int] = {}
        for counts in self.per_second.values():
            for second, value in counts.items():
                totals[second] = totals.get(second, 0) + value
        return totals

    def top_codes(self, n: int) -> list[str]:
        volume = {c: sum(v.values()) for c, v in self.per_second.items()}
        return sorted(volume, key=lambda c: -volume[c])[:n]


# Codes shown by raw-record analysis to be periodic heartbeats rather than traffic. A
# 50 ms window holds a fixed count regardless of radio behaviour, so any ratio is 1.00 by
# construction. Host A measured 0xB881 in c049 at 238/s with inter-arrival p10-p90 of
# 4.97-5.03 ms over 14 distinct values and zero window-count spread.
MODEM_ARTIFACT_CODES = {"0xB881"}

MODEM_NAMED_CODES = {
    "0xB872": "NR L2 UL TB",
    "0xB873": "NR L2 UL BSR",
    "0xB881": "UL TB stats",
    "0xB883": "UL sched report",
    "0xB888": "PDSCH stats",
    "0xB97F": "ML1 meas DB",
}


def read_modem_rates(path: Path, label: str) -> ModemRates | None:
    """Parse a dlf-rates CSV. Both hosts' header shapes are accepted; neither is guessed."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    probe_start_ms: int | None = None
    offset: float | None = None
    for line in text[:6]:
        if not line.startswith("#"):
            break
        if (m := re.search(r"probe_start_ms=(\d+)", line)):
            probe_start_ms = int(m.group(1))
        elif (m := re.search(r"probe_start=([0-9.]+)", line)):
            probe_start_ms = int(float(m.group(1)) * 1000)
        if (m := re.search(r"host_minus_utc_s=(-?[0-9.]+)", line)):
            offset = float(m.group(1))
        elif (m := re.search(r"host=modem(-?[0-9.]+)s", line)):
            offset = float(m.group(1))
    if probe_start_ms is None:
        return None                      # refuse rather than assume where zero sits
    per_second: dict[str, dict[int, int]] = {}
    lo, hi = 10**9, -(10**9)
    for row in csv.DictReader(line for line in text if not line.startswith("#")):
        try:
            second = int(row["second_rel_probe"])
            count = int(row["count"])
        except (KeyError, TypeError, ValueError):
            continue
        per_second.setdefault(row["code"], {})[second] = count
        lo, hi = min(lo, second), max(hi, second)
    if not per_second:
        return None
    return ModemRates(label, probe_start_ms, offset, per_second, (lo, hi))


def wrap_text(
    pdf: canvas.Canvas,
    text: str,
    x: float,
    y: float,
    width: float,
    size: float,
    leading: float,
    font: str = "Helvetica",
) -> float:
    """Draw wrapped text and return the y below it. The modem page's prose ran off the
    right edge when drawn as one drawString call."""
    pdf.setFont(font, size)
    words, line = text.split(), ""
    for word in words:
        trial = f"{line} {word}".strip()
        if pdf.stringWidth(trial, font, size) > width and line:
            pdf.drawString(x, y, line)
            y -= leading
            line = word
        else:
            line = trial
    if line:
        pdf.drawString(x, y, line)
        y -= leading
    return y


def draw_modem_page(
    pdf: canvas.Canvas,
    modems: Sequence[ModemRates],
    subscriber: LogData,
    x: float,
    width: float,
) -> None:
    """Modem activity against the video timeline, plus what it did at the late frames."""
    _, page_height = landscape(letter)
    captures = [v for v in (number(r.get("capture_timestamp_us")) for r in subscriber.rows) if v]
    if not captures:
        return
    capture0_ms = min(captures) / 1000.0
    duration_ms = max(values(subscriber.rows, "elapsed_ms"), default=0.0)
    if duration_ms <= 0:
        return

    # Late frames, on the same definition the rest of the report uses.
    e2r = values(subscriber.rows, "exposure_to_receive_ms")
    late_threshold = 2.5 * statistics.median(e2r) if e2r else float("inf")
    late_seconds: set[tuple[str, int]] = set()
    late_elapsed: list[float] = []
    for row in subscriber.rows:
        delay = number(row.get("exposure_to_receive_ms"))
        elapsed = number(row.get("elapsed_ms"))
        if delay is not None and elapsed is not None and delay >= late_threshold:
            late_elapsed.append(elapsed)

    def to_x(elapsed_ms: float) -> float:
        return x + width * max(0.0, min(1.0, elapsed_ms / duration_ms))

    def second_to_elapsed(m: ModemRates, second: int) -> float:
        return (m.probe_start_ms + second * 1000) - capture0_ms

    # A late-frame second is the second the frame landed in, not a +/-1 s neighbourhood:
    # on c046, 174 late frames over 600 s with a +/-1 s window covered most of the run, so
    # "ordinary" and "late" were the same population and every ratio came out 1.00x.
    for m in modems:
        for le in late_elapsed:
            second = math.floor((le + capture0_ms - m.probe_start_ms) / 1000.0)
            if m.seconds[0] <= second <= m.seconds[1]:
                late_seconds.add((m.label, second))

    top = page_height - 96
    pdf.setFillColor(INK)
    pdf.setFont("Helvetica-Bold", 11)
    pdf.drawString(x, top + 16, "Modem activity, second by second")
    pdf.setFont("Helvetica", 7.4)
    pdf.setFillColor(MUTED)
    blurb = (
        "Per-second DIAG RECORD COUNTS -- these are not grant sizes. The v3 record layouts need QCAT to "
        "decode, so this shows WHEN the modem's scheduling activity changed and by how much in relative "
        "terms, never the grant in bytes. Red ticks mark seconds holding a late frame."
    )
    wrap_text(pdf, blurb, x, top + 6, width, 7.4, 9.0)

    strip_h, gap = 92.0, 26.0
    row_y = top - 34
    for m in modems:
        totals = m.total_per_second()
        peak = max(totals.values()) if totals else 1
        # Scale from the INTERIOR percentiles, and drop the first and last second of the
        # capture. Those two are partial bins -- the capture starts and stops mid-second --
        # so a min/max axis is pinned by two start-up artifacts and every real variation in
        # between is flattened against the top of the box.
        interior = [
            v
            for sec, v in sorted(totals.items())[1:-1]
        ] or list(totals.values())
        interior.sort()
        lo_v = interior[int(len(interior) * 0.05)]
        hi_v = interior[int(len(interior) * 0.95)]
        if hi_v <= lo_v:
            lo_v, hi_v = min(interior), max(interior)
        pad = max(1.0, (hi_v - lo_v) * 0.20)
        base_v, span = lo_v - pad, max(1.0, (hi_v - lo_v) + 2 * pad)
        floor = min(totals.values()) if totals else 0
        pdf.setFillColor(PANEL)
        pdf.rect(x, row_y - strip_h, width, strip_h, fill=1, stroke=0)
        pdf.setFillColor(INK)
        pdf.setFont("Helvetica-Bold", 7.6)
        pdf.drawString(x, row_y + 4, f"{m.label}: all DIAG records/s")
        pdf.setFont("Helvetica", 6.8)
        pdf.setFillColor(MUTED)
        offset_note = (
            f"clock {m.host_minus_utc_s:+.2f} s vs UTC   " if m.host_minus_utc_s is not None else ""
        )
        pdf.drawRightString(
            x + width, row_y + 4, f"{offset_note}{lo_v:,}-{hi_v:,}/s typical (axis), {floor:,}-{peak:,} seen"
        )
        pdf.setStrokeColor(BLUE)
        pdf.setLineWidth(0.7)
        path = pdf.beginPath()
        started = False
        for second in sorted(totals):
            px = to_x(second_to_elapsed(m, second))
            py = row_y - strip_h + 4 + (strip_h - 10) * min(
                1.0, max(0.0, (totals[second] - base_v) / span)
            )
            if not started:
                path.moveTo(px, py)
                started = True
            else:
                path.lineTo(px, py)
        if started:
            pdf.drawPath(path)
        pdf.setStrokeColor(HexColor("#C2384A"))
        pdf.setLineWidth(0.6)
        for label, second in late_seconds:
            if label != m.label:
                continue
            px = to_x(second_to_elapsed(m, second))
            pdf.line(px, row_y - strip_h + 1, px, row_y - strip_h + 7)
        row_y -= strip_h + gap

    # What the named codes did at the late frames, versus the rest of the run.
    pdf.setFillColor(INK)
    pdf.setFont("Helvetica-Bold", 10)
    pdf.drawString(x, row_y - 2, "Named codes at the late frames")
    row_y -= 14
    # Coverage is over the span the table actually compares across: where the DLF window and
    # the video cell OVERLAP. Neither the cell length nor the capture window is right on its
    # own -- on S2 the DLF brackets a 10.9 s upload burst 119 s into a 298 s cell, so "of the
    # cell" reads 6% and "of the capture window" reads 27%, both true and neither what the
    # ratios were computed over. On a cell captured end to end the two coincide.
    def overlap_seconds(m: ModemRates) -> int:
        lo = max(second_to_elapsed(m, m.seconds[0]), 0.0)
        hi = min(second_to_elapsed(m, m.seconds[1]), duration_ms)
        return max(1, int((hi - lo) / 1000))

    # Weight by late FRAMES, not by seconds touched: a second holding 30 late frames and a
    # second holding one are not equal evidence, and on a cell whose late frames are
    # scattered singletons a raw second-count trips the guard on noise. On S2, 12 seconds
    # carry 291 of 296 in-window late frames and 4 seconds carry 5 between them.
    lead = modems[0]
    per_second_frames: dict[int, int] = {}
    for le in late_elapsed:
        sec = math.floor((le + capture0_ms - lead.probe_start_ms) / 1000.0)
        if lead.seconds[0] <= sec <= lead.seconds[1]:
            per_second_frames[sec] = per_second_frames.get(sec, 0) + 1
    in_window = sum(per_second_frames.values())
    dense = {sec: n for sec, n in per_second_frames.items() if n >= 5}
    covered = len(per_second_frames) / overlap_seconds(lead)
    pdf.setFont("Helvetica", 7.0)
    pdf.setFillColor(MUTED)
    if in_window:
        pdf.drawString(
            x,
            row_y,
            f"{len(dense)} second(s) carry {sum(dense.values()):,} of the {in_window:,} late frames in the "
            f"window the modem log and the video cell share; {len(per_second_frames) - len(dense)} further "
            f"second(s) carry {in_window - sum(dense.values()):,} between them "
            f"({covered * 100:.0f}% of seconds touched)",
        )
    row_y -= 12
    if in_window and not dense:
        wrap_text(
            pdf,
            f"WEAK EVIDENCE: the {in_window:,} late frames here are scattered singletons with no second "
            "carrying five or more, so each ratio below rests on a handful of isolated seconds. Treat a "
            "departure from 1.00 as a hint to capture a cell where late frames concentrate, not as a finding.",
            x,
            row_y,
            width,
            7.4,
            9.0,
            font="Helvetica-Bold",
        )
        row_y -= 22
    if covered > 0.5:
        wrap_text(
            pdf,
            "NOT COMPARABLE: late frames are spread over more than half the seconds in this run, so the "
            "'ordinary' and 'late' populations overlap and any ratio here would be near 1.00 by construction. "
            "The comparison needs a run where late frames are concentrated.",
            x,
            row_y,
            width,
            7.4,
            9.0,
            font="Helvetica-Bold",
        )
        return
    headers = ("host", "code", "meaning", "median/s ordinary", "median/s at late frames", "ratio")
    widths = (52.0, 52.0, 118.0, 104.0, 124.0, 50.0)
    pdf.setFont("Helvetica", 7.0)
    pdf.setFillColor(MUTED)
    cx = x
    for head, w in zip(headers, widths):
        pdf.drawString(cx, row_y, head.upper())
        cx += w
    row_y -= 3
    pdf.setStrokeColor(GRID)
    pdf.line(x, row_y, x + width, row_y)
    row_y -= 10
    for m in modems:
        codes = [c for c in MODEM_NAMED_CODES if c in m.per_second]
        codes += [c for c in m.top_codes(3) if c not in MODEM_NAMED_CODES]
        for code in codes:
            counts = m.per_second[code]
            late = [counts.get(s, 0) for (lbl, s) in late_seconds if lbl == m.label]
            ordinary = [v for s, v in counts.items() if (m.label, s) not in late_seconds]
            if not ordinary:
                continue
            med_o = statistics.median(ordinary)
            med_l = statistics.median(late) if late else float("nan")
            ratio = (med_l / med_o) if med_o else float("nan")
            # Per-capture viability, because a code's character changes between captures:
            # 0xB881 is a 5 ms heartbeat in c049 (238/s, inter-arrival p10-p90 4.97-5.03 ms,
            # every 50 ms window holding exactly 10 records, ratio structurally 1.00) and an
            # irregular 10/s stream in S1. A near-constant per-second rate is that heartbeat
            # signature and its ratio carries no information; a very low rate cannot resolve
            # a sub-second event at all. Say which, rather than print a number that looks
            # like a measurement.
            # A per-second summary CANNOT tell a heartbeat from steady traffic: 0xB881 is a
            # 5 ms heartbeat (every 50 ms window holds exactly 10 records, ratio structurally
            # 1.00) and 0xB883 is genuinely variable at 50 ms, yet both look flat at 1 s.
            # So only exclude what raw-record analysis has actually shown to be an artifact,
            # and otherwise print the ratio with the 1 s flatness flagged as unknown rather
            # than asserted. Host A established 0xB881's heartbeat by scanning c049's records.
            spread = (statistics.pstdev(ordinary) / med_o) if med_o else float("inf")
            if code in MODEM_ARTIFACT_CODES:
                note = "heartbeat - ratio meaningless"
            elif med_o < 5:
                note = "too sparse to say"
            else:
                note = ""
            if note:
                ratio = float("nan")
            flat = not note and spread < 0.02
            cells = (
                m.label,
                code,
                MODEM_NAMED_CODES.get(code, "(high volume)"),
                f"{med_o:,.0f}",
                "-" if med_l != med_l else f"{med_l:,.0f}",
                note if note else (f"{ratio:.2f}x *" if flat else f"{ratio:.2f}x"),
            )
            pdf.setFillColor(INK if ratio == ratio and (ratio < 0.8 or ratio > 1.25) else MUTED)
            pdf.setFont("Helvetica-Bold" if ratio == ratio and (ratio < 0.8 or ratio > 1.25) else "Helvetica", 7.2)
            cx = x
            for cell, w in zip(cells, widths):
                pdf.drawString(cx, row_y, cell)
                cx += w
            row_y -= 10
            if row_y < 70:
                return
    pdf.setFillColor(MUTED)
    pdf.setFont("Helvetica-Oblique", 7.0)
    wrap_text(
        pdf,
        "* marks a code whose per-second rate barely varies. This summary cannot tell a periodic heartbeat "
        "from steady traffic -- only raw-record timing can -- so treat a starred ratio as unverified. "
        "A ratio far from 1.00 means the modem's activity on that code changed in the seconds holding late "
        "frames. It says when, not why: a fall in uplink scheduling records is consistent with the radio "
        "withholding grants, but these counts cannot distinguish that from the modem logging less for "
        "another reason.",
        x,
        row_y - 8,
        width,
        7.0,
        8.6,
        font="Helvetica-Oblique",
    )


def draw_time_series(
    pdf: canvas.Canvas,
    logs: Sequence[LogData],
    loss_events: Sequence[Event],
    freeze_events: Sequence[Event],
    x: float,
    y: float,
    width: float,
    height: float,
) -> None:
    all_series = [(log, series(log)) for log in logs]
    latency_values = [latency for _, samples in all_series for _, latency in samples]
    duration = max(elapsed for _, samples in all_series for elapsed, _ in samples)
    y_max = max(1.0, percentile(latency_values, 99) * 1.2)

    pdf.setFillColor(INK)
    pdf.setFont("Helvetica-Bold", 11)
    pdf.drawString(x, y + height + 17, "Latency over time")
    pdf.setFont("Helvetica", 7.5)
    pdf.setFillColor(MUTED)
    pdf.drawRightString(x + width, y + height + 17, "milliseconds")

    for tick in range(5):
        tick_y = y + height * tick / 4
        pdf.setStrokeColor(GRID)
        pdf.line(x, tick_y, x + width, tick_y)
        pdf.setFillColor(MUTED)
        pdf.setFont("Helvetica", 7)
        pdf.drawRightString(x - 7, tick_y - 2, f"{y_max * tick / 4:.0f}")

    colors = {"publisher": CYAN, "subscriber": BLUE}
    for log, samples in all_series:
        stride = max(1, math.ceil(len(samples) / 1800))
        path = pdf.beginPath()
        for index, (elapsed, latency) in enumerate(samples[::stride]):
            point_x = x if duration <= 0 else x + width * elapsed / duration
            point_y = y + height * min(latency, y_max) / y_max
            (path.moveTo if index == 0 else path.lineTo)(point_x, point_y)
        pdf.setStrokeColor(colors[log.kind])
        pdf.setLineWidth(1.05)
        pdf.drawPath(path, stroke=1, fill=0)

    for event, color, offset in [
        *((event, RED, -1.0) for event in loss_events),
        *((event, ORANGE, 1.0) for event in freeze_events),
    ]:
        event_x = (
            x
            if duration <= 0
            else x + width * min(event.elapsed_ms, duration) / duration + offset
        )
        event_x = max(x, min(x + width, event_x))
        pdf.setStrokeColor(color)
        pdf.setLineWidth(0.55)
        pdf.setDash(2, 2)
        pdf.line(event_x, y, event_x, y + height)
    pdf.setDash()

    legend_x = x + 8
    for label, color in [
        *((log.label, colors[log.kind]) for log in logs),
        ("Frame loss", RED),
        ("Freeze", ORANGE),
    ]:
        pdf.setStrokeColor(color)
        pdf.setLineWidth(2)
        pdf.line(legend_x, y + height - 12, legend_x + 14, y + height - 12)
        pdf.setFillColor(MUTED)
        pdf.setFont("Helvetica", 7)
        pdf.drawString(legend_x + 18, y + height - 15, label)
        legend_x += 73

    pdf.setFillColor(MUTED)
    pdf.setFont("Helvetica", 7)
    for tick in range(5):
        tick_x = x + width * tick / 4
        pdf.drawCentredString(tick_x, y - 13, f"{duration * tick / 4000:.1f}s")
    pdf.setStrokeColor(INK)
    pdf.rect(x, y, width, height, fill=0, stroke=1)


def paired_frame_rows(
    publisher: LogData, subscriber: LogData
) -> list[tuple[dict[str, str], dict[str, str]]]:
    publisher_by_frame_id = {}
    for row in publisher.rows:
        frame_id = number(row.get("frame_id"))
        if frame_id is not None:
            publisher_by_frame_id[round(frame_id)] = row

    pairs = []
    for row in subscriber.rows:
        frame_id = number(row.get("frame_id"))
        if frame_id is None:
            continue
        publisher_row = publisher_by_frame_id.get(round(frame_id))
        if publisher_row is None:
            continue
        pairs.append((publisher_row, row))
    return pairs


def paired_transport_latencies(publisher: LogData, subscriber: LogData) -> list[float]:
    latencies = []
    for publisher_row, subscriber_row in paired_frame_rows(publisher, subscriber):
        packetize_timestamp_us = number(publisher_row.get("webrtc_packetize_timestamp_us"))
        receive_timestamp_us = number(subscriber_row.get("webrtc_receive_timestamp_us"))
        if packetize_timestamp_us is None or receive_timestamp_us is None:
            continue
        latency_us = receive_timestamp_us - packetize_timestamp_us
        if latency_us >= 0:
            latencies.append(latency_us / 1_000.0)
    return latencies


def latency_rows(logs: Sequence[LogData]) -> list[tuple[str, list[float]]]:
    metrics = []
    publisher = next((log for log in logs if log.kind == "publisher"), None)
    subscriber = next((log for log in logs if log.kind == "subscriber"), None)

    if publisher is not None:
        columns = (
            ("[Publisher] exposure to buffer", "capture_to_buffer_ms"),
            ("[Publisher] encode", "encode_ms"),
            ("[Publisher] exposure to packetize", "capture_to_packetize_ms"),
        )
        metrics.extend((label, values(publisher.rows, column)) for label, column in columns)

    if publisher is not None and subscriber is not None:
        metrics.append(
            ("[Transport] publish to receive", paired_transport_latencies(publisher, subscriber))
        )

    if subscriber is not None:
        if "receive_and_assembly_ms" in subscriber.rows[0]:
            columns = (
                ("[Subscriber] receive and assembly", "receive_and_assembly_ms"),
                ("[Subscriber] decode", "decode_ms"),
                ("[Subscriber] render", "render_ms"),
                ("End-to-end latency", "e2e_to_gpu_complete_ms"),
            )
        elif "e2e_to_gpu_complete_ms" in subscriber.rows[0]:
            columns = (
                ("[Subscriber] exposure to receive", "exposure_to_receive_ms"),
                ("[Subscriber] receive to decode", "receive_to_decode_ms"),
                ("[Subscriber] receive to GPU complete", "receive_to_gpu_complete_ms"),
                ("End-to-end latency", "e2e_to_gpu_complete_ms"),
            )
        else:
            columns = (
                ("[Subscriber] exposure to receive", "exposure_to_receive_ms"),
                ("[Subscriber] receive to decode", "receive_to_decode_ms"),
                ("[Subscriber] receive to paint", "receive_to_paint_ms"),
                ("End-to-end latency", "e2e_latency_ms"),
            )
        metrics.extend((label, values(subscriber.rows, column)) for label, column in columns)
    return [(label, samples) for label, samples in metrics if samples]


def pipeline_stage_means(logs: Sequence[LogData]) -> list[tuple[str, float, object]]:
    publisher = next((log for log in logs if log.kind == "publisher"), None)
    subscriber = next((log for log in logs if log.kind == "subscriber"), None)
    publisher_rows = publisher.rows if publisher is not None else []
    subscriber_rows = subscriber.rows if subscriber is not None else []

    if publisher is not None and subscriber is not None:
        pairs = paired_frame_rows(publisher, subscriber)
        publisher_rows = [publisher_row for publisher_row, _ in pairs]
        subscriber_rows = [subscriber_row for _, subscriber_row in pairs]

    stage_samples = []
    if publisher is not None:
        stage_samples.extend(
            (
                ("[P] exposure to buffer", values(publisher_rows, "capture_to_buffer_ms"), 0),
                (
                    "[P] encode and packetize",
                    summed_values(
                        publisher_rows,
                        (
                            "buffer_to_encoder_ms",
                            "encode_ms",
                            "encoder_to_packetize_ms",
                        ),
                    ),
                    1,
                ),
            )
        )
    if publisher is not None and subscriber is not None:
        stage_samples.append(
            ("[T] publish to receive", paired_transport_latencies(publisher, subscriber), 4)
        )
    if subscriber is not None:
        if "receive_and_assembly_ms" in subscriber.rows[0]:
            subscriber_stages = (
                ("[S] receive and assembly", "receive_and_assembly_ms", 5),
                ("[S] decode", "decode_ms", 6),
                ("[S] render", "render_ms", 8),
            )
            stage_samples.extend(
                (label, values(subscriber_rows, column), color_index)
                for label, column, color_index in subscriber_stages
            )
        elif "e2e_to_gpu_complete_ms" in subscriber.rows[0]:
            stage_samples.extend(
                (
                    ("[S] receive to decode", values(subscriber_rows, "receive_to_decode_ms"), 5),
                    (
                        "[S] decode to GPU render",
                        summed_values(
                            subscriber_rows,
                            (
                                "decode_to_sink_ms",
                                "sink_to_select_ms",
                                "select_to_prepare_ms",
                                "prepare_to_draw_encoded_ms",
                            ),
                        ),
                        6,
                    ),
                    (
                        "[S] draw to GPU complete",
                        values(subscriber_rows, "draw_encoded_to_gpu_complete_ms"),
                        10,
                    ),
                )
            )
        else:
            subscriber_stages = (
                ("[S] receive to decode", "receive_to_decode_ms", 5),
                ("[S] decode to sink", "decode_to_sink_ms", 6),
                ("[S] sink to prepare", "sink_to_prepare_ms", 8),
                ("[S] prepare to paint", "prepare_to_paint_ms", 10),
            )
            stage_samples.extend(
                (label, values(subscriber_rows, column), color_index)
                for label, column, color_index in subscriber_stages
            )

    return [
        (label, statistics.fmean(samples), PIPELINE_COLORS[color_index])
        for label, samples, color_index in stage_samples
        if samples
    ]


# Title sits at y+140 and the legend runs below the bar at y+96; 150 pt covers the block.
TIMELINE_HEIGHT = 150.0


def draw_latency_table(
    pdf: canvas.Canvas, logs: Sequence[LogData], x: float, y: float, width: float
) -> None:
    rows = latency_rows(logs)
    pdf.setFillColor(INK)
    pdf.setFont("Helvetica-Bold", 10.5)
    pdf.drawString(x, y + 18, "Latency summary")
    headers = (("Stage", 0), ("Mean", width - 135), ("P50", width - 88), ("P95", width - 41))
    pdf.setFillColor(NAVY)
    pdf.rect(x, y - 4, width, 21, fill=1, stroke=0)
    pdf.setFillColor(white)
    pdf.setFont("Helvetica-Bold", 7)
    for label, offset in headers:
        pdf.drawString(x + offset + 7, y + 4, label)
    row_y = y - 20
    for index, (label, samples) in enumerate(rows):
        pdf.setFillColor(PANEL if index % 2 == 0 else white)
        pdf.rect(x, row_y, width, 15, fill=1, stroke=0)
        pdf.setFillColor(INK)
        pdf.setFont("Helvetica", 7.3)
        pdf.drawString(x + 7, row_y + 4.5, label)
        for offset, value in zip(
            (width - 128, width - 81, width - 34),
            (statistics.fmean(samples), percentile(samples, 50), percentile(samples, 95)),
        ):
            pdf.drawRightString(x + offset, row_y + 4.5, f"{value:.1f}")
        row_y -= 15


def draw_pipeline_timeline(
    pdf: canvas.Canvas,
    logs: Sequence[LogData],
    x: float,
    y: float,
    width: float,
) -> None:
    stages = pipeline_stage_means(logs)
    total_ms = sum(mean_ms for _, mean_ms, _ in stages)
    pdf.setFillColor(INK)
    pdf.setFont("Helvetica-Bold", 10.5)
    pdf.drawString(x, y + 140, "Mean pipeline timeline")
    if not stages or total_ms <= 0:
        pdf.setFillColor(MUTED)
        pdf.setFont("Helvetica", 8)
        pdf.drawString(x, y + 116, "No complete pipeline-stage samples")
        return

    pdf.setFillColor(MUTED)
    pdf.setFont("Helvetica", 7.2)
    timeline_summary = f"Segment width is proportional to mean duration | total {total_ms:.1f} ms"
    pdf.drawString(x, y + 124, timeline_summary)

    bar_y = y + 96
    bar_height = 19
    cursor_x = x
    for index, (_, mean_ms, color) in enumerate(stages):
        segment_width = width * mean_ms / total_ms
        pdf.setFillColor(color)
        pdf.rect(cursor_x, bar_y, segment_width, bar_height, fill=1, stroke=0)
        if segment_width >= 12:
            pdf.setFillColor(white)
            pdf.setFont("Helvetica-Bold", 6.2)
            pdf.drawCentredString(
                cursor_x + segment_width / 2,
                bar_y + 6.2,
                str(index + 1),
            )
        cursor_x += segment_width
    pdf.setStrokeColor(INK)
    pdf.rect(x, bar_y, width, bar_height, fill=0, stroke=1)

    rows_per_column = 6
    column_width = width / 2
    legend_y = y + 78
    for index, (label, mean_ms, color) in enumerate(stages):
        column = index // rows_per_column
        row = index % rows_per_column
        item_x = x + column * column_width
        item_y = legend_y - row * 12
        pdf.setFillColor(color)
        pdf.rect(item_x, item_y - 1, 7, 7, fill=1, stroke=0)
        pdf.setFillColor(INK)
        pdf.setFont("Helvetica", 6.4)
        pdf.drawString(item_x + 11, item_y, f"{index + 1}. {label}  {mean_ms:.1f} ms")


def generate_report(
    publisher: LogData | None,
    subscriber: LogData | None,
    output: Path,
    title: str,
    counters: DecodeCounters | None = None,
    publisher_stats_path: Path | None = None,
    modem_rates: Sequence[ModemRates] = (),
) -> None:
    logs = [log for log in (publisher, subscriber) if log is not None]
    page_count = 3 if (modem_rates and subscriber is not None) else 2
    assert logs
    primary = subscriber or publisher
    assert primary is not None
    primary_latencies = values(primary.rows, primary.latency_column)
    duration_ms = max(values(primary.rows, "elapsed_ms"), default=0.0)

    event_log = subscriber or publisher
    assert event_log is not None
    loss_events = gap_events(event_log)
    freeze_events = subscriber_freeze_events(event_log)
    accounting = None
    if publisher is not None and subscriber is not None:
        loss_events = paired_loss_events(publisher, subscriber)
        accounting = paired_frame_accounting(publisher, subscriber)
    losses = sum(event.count for event in loss_events)

    sources = " + ".join(f"{log.label}: {log.path.name}" for log in logs)
    subtitle = sources
    # A run whose delivered resolution moved is flagged in the header rather than left
    # for a reader to notice -- being invisible is the failure this exists to prevent.
    subtitle_lines = [sources]
    res_track = resolution_track(subscriber) if subscriber is not None else None
    if res_track is not None and res_track.total:
        subtitle_lines.append(f"delivered resolution: {res_track.summary()}")
    # B4: encoder-side vs decoder-side, joined on frame ID. Only meaningful once the
    # publisher emits encoded_frame_width -- older runs simply have no encoder column.
    pairing = (
        pair_resolutions(publisher, subscriber)
        if publisher is not None and subscriber is not None
        else None
    )
    if pairing is not None and pairing.available and pairing.disagreements:
        subtitle_lines.append(f"encoder vs decoder: {pairing.summary()}")
    subtitle = subtitle_lines
    output.parent.mkdir(parents=True, exist_ok=True)
    pdf = canvas.Canvas(str(output), pagesize=landscape(letter))
    pdf.setTitle(title)
    pdf.setAuthor("LiveKit local_video")
    draw_header(pdf, title, subtitle)

    cards = (
        ("Rendered frames" if subscriber else "Packetized frames", f"{len(primary.rows):,}"),
        ("Duration", f"{duration_ms / 1000:.1f} s"),
        ("Mean latency", f"{statistics.fmean(primary_latencies):.1f} ms"),
        ("P50 latency", f"{percentile(primary_latencies, 50):.1f} ms"),
        ("P95 latency", f"{percentile(primary_latencies, 95):.1f} ms"),
        ("Frames not shown" if accounting is not None else "Frame losses", f"{losses:,}"),
    )
    # The resolution card sits on its own row beneath the latency cards rather than
    # extending the top row. As a seventh card in one row it ran off the right edge --
    # and it is the card added so a reader could not miss a collapse, so it was the one
    # thing invisible on the page.
    resolution_card = (
        (
            "Resolution CHANGED" if res_track.changed else "Resolution",
            res_track.modal or "-",
        )
        if res_track is not None and res_track.total
        else None
    )
    # Cards are sized from their count, not from a constant. A fixed 112 pt card with a
    # 11 pt gap ran to x=888 once a seventh card was added, and landscape letter is 792 --
    # so the resolution card, the one added precisely so a reader could not miss a
    # collapse, was the one printed off the right edge.
    page_width, page_height = landscape(letter)
    left, right_margin, gap = 38.0, 38.0, 11.0
    usable = page_width - left - right_margin
    card_width = (usable - gap * (len(cards) - 1)) / len(cards)
    top_row_y = 470.0 if resolution_card else 461.0
    for index, (label, value) in enumerate(cards):
        draw_card(pdf, left + index * (card_width + gap), top_row_y, card_width, label, value)

    # Second row, aligned under the three latency cards it qualifies: a resolution that
    # moved makes every latency figure above it a figure about a smaller picture.
    if resolution_card:
        first_latency = 2
        span_x = left + first_latency * (card_width + gap)
        span_w = 3 * card_width + 2 * gap
        draw_card(pdf, span_x, top_row_y - 60, span_w, *resolution_card)

    series_height = 175.0 if resolution_card else 205.0
    draw_time_series(pdf, logs, loss_events, freeze_events, 50, 206, 692, series_height)

    # The band under the chart was empty. It now carries the frame accounting, which is
    # the first thing an operator asks about a degraded feed and which no single counter
    # answers: a frame missing from the CSV may never have arrived, may have failed to
    # decode, or may have arrived and decoded and never been drawn.
    if accounting is not None:
        captures = [
            int(value)
            for value in (number(row.get("capture_timestamp_us")) for row in subscriber.rows)
            if value is not None and value > 0
        ]
        pub_stats = (
            read_publisher_stats(
                publisher_stats_path, (min(captures), max(captures)) if captures else None
            )
            if publisher_stats_path is not None
            else None
        )
        draw_paired_accounting(pdf, left, 92, usable, accounting, pub_stats)
    elif counters is not None and subscriber is not None:
        frame_ids = [
            int(value)
            for value in (number(row.get("frame_id")) for row in subscriber.rows)
            if value is not None
        ]
        if frame_ids:
            emitted = max(frame_ids) - min(frame_ids) + 1
            captures = [
                int(value)
                for value in (
                    number(row.get("capture_timestamp_us")) for row in subscriber.rows
                )
                if value is not None and value > 0
            ]
            pub_stats = (
                read_publisher_stats(
                    publisher_stats_path,
                    (min(captures), max(captures)) if captures else None,
                )
                if publisher_stats_path is not None
                else None
            )
            draw_frame_accounting(
                pdf,
                left,
                92 if pub_stats is not None else 74,
                usable,
                emitted,
                counters,
                len(subscriber.rows),
                pub_stats,
            )

    footer_left = left
    footer_right = page_width - right_margin
    freeze_note = (
        "Freeze markers use subscriber WebRTC freeze counters."
        if subscriber and values(subscriber.rows, "freeze_count")
        else "Freeze markers are inter-frame gaps over 3x the median interval."
    )

    def draw_footer(page_label: str, note: str) -> None:
        pdf.setStrokeColor(GRID)
        pdf.line(footer_left, 28, footer_right, 28)
        pdf.setFillColor(MUTED)
        pdf.setFont("Helvetica", 6.8)
        pdf.drawString(footer_left, 17, note)
        pdf.drawRightString(footer_right, 17, page_label)

    draw_footer(
        f"Page 1 of {page_count} - overview",
        (
            "Red marks a frame the publisher sent that never reached the subscriber's screen "
            "(any cause; the funnel splits network, decode and render). "
            if publisher is not None and subscriber is not None
            else "Frame-loss markers reflect frame-ID gaps. "
        )
        + freeze_note,
    )

    # Page 2 carries the per-stage detail. It was previously squeezed beside the latency
    # table on one page, which left the pipeline timeline 374 pt wide for up to twelve
    # stages; here each gets the full width.
    pdf.showPage()
    draw_header(pdf, title, subtitle)
    # Both blocks were at fixed y, which left a 220 pt band of white under the header on a
    # subscriber-only report and, worse, collided on a paired one: the table grows downward
    # at 15 pt a row, so a twelve-stage paired run reached y=85 and drew through the
    # timeline sitting at y=60. Anchor the table under the header and derive the timeline's
    # position from the table's actual height instead.
    table_top = 500.0
    table_bottom = table_top - 20.0 - 15.0 * len(latency_rows(logs))
    timeline_y = max(60.0, table_bottom - 45.0 - TIMELINE_HEIGHT)
    draw_latency_table(pdf, logs, left, table_top, usable)
    draw_pipeline_timeline(pdf, logs, left, timeline_y, usable)
    draw_footer(
        f"Page 2 of {page_count} - stage detail",
        "Latency percentiles per log, and mean time in each pipeline stage.",
    )
    if modem_rates and subscriber is not None:
        pdf.showPage()
        draw_header(pdf, title, subtitle)
        draw_modem_page(pdf, modem_rates, subscriber, left, usable)
        draw_footer(
            f"Page 3 of {page_count} - modem activity",
            "Per-second DIAG record counts, not grant sizes. Decoding the records themselves needs QCAT.",
        )
    pdf.save()


def main() -> int:
    args = parse_args()
    try:
        publisher = read_log(args.publisher, "publisher") if args.publisher else None
        subscriber = read_log(args.subscriber, "subscriber") if args.subscriber else None
        counters = (
            read_decode_counters(args.subscriber_log) if args.subscriber_log else None
        )
        modem_rates = [
            m
            for m in (
                read_modem_rates(args.modem_rates_a, "Host A") if args.modem_rates_a else None,
                read_modem_rates(args.modem_rates_b, "Host B") if args.modem_rates_b else None,
            )
            if m is not None
        ]
        for path, loaded in (
            (args.modem_rates_a, any(m.label == "Host A" for m in modem_rates)),
            (args.modem_rates_b, any(m.label == "Host B" for m in modem_rates)),
        ):
            if path is not None and not loaded:
                print(
                    f"warning: {path} has no probe_start in its header, or no rows; "
                    "modem page will omit it rather than guess where second 0 sits",
                    file=sys.stderr,
                )
        generate_report(
            publisher,
            subscriber,
            args.output,
            args.title,
            counters,
            args.publisher_stats,
            modem_rates,
        )
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(f"Wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
