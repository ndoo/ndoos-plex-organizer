from __future__ import annotations

import csv
import fnmatch
import os
import posixpath
import re
import time
from collections import defaultdict

from . import naming
from .config import DISC_DIRS, Config
from .delugeidx import DelugeIndex
from .model import MediaFile, Op, PlexItem
from .pathmap import PathMapper
from .plexsrc import path_excluded


_NORMALIZED = re.compile(r"^(.+? \(\d{4}\))")


def _label_key(size_of, files: list[MediaFile], mi: int) -> tuple:
    f = next(f for f in files if f.media_index == mi)
    return (size_of(f.path), f.path)


def _labels(files: list[MediaFile], size_of=lambda p: 0) -> dict[int, str | None]:
    """Version labels per media index; None when the item has a single version."""
    first: dict[int, MediaFile] = {}
    for f in files:
        first.setdefault(f.media_index, f)
    if len(first) < 2:
        return {mi: None for mi in first}
    labels = {mi: naming.media_label(f.resolution, f.hdr) for mi, f in first.items()}
    counts: dict[str, int] = defaultdict(int)
    for lab in labels.values():
        counts[lab] += 1
    ranked = {mi: n for n, mi in enumerate(sorted(labels, key=lambda mi: _label_key(size_of, files, mi)), 1)}
    return {mi: (f"{lab} #{ranked[mi]}" if counts[lab] > 1 or not lab else lab) for mi, lab in labels.items()}


def _destinations(item: PlexItem, part_paths: tuple[str, ...] = (), size_of=lambda p: 0) -> list[tuple[MediaFile, str]]:
    labels = _labels(item.files, size_of)
    split = item.kind == "movie" and any(fnmatch.fnmatch(f.path, g) for f in item.files for g in part_paths)
    order = {f.path: n for n, f in enumerate(sorted(item.files, key=lambda f: f.path), 1)}
    if item.dst_override:
        return [(f, item.dst_override[f.path]) for f in item.files]
    out = []
    for f in item.files:
        ext = posixpath.splitext(f.path)[1]
        part = f.part_index + 1 if f.part_count > 1 else None
        label = labels.get(f.media_index)
        if split:
            part, label = order[f.path], None
        if item.kind == "movie":
            folder, fname = naming.movie_names(
                item.title, item.year, ext, edition=item.edition, label=label, part=part, id_tag=item.id_tag
            )
            dst = posixpath.join(item.root, folder, fname)
        else:
            show, season, fname = naming.episode_names(
                item.show_title or item.title,
                item.show_year,
                item.season or 0,
                item.episodes,
                ext,
                label=label,
                part=part,
                id_tag=item.id_tag,
            )
            dst = posixpath.join(item.root, show, season, fname)
        out.append((f, dst))
    return out


_COUNTER = re.compile(r" #\d+(?=\.)")


def _only_counter_differs(src: str, dst: str) -> bool:
    return (
        posixpath.dirname(src) == posixpath.dirname(dst)
        and _COUNTER.sub("", posixpath.basename(src)) == _COUNTER.sub("", posixpath.basename(dst))
    )


def _title_changed(src: str, dst: str) -> bool:
    a, b = _NORMALIZED.match(posixpath.basename(src)), _NORMALIZED.match(posixpath.basename(dst))
    return bool(a and b and a.group(1) != b.group(1))


SUB_DIRS = {"subs", "sub", "subtitles", "subtitle"}


def _sidecars(cfg: Config, mapper: PathMapper, src: str, dst: str) -> list[tuple[str, str]]:
    src_dir, src_name = posixpath.split(src)
    stem = posixpath.splitext(src_name)[0]
    dst_dir = posixpath.dirname(dst)
    dst_stem = posixpath.splitext(posixpath.basename(dst))[0]
    scan = [src_dir]
    try:
        scan += [posixpath.join(src_dir, d) for d in sorted(os.listdir(mapper.to_local(src_dir)))
                 if d.lower() in SUB_DIRS and os.path.isdir(os.path.join(mapper.to_local(src_dir), d))]
    except OSError:
        return []
    out = []
    for directory in scan:
        try:
            names = os.listdir(mapper.to_local(directory))
        except OSError:
            continue
        for n in sorted(names):
            if (directory == src_dir and n == src_name) or not n.startswith(stem + "."):
                continue
            if posixpath.splitext(n)[1].lower() not in cfg.naming.sidecar_exts:
                continue
            out.append((posixpath.join(directory, n), posixpath.join(dst_dir, dst_stem + n[len(stem):])))
    return out


