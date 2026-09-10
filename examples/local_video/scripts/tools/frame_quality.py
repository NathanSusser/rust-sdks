#!/usr/bin/env python3
"""Quality of sampled I420 frames: PSNR against a reference, and viewable PNGs.

The operator's question is "what is the minimum bitrate that still looks usable", and
loss/QP/resolution cannot answer it -- a stream can arrive complete and still look bad.
This reads the frames the subscriber sampled and turns them into a number and a picture.

No numpy on this host and no ffmpeg either, so:
  * PSNR uses PIL's ImageChops.difference + ImageStat, both C-speed. A pure-Python loop
    over 2.08 M pixels per frame would take minutes per cell.
  * I420 -> RGB is done by upsampling the chroma planes and merging as YCbCr, which is
    what makes the PNGs viewable without a decoder.

PSNR is computed on the LUMA plane only. That is the convention for video quality work
and it is the plane the eye weights most; chroma at 4:2:0 is already subsampled and a
combined figure would flatter every codec equally without discriminating between them.
"""
import argparse, math, sys
from pathlib import Path
from PIL import Image, ImageChops, ImageStat


def load_planes(path: Path, w: int, h: int):
    """Y, U, V from a planar I420 file. Returns None if the file is the wrong size."""
    data = path.read_bytes()
    ys, cs = w * h, (w // 2) * (h // 2)
    if len(data) < ys + 2 * cs:
        return None
    y = Image.frombytes("L", (w, h), data[:ys])
    u = Image.frombytes("L", (w // 2, h // 2), data[ys:ys + cs])
    v = Image.frombytes("L", (w // 2, h // 2), data[ys + cs:ys + 2 * cs])
    return y, u, v


def psnr(a: Image.Image, b: Image.Image) -> float:
    if a.size != b.size:
        b = b.resize(a.size, Image.BICUBIC)
    rms = ImageStat.Stat(ImageChops.difference(a, b)).rms[0]
    if rms <= 0:
        return float("inf")          # identical; report as inf rather than a huge number
    return 20.0 * math.log10(255.0 / rms)


def to_rgb(y: Image.Image, u: Image.Image, v: Image.Image) -> Image.Image:
    full = y.size
    return Image.merge("YCbCr", (y, u.resize(full, Image.BILINEAR),
                                 v.resize(full, Image.BILINEAR))).convert("RGB")


def frames_in(d: Path):
    return sorted(d.glob("*.i420"), key=lambda p: int(p.stem))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cell_dir", type=Path, help="directory holding *.i420")
    ap.add_argument("--width", type=int, default=1600)
    ap.add_argument("--height", type=int, default=1300)
    ap.add_argument("--ref-dir", type=Path,
                    help="reference I420 frames, named by SOURCE index (true PSNR)")
    ap.add_argument("--against", type=Path,
                    help="another cell dir to compare to (RELATIVE ladder, no true ref)")
    ap.add_argument("--loop-frames", type=int, default=808,
                    help="source clip length; source index = frame_id %% this")
    ap.add_argument("--png-dir", type=Path, help="also write viewable PNGs here")
    ap.add_argument("--png-every", type=int, default=1)
    args = ap.parse_args()

    files = frames_in(args.cell_dir)
    if not files:
        print(f"no .i420 frames in {args.cell_dir}", file=sys.stderr)
        return 1

    # Sizes are not assumed: a cell whose resolution collapsed writes smaller frames, and
    # silently comparing a 300x240 frame against a 1600x1300 reference would report a
    # catastrophic PSNR that is really a geometry mismatch.
    sizes = {}
    scores, used = [], 0
    if args.png_dir:
        args.png_dir.mkdir(parents=True, exist_ok=True)

    for i, f in enumerate(files):
        planes = None
        for (w, h) in ((args.width, args.height), (1200, 972), (800, 648),
                       (600, 480), (400, 324), (300, 240)):
            planes = load_planes(f, w, h)
            if planes and len(f.read_bytes()) == w * h * 3 // 2:
                sizes[f"{w}x{h}"] = sizes.get(f"{w}x{h}", 0) + 1
                break
            planes = None
        if planes is None:
            continue
        y, u, v = planes
        fid = int(f.stem)

        ref = None
        if args.ref_dir:
            src = fid % args.loop_frames
            for cand in (args.ref_dir / f"{src:04d}.i420", args.ref_dir / f"{src:08d}.i420",
                         args.ref_dir / f"ref-{src+1:04d}.i420"):
                if cand.exists():
                    ref = load_planes(cand, args.width, args.height)
                    break
        elif args.against:
            cand = args.against / f.name
            if cand.exists():
                ref = load_planes(cand, args.width, args.height) or load_planes(
                    cand, *[int(x) for x in list(sizes)[-1].split("x")])
        if ref:
            scores.append(psnr(ref[0], y)); used += 1

        if args.png_dir and i % args.png_every == 0:
            to_rgb(y, u, v).save(args.png_dir / f"{fid:08d}.png")

    print(f"cell {args.cell_dir.name}")
    print(f"  frames sampled   {len(files)}")
    print(f"  geometries       {sizes}")
    if scores:
        scores.sort()
        finite = [s for s in scores if s != float('inf')]
        if finite:
            print(f"  luma PSNR        p50 {finite[len(finite)//2]:.2f} dB   "
                  f"min {finite[0]:.2f}   max {finite[-1]:.2f}   n={used}")
    else:
        print("  luma PSNR        no reference matched -- pass --ref-dir or --against")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
