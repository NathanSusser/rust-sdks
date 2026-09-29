#!/usr/bin/env python3
"""Gather every results/log location on this host into ~/teleop-runs and remove duplicates.

Two phases, both run with --dry-run first:

  move   Each source location is moved (rename, same filesystem) to
         ~/teleop-runs/archive/<name>. Nothing is copied or lost. Symlinks that
         pointed into moved trees are removed.

  dedupe Every file under ~/teleop-runs is grouped by size, candidates are hashed
         (sha256), and within each group of byte-identical files ONE copy is kept:
         the one in the highest-priority tree (see TIERS), ties broken by path. The
         others are deleted. A zip is deleted only when every member's content
         (sha256 of the decompressed bytes) exists as a kept file. Hardlinks of the
         same inode count as one copy. Nothing is compared across hosts: a cell's
         copy on A and its mirror on B are both kept.

The plan (every move, every deletion with the path of the copy that is kept) is
written to ~/teleop-runs/archive/CONSOLIDATION-<host>-<utc>.tsv before anything is
done, and the tool refuses to delete a file whose kept twin it cannot re-verify.

Usage: consolidate_results.py --host a|b [--dry-run] [--phase move|dedupe|all]
Standard library only (runs on both hosts).
"""
from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sys
import time
import zipfile
from collections import defaultdict
from pathlib import Path

HOME = Path.home()
ROOT = HOME / "teleop-runs"
ARCHIVE = ROOT / "archive"

# (source, name under archive/). Missing sources are skipped.
SOURCES = {
    "a": [
        (HOME / "code/rust-sdks/results", "results-hosta"),
        (HOME / "teleop-share-20260925", "teleop-share-20260925"),
        (HOME / "network-analysis-2026-09-23", "network-analysis-2026-09-23"),
        (HOME / "qxdm-raw-diag-2026-09-23", "qxdm-raw-diag-2026-09-23"),
        (HOME / "teleop-packet-events-2026-09-23", "teleop-packet-events-2026-09-23"),
        (HOME / "teleop-archive-2026-09", "teleop-archive-2026-09"),
        (HOME / "hostb-loss-cells-2026-09-24", "hostb-loss-cells-2026-09-24"),
        (HOME / "teleop", "teleop-index-hosta"),
        (HOME / "hostb-dlf", "hostb-dlf"),
        (HOME / "vendor-escalation-2026-09-18-beam", "vendor-escalation-2026-09-18-beam"),
        (HOME / "sys-logs", "sys-logs-hosta"),
        (HOME / "network-analysis-2026-09-23.zip", "zips/network-analysis-2026-09-23.zip"),
        (HOME / "teleop-packet-events-2026-09-23.zip", "zips/teleop-packet-events-2026-09-23.zip"),
        (HOME / "vendor-escalation-2026-09-18-beam.zip", "zips/vendor-escalation-2026-09-18-beam.zip"),
    ],
    "b": [
        (HOME / "teleop", "teleop-hostb"),
        (HOME / "diag-logs", "diag-logs-hostb"),
        (HOME / "pcap-logs", "pcap-logs-hostb"),
        (HOME / "teleop-archive-2026-09", "teleop-archive-2026-09"),
        (HOME / "cell5m-a3-logs.tar.gz", "loose/cell5m-a3-logs.tar.gz"),
        (HOME / "cell5m-a_hostB_event_t288s.dlf", "loose/cell5m-a_hostB_event_t288s.dlf"),
        (HOME / "stale-test.dlf", "loose/stale-test.dlf"),
        (HOME / "hosta", "from-hosta-reductions"),
        (HOME / "incoming-hosta", "incoming-hosta"),
        (HOME / "sys-logs", "sys-logs-hostb"),
    ],
}

# Lower number = kept in preference. Paths are relative to ROOT.
TIERS = [
    (0, lambda r: not r.startswith("archive/")),                 # new-layout grids and legacy/
    (1, lambda r: r.startswith("archive/results-hosta/")),        # A's primary raw captures
    (1, lambda r: r.startswith("archive/teleop-hostb/cells/")),   # B's primary cell folders
    (1, lambda r: r.startswith("archive/diag-logs-hostb/")),
    (1, lambda r: r.startswith("archive/pcap-logs-hostb/")),
    (2, lambda r: r.startswith("archive/teleop-archive-2026-09/")),
    (2, lambda r: r.startswith("archive/teleop-hostb/")),
    (3, lambda r: True),                                          # assembled share/analysis folders
]
SKIP_NAMES = {"CONSOLIDATION"}  # the plan files themselves


def tier(rel: str) -> int:
    for t, pred in TIERS:
        if pred(rel):
            return t
    return 9


# Read-rate cap, MB/s. The first full run on Host A (2026-09-29) read hundreds of GB flat
# out; the desktop froze and the machine had to be reset. Every read now goes through
# this limiter, and the tool should be started under `ionice -c3 nice -n19`.
MAX_MBPS = 100.0


