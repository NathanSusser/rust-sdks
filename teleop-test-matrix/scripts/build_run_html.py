#!/usr/bin/env python3
"""Collect every rerun cell into one HTML: sent quality against received quality.

Per cell it joins three sources:
  <room>.jsonl              publisher snapshots -- encoder QP, grant, delivered bitrate,
                            the resolution ladder
  <room>.pub.csv            per-frame publisher stage timings
  hostb/<room>/subscriber.csv   Host B -- receive_qp, decode_ms, delivered resolution, loss

WHY QP IS COMPARED AS A DISTRIBUTION AND NOT A MEAN
---------------------------------------------------
Both sides report an interval mean over a 1 Hz poll, pinned deliberately on both
hosts: an interval statistic compared against one computed over a different window
compares the windows, which produced two false alarms in this programme in one day.
Even matched, a single mean hides the shape, so p5/p50/p95 are reported for each
side and the comparison is stated as a delta at the median with the spread beside
it.

The two outcomes were registered before the data existed. Close agreement means the
bitstream survived transit -- never verified here before. Divergence means something
altered it. Neither is the expected one.
"""
import csv
import glob
import html
import json
import os
import statistics
import sys

PIX_PER_S = 1600 * 1300 * 30


def pct(vals, p):
    if not vals:
        return float("nan")
    s = sorted(vals)
    return s[min(len(s) - 1, max(0, int(round(p / 100 * (len(s) - 1)))))]


def publisher_side(jsonl):
    rows = []
    for line in open(jsonl):
        try:
            snap = json.loads(line)
        except json.JSONDecodeError:
            continue
        vo = snap.get("video_out") or {}
        if vo.get("frames_encoded"):
            rows.append((snap["t_unix_us"] / 1e6, vo))
    if len(rows) < 3:
        return None
    qp, sent, grants = [], [], []
    for (t1, a), (t2, b) in zip(rows, rows[1:]):
        dfr = b["frames_encoded"] - a["frames_encoded"]
        dqp = b["qp_sum"] - a["qp_sum"]
        if dfr > 0:
            qp.append(dqp / dfr)
        dby = b["bytes_sent"] - a["bytes_sent"]
        if dby > 0 and t2 > t1:
            sent.append(dby * 8 / (t2 - t1))
    for _, vo in rows:
        if vo.get("target_bitrate_bps"):
            grants.append(vo["target_bitrate_bps"])
    t0 = rows[0][0]
    ladder, prev = [], None
    for t, vo in rows:
        r = (vo.get("frame_width"), vo.get("frame_height"))
        if r != prev:
            ladder.append((t - t0, r))
            prev = r
    last = rows[-1][1]
    full = sum(1 for _, vo in rows
               if (vo.get("frame_width"), vo.get("frame_height")) == (1600, 1300))
    return {
        "qp": qp, "sent": sent, "grants": grants, "ladder": ladder,
        "polls": len(rows), "full_frac": full / len(rows),
        "encoder": last.get("encoder_implementation", ""),
        "nack": last.get("nack_count", 0),
        "bw_s": last.get("quality_limitation_bandwidth_s", 0.0),
        "frames": last.get("frames_encoded", 0),
        "fps": last.get("frames_per_second", 0.0),
    }


def subscriber_side(path):
    if not os.path.exists(path):
        return None
    rows = list(csv.DictReader(open(path)))
    if not rows:
        return None

    def col(name):
        out = []
        for r in rows:
            v = r.get(name)
            if v not in (None, "", "nan", "NaN"):
                try:
                    out.append(float(v))
                except ValueError:
                    pass
        return out

    res = {}
    for r in rows:
        try:
            k = (int(float(r["frame_width"])), int(float(r["frame_height"])))
        except (KeyError, ValueError, TypeError):
            continue
        res[k] = res.get(k, 0) + 1
    qp = col("receive_qp")
    return {
        "rows": len(rows), "qp": qp, "decode_ms": col("decode_ms"),
        "recv_mbps": col("receive_bitrate_mbps"),
        "res": sorted(res.items(), key=lambda kv: -kv[1]),
        "decoder": next((r.get("decoder_implementation", "") for r in rows
                         if r.get("decoder_implementation")), ""),
    }


