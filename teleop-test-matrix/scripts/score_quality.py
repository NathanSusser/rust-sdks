#!/usr/bin/env python3
"""Score decoded I420 samples against the robot reference frames.

E1's scorer. The subscriber writes every Nth decoded frame as raw I420, named by
in-band frame ID. This matches each sample to the reference frame it actually
came from and reports PSNR-Y and SSIM-Y.

WHY MATCH BY CONTENT RATHER THAN BY OFFSET
------------------------------------------
The obvious mapping is `reference_index = (frame_id + offset) % 808`, with one
offset discovered per run. That is wrong here, and quietly so. The source clip is
29.85 fps served in a loop while the publisher captures at 30 fps, so the two
clocks drift: the publisher duplicates or skips a source frame roughly once every
few hundred frames, and every sample after that point is misaligned by one.
A fixed offset would score those against the wrong reference and report a PSNR
several dB low, which reads exactly like a codec result.

Every one of the 808 reference frames is distinct, so identifying a sample by
content is unambiguous. Matching is done on a cheap strided signature first, and
the winner is confirmed by full PSNR.

A sample whose best match is not clearly better than its runner-up is reported as
UNMATCHED rather than scored, because a forced match is indistinguishable from a
correct one in the output.
"""
import argparse
import csv
import glob
import os
import sys

import numpy as np

W, H = 1600, 1300          # the SOURCE geometry; samples may be smaller
Y_SIZE = W * H

# Resampler used whenever a frame has to be scaled to meet the reference. Recorded
# in the output because it is not a free choice: bilinear and Lanczos differ by a
# real amount, and a figure computed with one is not comparable to one computed
# with the other.
RESAMPLER_NAME = "lanczos"


def load_y(path, width=W, height=H, stride_y=None):
    """Read the Y plane of a raw I420 frame at its own geometry.

    Geometry comes from the subscriber's index.csv, never from an assumption: a
    cell whose encoder stepped down writes smaller frames, and reading those at the
    source geometry produces a confidently wrong image rather than an error.
    """
    stride = stride_y or width
    with open(path, "rb") as fh:
        buf = fh.read(stride * height)
    if len(buf) < stride * height:
        raise ValueError(f"{path}: short read, {len(buf)} of {stride * height} bytes")
    plane = np.frombuffer(buf, dtype=np.uint8).reshape(height, stride)
    return plane[:, :width]


def read_index(samples_dir):
    """Per-frame geometry written by the subscriber, keyed by frame id."""
    path = os.path.join(samples_dir, "index.csv")
    if not os.path.exists(path):
        return {}
    out = {}
    with open(path) as fh:
        for row in csv.DictReader(fh):
            try:
                out[int(row["frame_id"])] = (
                    int(row["width"]), int(row["height"]), int(row["stride_y"]))
            except (KeyError, ValueError):
                continue
    return out


def resize_y(y, size):
    """Scale a luma plane to (width, height)."""
    from PIL import Image
    if (y.shape[1], y.shape[0]) == size:
        return y
    return np.asarray(Image.fromarray(y).resize(size, Image.LANCZOS))


def signature(y):
    """A small, cheap fingerprint: every 40th pixel in each axis, mean-centred."""
    s = y[::40, ::40].astype(np.float32)
    return (s - s.mean()).ravel()


def psnr_y(a, b):
    diff = a.astype(np.float32) - b.astype(np.float32)
    mse = float(np.mean(diff * diff))
    if mse == 0.0:
        return float("inf")
    return 10.0 * np.log10((255.0**2) / mse)


