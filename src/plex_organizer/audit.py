from __future__ import annotations

import difflib
import posixpath
import re
from collections import defaultdict
from dataclasses import dataclass, field

from .model import PlexItem

_SXE = re.compile(r"[sS](\d{1,2})[ ._-]?[eE](\d{1,3})(?:[eE-]+(\d{1,3}))?")
_QUALITY = re.compile(
    r"^(2160p|1080[pi]|720p|576p|480p|4k|uhd|web|webrip|web-dl|bluray|blu-ray|bdrip|hdtv|dvdrip|dvd|remux|repack|proper|"
    r"extended|amzn|nf|hdr|hdr10|dv|x264|x265|h264|h\.264|hevc|avc|aac|ac3|dts|ddp?\d?|atmos|internal|ws)$",
    re.I,
)
_STOP = {"the", "and", "a", "of", "to", "in", "s", "new"}


def _tokens(text: str) -> list[str]:
    text = text.lower().replace("&", " and ").replace("’", "").replace("'", "")
    return [t for t in re.findall(r"[a-z0-9]+", re.sub(r"\(\d{4}\)", " ", text)) if t not in _STOP]


@dataclass
class Parsed:
    series: str
    season: int
    episodes: list[int]
    title: str


def parse_filename(name: str) -> Parsed | None:
    stem = posixpath.splitext(name)[0]
    m = _SXE.search(stem)
    if not m:
        return None
    eps = [int(m.group(2))] + ([int(m.group(3))] if m.group(3) else [])
    rest = [t for t in re.split(r"[.\s_]+", stem[m.end():]) if t]
    title = []
    for t in rest:
        if _QUALITY.match(t) or t.startswith("["):
            break
        title.append(t)
    return Parsed(re.sub(r"[.\s_]+", " ", stem[: m.start()]).strip(" -"), int(m.group(1)), eps, " ".join(title))


@dataclass
class Finding:
    library: str
    show: str
    kind: str
    count: int = 0
    examples: list[str] = field(default_factory=list)


def audit(items: list[PlexItem]) -> list[Finding]:
    found: dict[tuple, Finding] = {}
    series_seen: dict[tuple, set[str]] = defaultdict(set)

    def hit(item: PlexItem, kind: str, example: str) -> None:
        f = found.setdefault((item.library, item.show_title, kind), Finding(item.library, item.show_title or "", kind))
        f.count += 1
        if len(f.examples) < 3:
            f.examples.append(example)

    for it in items:
        if it.kind != "episode":
            continue
        base = posixpath.basename(it.files[0].path)
        p = parse_filename(base)
        if not p:
            continue
        shown = f"{base[:70]}  ->  s{it.season:02d}e{it.episodes[0][0]:02d} {it.episodes[0][1]}"
        want = set(_tokens(it.show_title or ""))
        have = set(_tokens(p.series))
        if want and have:
            series_seen[(it.library, it.show_title)].add(" ".join(sorted(have)))
            if len(want & have) / len(want) < 0.5 and len(want & have) / len(have) < 0.5:
                hit(it, "different series in filename", shown)
        if (p.season, p.episodes[0]) != (it.season, it.episodes[0][0]):
            hit(it, "season/episode number differs", shown)
        elif p.title and it.episodes[0][1]:
            a, b = " ".join(_tokens(p.title)), " ".join(_tokens(it.episodes[0][1]))
            if a and b and difflib.SequenceMatcher(None, a, b).ratio() < 0.5 and not (set(a.split()) & set(b.split())):
                hit(it, "episode title differs", shown)
    for (lib, show), names in series_seen.items():
        if len(names) > 1:
            distinct = sorted(names)[:3]
            found.setdefault((lib, show, "several series in one show"), Finding(lib, show, "several series in one show", len(names), distinct))
    return sorted(found.values(), key=lambda f: (f.library, f.show, f.kind))


def render(findings: list[Finding]) -> str:
    out = []
    for f in findings:
        out.append(f"[{f.kind}] {f.library} :: {f.show} ({f.count})")
        out += [f"    {e}" for e in f.examples]
    return "\n".join(out)
