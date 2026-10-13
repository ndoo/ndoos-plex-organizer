from __future__ import annotations

import posixpath
import re
from collections import defaultdict
from dataclasses import dataclass

from . import clean as cleanmod
from .config import Config
from .model import MediaFile, PlexItem
from .naming import sanitize, with_year
from .pathmap import PathMapper
from .planner import SUB_DIRS

_EP_PATTERNS = (
    re.compile(r"\[S(\d{1,2})[ ._-]?E[pP]?[ ._-]?(\d{1,3})\]"),
    re.compile(r"[sS](\d{1,2})[ ._-]?[eE](\d{1,3})"),
)
_EP_ONLY = re.compile(r"(?:^|[ ._\[(-])[eE][pP]?[ ._-]?(\d{1,3})(?!\d)")
_SEASON_DIR = re.compile(r"^(?:season|series|s)[ ._-]?(\d{1,2})$", re.I)
_EXTRA_NAME = re.compile(
    r"featurette|behind the scenes|deleted scenes|making of|revisited|energized|sequence|sketches|art gallery"
    r"|short.*movie|commercial|trailer|relationship tree|interview",
    re.I)
_EXTRA_DIR = re.compile(r"(?<![a-z])(extras?|bonus|special features|featurettes)(?![a-z])", re.I)
_SPECIAL = re.compile(r"(?<![a-z])(ova|oad|special)(?![a-z])", re.I)


@dataclass
class Unhandled:
    path: str
    reason: str


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", re.sub(r"\(\d{4}[^)]*\)", "", text.lower()))


def _extra_type(name: str) -> str:
    name = re.sub(r"[._]+", " ", name)
    if re.search(r"deleted scenes", name, re.I):
        return "Deleted Scenes"
    if re.search(r"behind the scenes|making of", name, re.I):
        return "Behind The Scenes"
    if re.search(r"interview", name, re.I):
        return "Interviews"
    if re.search(r"short.*movie|\bshorts?\b", name, re.I):
        return "Shorts"
    if re.search(r"commercial|trailer", name, re.I):
        return "Trailers"
    if re.search(r"sequence", name, re.I):
        return "Other"
    return "Featurettes"


def parse_episode(name: str, parent_dirs: list[str]) -> tuple[int, int] | None:
    stem = posixpath.splitext(name)[0]
    for pat in _EP_PATTERNS:
        m = pat.search(stem)
        if m:
            return int(m.group(1)), int(m.group(2))
    m = _EP_ONLY.search(stem)
    if m:
        for d in reversed(parent_dirs):
            sm = _SEASON_DIR.match(d.strip())
            if sm:
                return int(sm.group(1)), int(m.group(1))
    return None


def _season_of(dirs: list[str]) -> int | None:
    for d in reversed(dirs):
        m = _SEASON_DIR.match(d.strip())
        if m:
            return int(m.group(1))
    return None


def load_aliases(records: list[dict], roots: list[str], mapper: PathMapper) -> dict[str, str]:
    """Old top-level folder name -> new show folder name, learned from the rename journal."""
    pairs: list[tuple[str, str]] = []
    for r in records:
        if r.get("type") == "fs":
            pairs.append((r["src"], r["dst"]))
        elif r.get("type") == "torrent":
            for _, rel, tgt in r["files"]:
                pairs.append((mapper.deluge_to_local(posixpath.join(r["old_save_path"], rel)),
                              mapper.deluge_to_local(posixpath.join(r["new_save_path"], tgt))))
    aliases: dict[str, str] = {}
    for root in roots:
        lr = mapper.to_local(root).rstrip("/") + "/"
        for old, new in pairs:
            if old.startswith(lr) and new.startswith(lr):
                a, b = old[len(lr):].split("/")[0], new[len(lr):].split("/")[0]
                if _norm(a) != _norm(b):
                    aliases[_norm(a)] = _norm(b)
    return aliases