CSS = """
:root{--ground:#F6F7F9;--panel:#FFF;--panel-2:#EEF0F4;--ink:#181D25;--ink-2:#3D4653;
--ink-3:#6D7683;--rule:#D3D8E0;--rule-2:#E4E8ED;--accent:#B26A0B;--good:#3C6540;
--good-soft:#E6EFE6;--bad:#8E2C2C;--bad-soft:#F6E7E7;--warn:#8A6A12;--warn-soft:#FAF2DE;
--sent:#2A6668;--recv:#B26A0B}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--ground:#10141A;
--panel:#171C24;--panel-2:#1E242E;--ink:#E7EBF1;--ink-2:#B4BDCA;--ink-3:#818C9B;
--rule:#2C343F;--rule-2:#232A34;--accent:#E0A445;--good:#8DBE92;--good-soft:#182219;
--bad:#D98686;--bad-soft:#2A1919;--warn:#D8B65C;--warn-soft:#272014;--sent:#5FA8AA;
--recv:#E0A445}}
:root[data-theme="dark"]{--ground:#10141A;--panel:#171C24;--panel-2:#1E242E;--ink:#E7EBF1;
--ink-2:#B4BDCA;--ink-3:#818C9B;--rule:#2C343F;--rule-2:#232A34;--accent:#E0A445;
--good:#8DBE92;--good-soft:#182219;--bad:#D98686;--bad-soft:#2A1919;--warn:#D8B65C;
--warn-soft:#272014;--sent:#5FA8AA;--recv:#E0A445}
*{box-sizing:border-box}
body{background:var(--ground);color:var(--ink);font-family:"IBM Plex Serif",Georgia,serif;
font-size:15.5px;line-height:1.58;-webkit-font-smoothing:antialiased}
.wrap{max-width:1100px;margin:0 auto;padding:0 24px 70px}
header.mast{border-bottom:2px solid var(--ink);padding:40px 0 16px}
.eyebrow{font-family:"IBM Plex Mono",monospace;font-size:11px;letter-spacing:.16em;
text-transform:uppercase;color:var(--accent);font-weight:600;margin-bottom:11px}
h1{font-family:"IBM Plex Sans",system-ui,sans-serif;font-weight:700;
font-size:clamp(1.8rem,4.3vw,2.6rem);line-height:1.05;letter-spacing:-.02em;margin:0 0 12px}
.standfirst{font-size:1.06rem;color:var(--ink-2);max-width:64ch;margin:0}
h2{font-family:"IBM Plex Sans",system-ui,sans-serif;font-weight:600;font-size:1.45rem;
margin:36px 0 10px;letter-spacing:-.01em}
h3{font-family:"IBM Plex Sans",system-ui,sans-serif;font-weight:600;font-size:1.02rem;margin:0}
p{margin:0 0 12px;max-width:70ch}
code,.mono{font-family:"IBM Plex Mono",monospace;font-size:.87em}
.tscroll{overflow-x:auto;margin:14px 0 20px;border:1px solid var(--rule);background:var(--panel)}
table{border-collapse:collapse;width:100%;font-size:13px;font-family:"IBM Plex Sans",system-ui,sans-serif}
th{text-align:left;font-weight:600;font-size:10.5px;letter-spacing:.06em;text-transform:uppercase;
color:var(--ink-3);padding:9px 12px;border-bottom:1px solid var(--rule);white-space:nowrap}
td{padding:7px 12px;border-bottom:1px solid var(--rule-2);color:var(--ink-2);vertical-align:top}
tr:last-child td{border-bottom:none}
td.num,th.num{font-family:"IBM Plex Mono",monospace;font-variant-numeric:tabular-nums;white-space:nowrap}
td.k{color:var(--ink);font-weight:500}
.pill{font-family:"IBM Plex Mono",monospace;font-size:10px;font-weight:600;letter-spacing:.05em;
text-transform:uppercase;padding:2px 7px;border-radius:2px;display:inline-block;white-space:nowrap}
.p-good{background:var(--good-soft);color:var(--good)}
.p-bad{background:var(--bad-soft);color:var(--bad)}
.p-warn{background:var(--warn-soft);color:var(--warn)}
.cell{border:1px solid var(--rule);background:var(--panel);margin:18px 0;
box-shadow:0 1px 2px rgba(24,29,37,.05)}
.cellhead{display:flex;align-items:baseline;gap:14px;flex-wrap:wrap;
padding:14px 18px;border-bottom:1px solid var(--rule);background:var(--panel-2)}
.cellhead .cap{font-family:"IBM Plex Mono",monospace;font-size:1.05rem;font-weight:600;color:var(--accent)}
.cellhead .meta{font-family:"IBM Plex Mono",monospace;font-size:11px;color:var(--ink-3);margin-left:auto}
.cellbody{padding:16px 18px}
.qprow{display:flex;gap:22px;flex-wrap:wrap;align-items:center;margin:0 0 12px}
.qpbox{flex:1 1 210px;min-width:210px}
.qplabel{font-family:"IBM Plex Mono",monospace;font-size:10px;letter-spacing:.11em;
text-transform:uppercase;color:var(--ink-3);margin-bottom:4px}
.qpval{font-family:"IBM Plex Mono",monospace;font-size:1.6rem;font-weight:600;
font-variant-numeric:tabular-nums;line-height:1}
.qpspread{font-family:"IBM Plex Mono",monospace;font-size:11px;color:var(--ink-3);margin-top:3px}
.s-sent{color:var(--sent)} .s-recv{color:var(--recv)}
.ladder{font-family:"IBM Plex Mono",monospace;font-size:11.5px;color:var(--ink-2)}
.callout{background:var(--panel-2);border-left:3px solid var(--ink-3);padding:14px 18px;margin:16px 0}
.callout.bad{border-left-color:var(--bad);background:var(--bad-soft)}
.callout.good{border-left-color:var(--good);background:var(--good-soft)}
.callout p:last-child{margin-bottom:0}
.ctag{font-family:"IBM Plex Mono",monospace;font-size:10px;font-weight:600;letter-spacing:.12em;
text-transform:uppercase;color:var(--ink-3);display:block;margin-bottom:5px}
.callout.bad .ctag{color:var(--bad)} .callout.good .ctag{color:var(--good)}
footer{margin-top:44px;padding-top:16px;border-top:2px solid var(--ink);
font-family:"IBM Plex Mono",monospace;font-size:11px;color:var(--ink-3);
display:flex;justify-content:space-between;gap:14px;flex-wrap:wrap}
"""