def sha256(path: Path, bufsize: int = 4 << 20) -> str:
    h = hashlib.sha256()
    t0, done = time.monotonic(), 0
    with open(path, "rb") as f:
        fd = f.fileno()
        while chunk := f.read(bufsize):
            h.update(chunk)
            done += len(chunk)
            ahead = done / (MAX_MBPS * 1e6) - (time.monotonic() - t0)
            if ahead > 0:
                time.sleep(ahead)
        try:  # do not let a one-pass read evict everything else from the page cache
            os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
        except (AttributeError, OSError):
            pass
    return h.hexdigest()


def apply_plan(plan_path: Path, log) -> None:
    """Delete the duplicates listed by a dry run, re-verifying each pair first.

    A listed duplicate is deleted only if it still exists, its kept twin still exists,
    and (for separate copies) both still hash to the recorded sha256. Hardlinks of the
    kept inode are unlinked without reading anything."""
    removed = freed = skipped = 0
    rows = [l.rstrip("\n").split("\t") for l in open(plan_path) if l.startswith("delete\t")]
    print(f"  {len(rows)} planned deletions from {plan_path.name}")
    for i, r in enumerate(rows, 1):
        dup = Path(r[1])
        if not r[2].startswith("keep="):
            skipped += 1
            continue
        keep, h, kind, size = Path(r[2][5:]), r[3].removeprefix("sha256="), r[4], int(r[5])
        if not dup.exists() or not keep.exists() or dup == keep:
            skipped += 1
            log.write(f"skip\t{dup}\tmissing or same path\n")
            continue
        ds, ks = dup.stat(), keep.stat()
        same_inode = (ds.st_dev, ds.st_ino) == (ks.st_dev, ks.st_ino)
        if not same_inode and (ds.st_size != ks.st_size or sha256(keep) != h or sha256(dup) != h):
            skipped += 1
            log.write(f"skip\t{dup}\tno longer identical to {keep}\n")
            continue
        dup.unlink()
        removed += 1
        if not same_inode and ds.st_nlink == 1:
            freed += size
        log.write(f"deleted\t{dup}\tkeep={keep}\t{kind}\t{size}\n")
        if i % 100 == 0:
            print(f"  {i}/{len(rows)}  removed {removed}, freed {human(freed)}", flush=True)
    for dirpath, dirnames, filenames in os.walk(ARCHIVE, topdown=False):
        p = Path(dirpath)
        if p != ARCHIVE and not any(p.iterdir()):
            p.rmdir()
    print(f"  removed {removed} duplicate paths, freed {human(freed)}, skipped {skipped}")


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n} B"


def phase_move(host: str, dry: bool, log) -> None:
    ARCHIVE.mkdir(parents=True, exist_ok=True)
    moved_roots = []
    for src, name in SOURCES[host]:
        if not src.exists() and not src.is_symlink():
            continue
        dst = ARCHIVE / name
        if dst.exists():
            print(f"  SKIP move {src} -> {dst}: destination exists", file=sys.stderr)
            continue
        log.write(f"move\t{src}\t{dst}\n")
        print(f"  move {src} -> {dst}")
        moved_roots.append(dst)
        if not dry:
            dst.parent.mkdir(parents=True, exist_ok=True)
            os.rename(src, dst)          # same filesystem: atomic, no copy
    # symlinks inside moved trees that now dangle (e.g. latest-cell) are removed
    for root in moved_roots:
        if dry or not root.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            for n in dirnames + filenames:
                p = Path(dirpath) / n
                if p.is_symlink() and not p.exists():
                    log.write(f"rmlink\t{p}\t{os.readlink(p)}\n")
                    p.unlink()