def plan_ingest(cfg: Config, items: list[PlexItem], mapper: PathMapper,
                aliases: dict[str, str] | None = None) -> tuple[list[PlexItem], list[str], list[Unhandled]]:
    """Return synthetic items with fixed destinations, zero-byte stubs to purge, and files left unhandled."""
    shows: dict[tuple[str, str], PlexItem] = {}
    specials: dict[tuple[str, str], int] = defaultdict(int)
    for it in items:
        if it.kind == "episode" and it.show_title:
            shows.setdefault((it.library, _norm(it.show_title)), it)
            if it.season == 0:
                specials[(it.library, _norm(it.show_title))] = max(
                    specials[(it.library, _norm(it.show_title))], max(n for n, _ in it.episodes))
    out: list[PlexItem] = []
    stubs: list[str] = []
    unhandled: list[Unhandled] = []
    for s in cleanmod.find(cfg, items, mapper, None):
        if s.category != "review-video":
            continue
        if s.size == 0:
            stubs.append(s.path)
            continue
        root = next((it.root for it in items if s.path.startswith(it.root + "/")), None)
        lib = next((it.library for it in items if s.path.startswith(it.root + "/")), None)
        if not root:
            unhandled.append(Unhandled(s.path, "outside libraries"))
            continue
        rel = posixpath.relpath(s.path, root).split("/")
        dirs, name = rel[:-1], rel[-1]
        aliases = aliases or {}
        show = next((shows[(lib, k)] for d in dirs
                     for k in (_norm(d), aliases.get(_norm(d), "")) if (lib, k) in shows), None)
        if show is None:
            unhandled.append(Unhandled(s.path, "no matching Plex show in path"))
            continue
        base = with_year(show.show_title, show.show_year)
        ext = posixpath.splitext(name)[1]
        season = _season_of(dirs)
        ep = parse_episode(name, dirs)
        key = (lib, _norm(show.show_title))
        if any(_EXTRA_DIR.search(d) for d in dirs) or _EXTRA_NAME.search(re.sub(r"[._]+", " ", name)):
            parent = posixpath.join(root, base, f"Season {season:02d}") if season else posixpath.join(root, base)
            dst = posixpath.join(parent, _extra_type(name), sanitize(name))
        elif ep:
            title = ""
            dst = posixpath.join(root, base, f"Season {ep[0]:02d}", f"{base} - s{ep[0]:02d}e{ep[1]:02d}{title}{ext}")
        elif _SPECIAL.search(name):
            specials[key] += 1
            label = re.sub(r"[\[(][^\])]*[\])]", "", posixpath.splitext(name)[0]).replace("_", " ").strip(" -.")
            dst = posixpath.join(root, base, "Season 00", f"{base} - s00e{specials[key]:02d} - {sanitize(label)}{ext}")
        else:
            unhandled.append(Unhandled(s.path, "no episode number or extras pattern"))
            continue
        out.append(PlexItem(lib, "custom", root, name, files=[MediaFile(s.path)], dst_override={s.path: dst}))
    return out, stubs, unhandled


def plan_subtitle_rescue(cfg: Config, items: list[PlexItem], mapper: PathMapper, records: list[dict]) -> list[PlexItem]:
    """Move subtitles left behind by an earlier rename next to the video they belonged to.

    The rename journal says where each video went; a leftover subtitle whose name starts with the old
    video's stem follows it, in the same folder or in a Subs folder beside it.
    """
    moved: dict[tuple[str, str], tuple[str, str]] = {}  # (old dir, old stem) -> (new dir, new stem)
    video_exts = set(cfg.naming.video_exts)
    pairs: list[tuple[str, str]] = []
    for r in records:
        if r.get("type") == "fs":
            pairs.append((r["src"], r["dst"]))
        elif r.get("type") == "torrent":
            for _, rel, tgt in r["files"]:
                pairs.append((mapper.deluge_to_local(posixpath.join(r["old_save_path"], rel)),
                              mapper.deluge_to_local(posixpath.join(r["new_save_path"], tgt))))
    for old, new in pairs:
        if posixpath.splitext(old)[1].lower() in video_exts:
            moved[(posixpath.dirname(old), posixpath.splitext(posixpath.basename(old))[0])] = (
                posixpath.dirname(new), posixpath.splitext(posixpath.basename(new))[0])
    out: list[PlexItem] = []
    for s in cleanmod.find(cfg, items, mapper, None):
        if s.category != "keep-subtitle":
            continue
        local = mapper.to_local(s.path)
        here = posixpath.dirname(local)
        parents = [here] + ([posixpath.dirname(here)] if posixpath.basename(here).lower() in SUB_DIRS else [])
        name = posixpath.basename(local)
        hit = next(((new_dir, new_stem + name[len(stem):]) for d in parents for (od, stem), (new_dir, new_stem) in moved.items()
                    if od == d and name.startswith(stem + ".")), None)
        if not hit:
            continue
        root = next((it.root for it in items if s.path.startswith(it.root + "/")), None)
        lib = next((it.library for it in items if s.path.startswith(it.root + "/")), None)
        dst = posixpath.join(mapper.local_to_plex(hit[0]), hit[1])
        if root and dst != s.path:
            out.append(PlexItem(lib, "custom", root, name, files=[MediaFile(s.path)], dst_override={s.path: dst}))
    return out