def fmt(v, nd=1):
    return "—" if v != v else f"{v:.{nd}f}"


def main():
    d = sys.argv[1] if len(sys.argv) > 1 else "results/08-e2-rerun"
    out = sys.argv[2] if len(sys.argv) > 2 else "e2-rerun.html"

    cells = []
    for jf in sorted(glob.glob(os.path.join(d, "*.jsonl"))):
        room = os.path.basename(jf)[: -len(".jsonl")]
        pub = publisher_side(jf)
        if not pub:
            continue
        sub = subscriber_side(os.path.join(d, "hostb", room, "subscriber.csv"))
        cap = int(room.split("-")[1].rstrip("k")) * 1000
        cells.append((room, cap, pub, sub))
    cells.sort(key=lambda c: (c[1], c[0]))

    paired = [c for c in cells if c[3] and c[3]["qp"]]
    parts = [f"<title>E2 Rerun — Sent vs Received</title>",
             '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?'
             'family=IBM+Plex+Sans:wght@400;500;600;700&family=IBM+Plex+Serif:wght@400;600'
             '&family=IBM+Plex+Mono:wght@400;500;600&display=swap">',
             f"<style>{CSS}</style>", '<div class="wrap">',
             '<header class="mast">',
             '<div class="eyebrow">E2 rerun &middot; sent quality against received quality</div>',
             "<h1>E2 Rerun — Sent vs Received</h1>",
             '<p class="standfirst">Every cell with both halves: what the encoder chose, '
             'what the decoder read out of the bitstream that arrived, and the difference. '
             f'{len(cells)} cells, {len(paired)} with a receive-side quantiser.</p>',
             "</header>"]

    # Summary of the comparison across every paired cell.
    if paired:
        parts.append("<h2>The comparison</h2>")
        parts.append('<div class="tscroll"><table><thead><tr>'
                     '<th>Cell</th><th class="num">Cap</th>'
                     '<th class="num">QP sent p50</th><th class="num">QP recv p50</th>'
                     '<th class="num">&Delta;</th><th class="num">Sent Mbps</th>'
                     '<th class="num">Recv Mbps</th><th class="num">Full size</th>'
                     '<th>Verdict</th></tr></thead><tbody>')
        for room, cap, pub, sub in paired:
            qs, qr = pct(pub["qp"], 50), pct(sub["qp"], 50)
            delta = qr - qs
            v = ('<span class="pill p-good">bitstream intact</span>' if abs(delta) <= 1.5
                 else '<span class="pill p-bad">diverges</span>')
            parts.append(
                f'<tr><td class="k mono">{html.escape(room)}</td>'
                f'<td class="num">{cap/1e6:.1f}</td>'
                f'<td class="num s-sent">{fmt(qs)}</td>'
                f'<td class="num s-recv">{fmt(qr)}</td>'
                f'<td class="num">{delta:+.1f}</td>'
                f'<td class="num">{statistics.mean(pub["sent"])/1e6:.3f}</td>'
                f'<td class="num">{fmt(statistics.mean(sub["recv_mbps"])/1 if sub["recv_mbps"] else float("nan"),3)}</td>'
                f'<td class="num">{100*pub["full_frac"]:.0f}%</td>'
                f'<td>{v}</td></tr>')
        parts.append("</tbody></table></div>")
        parts.append('<div class="callout"><span class="ctag">How to read the delta</span>'
                     "<p>Both sides are 1 Hz interval means, with the poll cadence pinned to "
                     "1 Hz on both hosts so the two averages describe the same window. "
                     "Agreement within about 1.5 QP means the bitstream the decoder read is "
                     "the bitstream the encoder wrote — which this programme had never "
                     "verified. A larger gap means something altered it in transit.</p></div>")

    parts.append("<h2>Every cell</h2>")
    for room, cap, pub, sub in cells:
        qs = pct(pub["qp"], 50)
        parts.append('<div class="cell"><div class="cellhead">'
                     f'<span class="cap">{cap/1e6:g} Mbps</span>'
                     f'<h3>{html.escape(room)}</h3>'
                     f'<span class="meta">{html.escape(pub["encoder"])}'
                     f' &middot; {pub["frames"]} frames &middot; {pub["polls"]} polls</span>'
                     "</div><div class=\"cellbody\">")
        parts.append('<div class="qprow">')
        parts.append('<div class="qpbox"><div class="qplabel">QP sent (encoder)</div>'
                     f'<div class="qpval s-sent">{fmt(qs)}</div>'
                     f'<div class="qpspread">p5 {fmt(pct(pub["qp"],5))} &middot; '
                     f'p95 {fmt(pct(pub["qp"],95))}</div></div>')
        if sub and sub["qp"]:
            qr = pct(sub["qp"], 50)
            parts.append('<div class="qpbox"><div class="qplabel">QP received (decoder)</div>'
                         f'<div class="qpval s-recv">{fmt(qr)}</div>'
                         f'<div class="qpspread">p5 {fmt(pct(sub["qp"],5))} &middot; '
                         f'p95 {fmt(pct(sub["qp"],95))}</div></div>')
            parts.append('<div class="qpbox"><div class="qplabel">Difference</div>'
                         f'<div class="qpval">{qr-qs:+.1f}</div>'
                         '<div class="qpspread">received minus sent</div></div>')
        else:
            parts.append('<div class="qpbox"><div class="qplabel">QP received</div>'
                         '<div class="qpval" style="color:var(--ink-3)">—</div>'
                         '<div class="qpspread">no subscriber half</div></div>')
        parts.append("</div>")

        parts.append('<div class="tscroll"><table><tbody>')
        parts.append(f'<tr><td class="k">Delivered bitrate</td><td class="num">'
                     f'{statistics.mean(pub["sent"])/1e6:.3f} Mbps '
                     f'({statistics.mean(pub["sent"])/PIX_PER_S:.4f} bpp)</td></tr>')
        if pub["grants"]:
            g = pub["grants"]
            parts.append(f'<tr><td class="k">Grant vs cap</td><td class="num">'
                         f'{statistics.mean(g)/cap:.2f} mean, min {min(g)/cap:.2f}</td></tr>')
        parts.append(f'<tr><td class="k">Time at 1600&times;1300</td>'
                     f'<td class="num">{100*pub["full_frac"]:.0f}%</td></tr>')
        parts.append('<tr><td class="k">Resolution over the run</td><td class="ladder">'
                     + " &rarr; ".join(f"{w}&times;{h}@{t:.0f}s" for t, (w, h) in pub["ladder"])
                     + "</td></tr>")
        parts.append(f'<tr><td class="k">Bandwidth-limited</td><td class="num">'
                     f'{pub["bw_s"]:.1f} s &middot; NACK {pub["nack"]} &middot; '
                     f'{pub["fps"]:.0f} fps</td></tr>')
        if sub:
            parts.append(f'<tr><td class="k">Host B decode</td><td class="num">'
                         f'p50 {fmt(pct(sub["decode_ms"],50),2)} ms &middot; '
                         f'p95 {fmt(pct(sub["decode_ms"],95),2)} ms &middot; '
                         f'{html.escape(sub["decoder"])} &middot; {sub["rows"]} rows</td></tr>')
            parts.append('<tr><td class="k">Host B resolutions</td><td class="ladder">'
                         + ", ".join(f"{w}&times;{h} {n}" for (w, h), n in sub["res"][:5])
                         + "</td></tr>")
        parts.append("</tbody></table></div></div></div>")

    parts.append('<footer><span>Host A publisher + Host B subscriber &middot; '
                 'robot footage &middot; H.264 NVENC &middot; 1600&times;1300 @ 30 fps</span>'
                 '<span>bitrate pinned to cap &middot; 1 Hz polls both hosts</span></footer>')
    parts.append("</div>")

    with open(out, "w") as fh:
        fh.write("\n".join(parts))
    print(f"wrote {out}: {len(cells)} cells, {len(paired)} paired")


if __name__ == "__main__":
    main()