def walk_files(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        for n in filenames:
            p = Path(dirpath) / n
            if p.is_symlink() or not p.is_file():
                continue
            if any(n.startswith(s) for s in SKIP_NAMES):
                continue
            yield p


def phase_dedupe(dry: bool, log) -> None:
    by_size: dict[int, list[Path]] = defaultdict(list)
    zips: list[Path] = []
    for p in walk_files(ROOT):
        st = p.stat()
        if st.st_size == 0:
            continue
        by_size[st.st_size].append(p)
        if p.suffix == ".zip":
            zips.append(p)

    # hash only sizes shared by at least two distinct inodes
    content: dict[str, list[Path]] = defaultdict(list)
    kept_hashes: set[str] = set()
    t0 = time.time()
    todo = [(s, ps) for s, ps in by_size.items() if len({(q.stat().st_dev, q.stat().st_ino) for q in ps}) > 1]
    print(f"  hashing {sum(len(ps) for _, ps in todo)} candidate files "
          f"({human(sum(s * len(ps) for s, ps in todo))})")
    for s, ps in todo:
        seen_inode: dict[tuple, str] = {}
        for p in ps:
            st = p.stat()
            key = (st.st_dev, st.st_ino)
            h = seen_inode.get(key) or sha256(p)
            seen_inode[key] = h
            content[h].append(p)
    print(f"  hashed in {time.time() - t0:.0f} s")

    freed = 0
    removed = 0
    for h, ps in content.items():
        if len(ps) < 2:
            continue
        ps.sort(key=lambda p: (tier(str(p.relative_to(ROOT))), str(p)))
        keep = ps[0]
        kept_hashes.add(h)
        keep_inode = (keep.stat().st_dev, keep.stat().st_ino)
        for dup in ps[1:]:
            st = dup.stat()
            same_inode = (st.st_dev, st.st_ino) == keep_inode
            # a hardlink of the kept inode frees nothing but is still a duplicate path
            log.write(f"delete\t{dup}\tkeep={keep}\tsha256={h}\t{'hardlink' if same_inode else 'copy'}\t{st.st_size}\n")
            removed += 1
            if not same_inode and st.st_nlink == 1:
                freed += st.st_size
            if not dry:
                if not same_inode and sha256(keep) != h:
                    print(f"  REFUSED: kept twin {keep} no longer matches; leaving {dup}", file=sys.stderr)
                    continue
                dup.unlink()

    # zips: delete only when every member is present as a kept file
    for z in zips:
        if not z.exists():
            continue
        try:
            with zipfile.ZipFile(z) as zf:
                members = [i for i in zf.infolist() if not i.is_dir() and i.file_size > 0]
                missing = 0
                for info in members:
                    hh = hashlib.sha256()
                    with zf.open(info) as f:
                        while chunk := f.read(8 << 20):
                            hh.update(chunk)
                    d = hh.hexdigest()
                    if d not in content and d not in kept_hashes:
                        # present only if some file on disk has this content; check by size first
                        cands = by_size.get(info.file_size, [])
                        if not any(sha256(c) == d for c in cands if c.exists()):
                            missing += 1
                            break
        except zipfile.BadZipFile:
            log.write(f"keepzip\t{z}\tbad zip\n")
            continue
        if missing:
            log.write(f"keepzip\t{z}\thas members not present elsewhere\n")
            print(f"  keep {z.name}: some members exist only inside it")
            continue
        size = z.stat().st_size
        log.write(f"delete\t{z}\tall {len(members)} members present elsewhere\tzip\t{size}\n")
        print(f"  zip {z.name}: all {len(members)} members present elsewhere -> delete ({human(size)})")
        removed += 1
        freed += size
        if not dry:
            z.unlink()

    # empty directories left behind
    if not dry:
        for dirpath, dirnames, filenames in os.walk(ARCHIVE, topdown=False):
            p = Path(dirpath)
            if p != ARCHIVE and not any(p.iterdir()):
                p.rmdir()
    print(f"  duplicates {'to remove' if dry else 'removed'}: {removed} paths, {human(freed)} freed")


def phase_hardlinks(dry: bool, log) -> None:
    """Remove extra PATHS that are hardlinks of the same file (same inode).

    They cost no disk space but read as duplicates. Metadata only: nothing is read.
    Within each inode the highest-priority path (TIERS) is kept."""
    by_inode: dict[tuple, list[Path]] = defaultdict(list)
    for p in walk_files(ROOT):
        st = p.stat()
        if st.st_nlink > 1:
            by_inode[(st.st_dev, st.st_ino)].append(p)
    removed = 0
    for key, ps in by_inode.items():
        if len(ps) < 2:
            continue
        ps.sort(key=lambda p: (tier(str(p.relative_to(ROOT))), str(p)))
        keep = ps[0]
        for dup in ps[1:]:
            log.write(f"delete\t{dup}\tkeep={keep}\tinode={key[1]}\thardlink\t0\n")
            removed += 1
            if not dry:
                st = dup.stat()
                if (st.st_dev, st.st_ino) == key and keep.exists():
                    dup.unlink()
    if not dry:
        for dirpath, dirnames, filenames in os.walk(ARCHIVE, topdown=False):
            q = Path(dirpath)
            if q != ARCHIVE and not any(q.iterdir()):
                q.rmdir()
    print(f"  hardlinked duplicate paths {'to remove' if dry else 'removed'}: {removed} (no disk space involved)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", choices=("a", "b"), required=True)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--phase", choices=("move", "dedupe", "hardlinks", "all"), default="all")
    ap.add_argument("--apply-plan", type=Path, help="delete the duplicates listed in a dry-run .tsv")
    ap.add_argument("--max-mbps", type=float, default=100.0, help="read-rate cap in MB/s (default 100)")
    a = ap.parse_args()
    globals()["MAX_MBPS"] = a.max_mbps
    ARCHIVE.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    plan = ARCHIVE / f"CONSOLIDATION-host{a.host}-{stamp}{'-dryrun' if a.dry_run else ''}.tsv"
    with open(plan, "w") as log:
        log.write("# action\tpath\tdetail...\n")
        if a.apply_plan:
            print("apply plan:")
            apply_plan(a.apply_plan, log)
            print(f"plan/log: {plan}")
            return 0
        if a.phase in ("move", "all"):
            print("move:")
            phase_move(a.host, a.dry_run, log)
        if a.phase == "hardlinks":
            print("hardlinks:")
            phase_hardlinks(a.dry_run, log)
        if a.phase in ("dedupe", "all"):
            print("dedupe:")
            phase_dedupe(a.dry_run, log)
    print(f"plan/log: {plan}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