def ssim_y(a, b):
    """Global SSIM on the luma plane, 8x8 blocks. Close enough to ffmpeg's SSIM-Y
    for a gate; the authoritative figure stays ffmpeg's."""
    a = a.astype(np.float64)
    b = b.astype(np.float64)
    bh, bw = 8, 8
    h, w = a.shape[0] // bh * bh, a.shape[1] // bw * bw
    av = a[:h, :w].reshape(h // bh, bh, w // bw, bw).transpose(0, 2, 1, 3).reshape(-1, bh * bw)
    bv = b[:h, :w].reshape(h // bh, bh, w // bw, bw).transpose(0, 2, 1, 3).reshape(-1, bh * bw)
    mu_a, mu_b = av.mean(1), bv.mean(1)
    va, vb = av.var(1), bv.var(1)
    cov = ((av - mu_a[:, None]) * (bv - mu_b[:, None])).mean(1)
    c1, c2 = (0.01 * 255) ** 2, (0.03 * 255) ** 2
    s = ((2 * mu_a * mu_b + c1) * (2 * cov + c2)) / ((mu_a**2 + mu_b**2 + c1) * (va + vb + c2))
    return float(s.mean())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--samples", required=True,
                    help="directory of decoded I420 samples named by frame id")
    ap.add_argument("--refs", required=True, help="directory of reference .i420 frames")
    ap.add_argument("--out", help="write per-frame results to this CSV")
    ap.add_argument("--margin-db", type=float, default=2.0,
                    help="best match must beat the runner-up by this many dB")
    args = ap.parse_args()

    ref_paths = sorted(glob.glob(os.path.join(args.refs, "*.i420")))
    if not ref_paths:
        sys.exit(f"no reference frames in {args.refs}")
    refs = [load_y(p) for p in ref_paths]
    sig_matrix = np.stack([signature(y) for y in refs])
    print(f"loaded {len(refs)} reference frames", file=sys.stderr)

    sample_paths = sorted(
        glob.glob(os.path.join(args.samples, "*.i420"))
        + glob.glob(os.path.join(args.samples, "*.yuv"))
    )
    if not sample_paths:
        sys.exit(f"no samples in {args.samples}")

    # Pass 1: independent content match, keeping only the confident ones.
    #
    # Matching is possible at all only because the codec's error is smaller than the
    # gap between neighbouring frames -- measured at 31-40 dB apart on this clip
    # against a 42 dB encode. That margin is real but thin, so roughly half the
    # samples cannot be pinned on content alone and pass 2 places them.
    index = read_index(args.samples)
    if not index:
        print("WARNING: no index.csv; assuming every sample is at source geometry. "
              "A cell whose encoder stepped down will be read wrongly.", file=sys.stderr)

    # Samples are scored at BOTH geometries when they differ from the source, because
    # the two answer different questions and must never be pooled:
    #   at_reference  upscale the sample to the source size -- "how good is the picture
    #                 the operator sees", which charges the codec for the resolution drop
    #   at_delivered  downscale the source to the sample's size -- "how good is the codec
    #                 at the size it chose", which hides the drop entirely
    samples = []
    for sp in sample_paths:
        name = os.path.basename(sp)
        digits = "".join(ch for ch in os.path.splitext(name)[0] if ch.isdigit())
        fid = int(digits) if digits else None
        w, h, sy = index.get(fid, (W, H, W))
        y = load_y(sp, w, h, sy)
        samples.append((name, resize_y(y, (W, H)) if (w, h) != (W, H) else y, (w, h), y))
    confident = {}
    per_sample = {}
    for idx, (name, y, _native, _raw) in enumerate(samples):
        d = np.linalg.norm(sig_matrix - signature(y), axis=1)
        shortlist = np.argsort(d)[:15]
        scored = sorted(((psnr_y(y, refs[i]), i) for i in shortlist), reverse=True)
        best_db, best_i = scored[0]
        runner_db = scored[1][0] if len(scored) > 1 else -1e9
        per_sample[idx] = (best_db, best_i, best_db - runner_db)
        if best_db - runner_db >= args.margin_db:
            confident[idx] = best_i

    # Pass 2: the samples are in frame-id order and the source advances at a constant
    # stride, so the confident matches determine the rest. Fitting the line and then
    # CHECKING each placement is what makes this safe: a placement is accepted only if
    # its own PSNR beats what a neighbouring frame would score.
    placed = {}
    if len(confident) >= 2:
        ks = sorted(confident)
        strides = [(confident[b] - confident[a]) / (b - a) for a, b in zip(ks, ks[1:])]
        stride = float(np.median(strides))
        k0 = ks[0]
        for idx in range(len(samples)):
            predicted = int(round(confident[k0] + (idx - k0) * stride))
            if 0 <= predicted < len(refs):
                placed[idx] = predicted

    rows = []
    total_mse = 0.0
    n_scored = 0
    ssims = []
    for idx, (name, y, native, raw) in enumerate(samples):
        best_db, best_i, margin = per_sample[idx]
        if idx in confident:
            ref_i, how = confident[idx], "content"
        elif idx in placed:
            ref_i, how = placed[idx], "sequence"
        else:
            rows.append({"sample": name, "ref_index": "", "delivered_res": "", "psnr_at_reference_db": "",
                         "psnr_at_delivered_db": "", "ssim_y": "", "margin_db": f"{margin:.3f}",
                         "resampler": RESAMPLER_NAME, "matched_by": "none", "status": "UNMATCHED"})
            continue
        db = psnr_y(y, refs[ref_i])
        # A correct placement must beat what its neighbour would score; otherwise the
        # sequence fit has drifted and the frame is left unscored rather than guessed.
        neighbour = refs[ref_i + 1] if ref_i + 1 < len(refs) else refs[ref_i - 1]
        if db <= psnr_y(y, neighbour):
            rows.append({"sample": name, "ref_index": "", "delivered_res": "", "psnr_at_reference_db": "",
                         "psnr_at_delivered_db": "", "ssim_y": "", "margin_db": f"{margin:.3f}",
                         "resampler": RESAMPLER_NAME, "matched_by": how, "status": "REJECTED"})
            continue
        s = ssim_y(y, refs[ref_i])
        ssims.append(s)
        total_mse += (255.0**2) / (10 ** (db / 10.0))
        n_scored += 1
        # Scored at the delivered size too, when the encoder stepped down.
        if native != (W, H):
            ref_small = resize_y(refs[ref_i], native)
            db_delivered = f"{psnr_y(raw, ref_small):.3f}"
        else:
            db_delivered = f"{db:.3f}"
        rows.append({"sample": name, "ref_index": ref_i + 1,
                     "delivered_res": f"{native[0]}x{native[1]}",
                     "psnr_at_reference_db": f"{db:.3f}",
                     "psnr_at_delivered_db": db_delivered,
                     "ssim_y": f"{s:.5f}", "margin_db": f"{margin:.3f}",
                     "resampler": RESAMPLER_NAME,
                     "matched_by": how, "status": "ok"})

    ok = [r for r in rows if r["status"] == "ok"]
    print(f"samples {len(rows)}  scored {len(ok)}  "
          f"(content {sum(1 for r in ok if r['matched_by']=='content')}, "
          f"sequence {sum(1 for r in ok if r['matched_by']=='sequence')})  "
          f"unscored {len(rows) - len(ok)}")
    if ok:
        p = sorted(float(r["psnr_at_reference_db"]) for r in ok)
        # ffmpeg averages the MSE across frames and converts once. Averaging per-frame
        # dB instead reads ~3 dB high on this content, which would look like a codec
        # result rather than an arithmetic choice.
        agg = 10.0 * np.log10((255.0**2) / (total_mse / n_scored))
        print(f"PSNR-Y  {agg:.2f} dB  (ffmpeg-comparable, MSE-averaged)")
        print(f"        mean-of-frames {sum(p)/len(p):.2f}  median {p[len(p)//2]:.2f}  "
              f"min {p[0]:.2f}  max {p[-1]:.2f}")
        print(f"SSIM-Y  mean {sum(ssims)/len(ssims):.5f}")
    if args.out:
        with open(args.out, "w", newline="") as fh:
            wr = csv.DictWriter(fh, fieldnames=list(rows[0]))
            wr.writeheader()
            wr.writerows(rows)
        print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
