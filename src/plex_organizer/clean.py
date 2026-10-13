from __future__ import annotations

import fnmatch
import logging
import os
import posixpath
import re
from collections import defaultdict
from dataclasses import dataclass

from .config import DISC_DIRS, Config
from .delugeidx import DelugeApi, DelugeIndex
from .model import PlexItem
from .pathmap import PathMapper

log = logging.getLogger(__name__)

IMAGE_EXTS = {".iso", ".img", ".mdf", ".nrg", ".bin", ".cue", ".miniso"}
ARCHIVE_EXTS = re.compile(r"\.(rar|r\d{2,3}|zip|7z|sfv)$", re.I)
KEEP_GLOBS = ("poster.*", "folder.*", "cover.*", "fanart.*", "background.*", "banner.*", "theme.*", "season*-poster.*")


JUNK_NAMES = {".DS_Store"}
_SXE = re.compile(r"[sS](\d{1,2})[ ._-]?[eE](\d{1,3})")


_YEAR = re.compile(r"^(.+?)[ ._(\[-]+((?:19|20)\d{2})(?!\d)")


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _codes(name: str, parent: str = "") -> set[tuple]:
    """Show + season/episode, or movie title + year, found in a filename (or its folder name)."""
    m = _SXE.search(name)
    if not m:
        for source in (name, parent):
            y = _YEAR.match(source)
            if y:
                return {(_norm(y.group(1)), int(y.group(2)))}
        return set()
    show = re.sub(r"[^a-z0-9]", "", name[: m.start()].lower())
    show = re.sub(r"\d{4}$", "", show)
    return {(show, int(m.group(1)), int(m.group(2)))}


def _episode_tracked(name: str, parent: str, tracked_codes: set) -> bool:
    return bool(_codes(name, parent) & tracked_codes)


@dataclass
class Straggler:
    path: str  # Plex path
    size: int
    owner: str = "free"
    index: int = -1
    disc_root: str = ""  # Plex path of the disc folder, for keep-disc files
    category: str = "delete"  # delete | keep-subtitle | keep-asset | keep-disc | review-video


def find(cfg: Config, items: list[PlexItem], mapper: PathMapper, index: DelugeIndex | None) -> list[Straggler]:
    tracked = {f.path for it in items for f in it.files}
    roots = sorted({it.root for it in items})
    video_exts, sub_exts = set(cfg.naming.video_exts), {".srt", ".ass", ".ssa", ".sub", ".idx", ".sup", ".vtt"}
    stems_all = {code for p in tracked for code in _codes(posixpath.basename(p), posixpath.basename(posixpath.dirname(p)))}
    stems: dict[str, list[str]] = defaultdict(list)
    for p in tracked:
        stems[posixpath.dirname(p)].append(posixpath.splitext(posixpath.basename(p))[0])
    out: list[Straggler] = []
    for root in roots:
        local_root = mapper.to_local(root)
        disc_roots: set[str] = set()
        for d, dirs, _ in os.walk(local_root):
            if DISC_DIRS & set(dirs):
                disc_roots.add(d)
        for d, _, files in os.walk(local_root):
            rel = os.path.relpath(d, local_root)
            pdir = root if rel == "." else posixpath.join(root, rel)
            for name in files:
                p = posixpath.join(pdir, name)
                if p in tracked:
                    continue
                ext = posixpath.splitext(name)[1].lower()
                if any(name.startswith(s + ".") for s in stems.get(pdir, [])):
                    continue  # sidecar of a tracked video
                s = Straggler(p, os.path.getsize(os.path.join(d, name)))
                inside = [r for r in disc_roots if d == r or d.startswith(r + "/")]
                if inside:
                    s.category = "keep-disc"
                    best = max(inside, key=len)
                    rel_root = os.path.relpath(best, local_root)
                    s.disc_root = root if rel_root == "." else posixpath.join(root, rel_root)
                elif ext in IMAGE_EXTS:
                    s.category = "keep-image"
                elif name.startswith(".") and name not in JUNK_NAMES:
                    s.category = "review-temp"
                elif ARCHIVE_EXTS.search(name) and not _episode_tracked(name, posixpath.basename(pdir), stems_all):
                    s.category = "review-archive"
                elif ext in sub_exts:
                    s.category = "keep-subtitle"
                elif any(fnmatch.fnmatch(name.lower(), g) for g in KEEP_GLOBS):
                    s.category = "keep-asset"
                elif ext in video_exts and ".leftovers" not in p and not any(
                    fnmatch.fnmatch(name.lower(), g) for g in cfg.naming.leftover_globs
                ):
                    s.category = "review-video"
                if index is not None:
                    hit = index.lookup(mapper.to_deluge(p))
                    if hit:
                        s.owner, s.index = hit
                out.append(s)
    return out


