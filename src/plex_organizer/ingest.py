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

_EP_PATTERNS = (
    re.compile(r"\[S(\d{1,2})[ ._-]?E[pP]?[ ._-]?(\d{1,3})\]"),
    re.compile(r"[sS](\d{1,2})[ ._-]?[eE](\d{1,3})"),
)
_EP_ONLY = re.compile(r"(?:^|[ ._\[(-])[eE][pP]?[ ._-]?(\d{1,3})(?!\d)")
_SEASON_DIR = re.compile(r"^(?:season|series|s)[ ._-]?(\d{1,2})$", re.I)
_EXTRA_DIRS = {"extras", "dvd extras", "bonus", "featurettes", "special features"}
_EXTRA_NAME = re.compile(
    r"featurette|behind the scenes|making of|revisited|energized|sequence|sketches|art gallery|short.*movie|commercial|trailer",
    re.I)
_SPECIAL = re.compile(r"\b(ova|oad|special)\b", re.I)


@dataclass
class Unhandled:
    path: str
    reason: str


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", re.sub(r"\(\d{4}[^)]*\)", "", text.lower()))


def _extra_type(name: str) -> str:
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


def plan_ingest(cfg: Config, items: list[PlexItem], mapper: PathMapper) -> tuple[list[PlexItem], list[str], list[Unhandled]]:
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
        show = next((shows[(lib, _norm(d))] for d in dirs if (lib, _norm(d)) in shows), None)
        if show is None:
            unhandled.append(Unhandled(s.path, "no matching Plex show in path"))
            continue
        base = with_year(show.show_title, show.show_year)
        ext = posixpath.splitext(name)[1]
        season = _season_of(dirs)
        ep = parse_episode(name, dirs)
        key = (lib, _norm(show.show_title))
        if any(d.strip().lower() in _EXTRA_DIRS for d in dirs) or _EXTRA_NAME.search(name):
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