def _torrent_excluded(cfg: Config, index: DelugeIndex, tid: str) -> bool:
    t = index.torrents[tid]
    ex = cfg.exclude
    return (
        tid in ex.torrents
        or any(fnmatch.fnmatch(t.name, g) for g in ex.torrents)
        or bool(t.label and t.label in ex.torrent_labels)
    )


def build_plan(
    cfg: Config,
    items: list[PlexItem],
    mapper: PathMapper,
    index: DelugeIndex | None,
    *,
    settle: bool = False,
) -> list[Op]:
    ops: list[Op] = []

    def size_of(path: str) -> int:
        try:
            return os.path.getsize(mapper.to_local(path))
        except OSError:
            return 0

    for item in items:
        title = item.show_title or item.title
        for f, dst in _destinations(item, cfg.naming.part_paths, size_of):
            is_sub = item.kind == "custom" and posixpath.splitext(f.path)[1].lower() in cfg.naming.sidecar_exts
            pairs = [(f.path, dst, False)]
            if not is_sub:
                pairs += [(s, d, True) for s, d in _sidecars(cfg, mapper, f.path, dst)]
            for src, d, is_side in pairs:
                op = Op(item.library, item.root, src, d, title, sidecar=is_side)
                if src == d or _only_counter_differs(src, d):
                    op.action, op.reason = "ok", "already named"
                elif item.kind == "movie" and not item.year:
                    op.action, op.reason = "skip", "no year (unmatched in Plex)"
                elif item.kind == "movie" and _title_changed(src, d):
                    op.action, op.reason = "skip", "Plex match changed; review"
                ops.append(op)

    by_src: dict[str, Op] = {}
    for op in ops:
        if op.src in by_src:
            op.action, op.reason = "skip", "file claimed by another item"
        else:
            by_src[op.src] = op

    seen_dst: dict[str, Op] = {}
    for op in ops:
        local_src = mapper.to_local(op.src)
        if index is not None:
            hit = index.lookup(mapper.to_deluge(op.src))
            if hit:
                op.owner = hit[0]
        if op.action != "move":
            seen_dst.setdefault(op.dst, op)
            continue
        if DISC_DIRS & set(op.src.split("/")):
            op.action, op.reason = "skip", "inside disc structure"
        elif path_excluded(cfg, op.src):
            op.action, op.reason = "skip", "excluded path"
        elif not os.path.exists(local_src):
            op.action, op.reason = "skip", "source missing"
        elif op.dst in seen_dst:
            op.action, op.reason = "skip", "duplicate target"
        elif os.path.exists(mapper.to_local(op.dst)) and op.dst not in by_src:
            op.action, op.reason = "skip", "target exists"
        elif op.owner != "free" and index is not None:
            if _torrent_excluded(cfg, index, op.owner):
                op.action, op.reason = "skip", "excluded torrent"
            elif index.torrents[op.owner].busy:
                op.action, op.reason = "skip", "torrent busy"
            elif settle and not index.torrents[op.owner].complete:
                op.action, op.reason = "skip", "torrent incomplete"
        elif settle and time.time() - os.path.getmtime(local_src) < cfg.settle_seconds:
            op.action, op.reason = "skip", "recently modified"
        seen_dst.setdefault(op.dst, op)

    if index is not None:
        _torrent_pass(cfg, mapper, index, ops, by_src)
    return ops


def _torrent_pass(cfg: Config, mapper: PathMapper, index: DelugeIndex, ops: list[Op], by_src: dict[str, Op]) -> None:
    groups: dict[str, list[Op]] = defaultdict(list)
    for op in ops:
        if op.owner != "free":
            groups[op.owner].append(op)
    for tid, tops in groups.items():
        if all(o.action == "ok" for o in tops):
            continue
        t = index.torrents[tid]
        problem = None
        if len({o.root for o in tops}) > 1:
            problem = "torrent spans libraries"
        for _, rel in t.files:
            if problem:
                break
            peer = by_src.get(mapper.deluge_to_plex(t.abs_path(rel)))
            if peer is None:
                base = posixpath.basename(rel).lower()
                if posixpath.splitext(rel)[1].lower() in cfg.naming.video_exts and not any(
                    fnmatch.fnmatch(base, g) for g in cfg.naming.leftover_globs
                ):
                    problem = "torrent has unmatched video"
            elif peer.action == "skip":
                problem = f"torrent blocked ({peer.reason})"
        if problem:
            for o in tops:
                if o.action == "move":
                    o.action, o.reason = "skip", problem


def write_csv(ops: list[Op], path: str) -> None:
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["library", "action", "reason", "owner", "src", "dst"])
        for o in ops:
            w.writerow([o.library, o.action, o.reason, o.owner, o.src, o.dst])