def apply_clean(
    stragglers: list[Straggler], mapper: PathMapper, index: DelugeIndex | None, api: DelugeApi | None, log_path: str
) -> tuple[int, list[str]]:
    todo = [s for s in stragglers if s.category == "delete"]
    by_torrent: dict[str, list[Straggler]] = defaultdict(list)
    skipped: list[str] = []
    for s in todo:
        if s.owner != "free":
            by_torrent[s.owner].append(s)
    deletable = [s for s in todo if s.owner == "free"]
    for tid, group in by_torrent.items():
        t = index.torrents[tid]
        if len(group) >= len(t.files):
            skipped.append(f"{tid} {t.name}: every file is a straggler; remove the torrent in Deluge")
            continue
        if api is None:
            raise RuntimeError("torrent-owned stragglers but no Deluge connection")
        api.deselect_files(tid, [s.index for s in group])
        deletable += group
    removed = 0
    with open(log_path, "a") as fh:
        for s in deletable:
            local = mapper.to_local(s.path)
            try:
                os.remove(local)
            except FileNotFoundError:
                continue
            fh.write(s.path + "\n")
            removed += 1
    return removed, skipped


def summarize(stragglers: list[Straggler]) -> str:
    agg: dict[tuple[str, str, str], list[int]] = defaultdict(lambda: [0, 0])
    for s in stragglers:
        ext = posixpath.splitext(s.path)[1].lower() or "(none)"
        e = agg[(s.category, "torrent" if s.owner != "free" else "free", ext)]
        e[0] += 1
        e[1] += s.size
    lines = [f"{n:7d} {size / 1e6:10.1f}MB  {cat:14s} {own:8s} {ext}" for (cat, own, ext), (n, size) in sorted(agg.items())]
    return "\n".join(lines)


@dataclass
class DiscMove:
    root: str  # Plex path of the folder containing BDMV / VIDEO_TS
    dest: str  # Plex path of the target folder
    owner: str = "free"
    reason: str = ""


def plan_discs(cfg: Config, items: list[PlexItem], stragglers: list[Straggler], mapper: PathMapper,
               index: DelugeIndex | None) -> list[DiscMove]:
    if not cfg.disc_dir:
        return []
    tracked = [f.path for it in items for f in it.files]
    roots: dict[str, None] = {}
    for s in stragglers:
        if s.category == "keep-disc" and s.disc_root:
            roots[s.disc_root] = None
    moves = []
    for root in sorted(roots):
        name = posixpath.basename(root)
        if re.match(r"(?i)^(season|disc|dvd|bd|vol(ume)?)[ _.-]?\d*$", name):
            name = f"{posixpath.basename(posixpath.dirname(root))} - {name}"
        m = DiscMove(root, posixpath.join(cfg.disc_dir, name))
        files = []
        for d, _, names in os.walk(mapper.to_local(root)):
            rel = os.path.relpath(d, mapper.to_local(root))
            files += [posixpath.join(root, rel, n) if rel != "." else posixpath.join(root, n) for n in names]
        owners = {index.lookup(mapper.to_deluge(f))[0] if index and index.lookup(mapper.to_deluge(f)) else None
                  for f in files}
        if any(t.startswith(root + "/") for t in tracked):
            m.reason = "Plex tracks files inside"
        elif os.path.exists(mapper.to_local(m.dest)):
            m.reason = "destination exists"
        elif owners == {None}:
            pass
        elif None in owners or len(owners) > 1:
            m.reason = "mixed ownership"
        else:
            tid = next(iter(owners))
            t = index.torrents[tid]
            root_d = mapper.to_deluge(root)
            if posixpath.dirname(root_d) != t.save_path:
                m.reason = "disc nested inside torrent"
            elif any(not t.abs_path(rel).startswith(root_d + "/") for _, rel in t.files):
                m.reason = "torrent has files outside the disc"
            elif t.busy:
                m.reason = "torrent busy"
            else:
                m.owner = tid
        moves.append(m)
    return moves


def apply_discs(moves: list[DiscMove], mapper: PathMapper, index: DelugeIndex | None, api: DelugeApi | None,
                journal) -> int:
    from .delugeidx import wait_for

    done = 0
    for m in moves:
        if m.reason:
            continue
        os.makedirs(mapper.to_local(posixpath.dirname(m.dest)), exist_ok=True)
        if m.owner == "free":
            src, dst = mapper.to_local(m.root), mapper.to_local(m.dest)
            journal.write({"type": "fs", "src": src, "dst": dst})
            os.rename(src, dst)
        else:
            t = index.torrents[m.owner]
            new_sp = mapper.to_deluge(posixpath.dirname(m.dest))
            journal.write({"type": "torrent", "id": m.owner, "old_save_path": t.save_path, "new_save_path": new_sp,
                           "files": [[i, rel, rel] for i, rel in t.files]})
            api.move_storage(m.owner, new_sp)
            wait_for(api, m.owner, lambda x: x.save_path == new_sp)
        done += 1
    return done
