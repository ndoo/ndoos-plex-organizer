from __future__ import annotations

import fnmatch
import logging
import re
import time

from plexapi.exceptions import NotFound

from .config import Config
from .model import MediaFile, PlexItem

log = logging.getLogger(__name__)


def library_excluded(cfg: Config, name: str, key: str) -> bool:
    ex = cfg.exclude.libraries
    if name in ex or str(key) in ex:
        return True
    inc = cfg.include_libraries
    return bool(inc) and name not in inc and str(key) not in inc


def title_excluded(cfg: Config, *titles: str | None) -> bool:
    return any(re.search(p, t, re.I) for p in cfg.exclude.titles for t in titles if t)


def path_excluded(cfg: Config, path: str) -> bool:
    return any(fnmatch.fnmatch(path, g) for g in cfg.exclude.paths)


def _root_for(path: str, roots: list[str]) -> str | None:
    best = None
    for r in roots:
        r2 = r.rstrip("/")
        if path.startswith(r2 + "/") and (best is None or len(r2) > len(best)):
            best = r2
    return best


def _hdr(media) -> str | None:
    try:
        for part in media.parts:
            for s in part.videoStreams():
                if getattr(s, "DOVIPresent", False):
                    return "DV"
                if getattr(s, "colorTrc", None) in ("smpte2084", "arib-std-b67"):
                    return "HDR"
    except Exception:  # noqa: BLE001 - stream data is optional
        pass
    return None


def _files(item, root: str | None, roots: list[str]) -> list[MediaFile]:
    out: list[MediaFile] = []
    for mi, media in enumerate(item.media):
        parts = list(media.parts)
        for pi, part in enumerate(parts):
            if not _root_for(part.file, roots):
                continue
            out.append(
                MediaFile(
                    path=part.file,
                    media_index=mi,
                    part_index=pi,
                    part_count=len(parts),
                    resolution=getattr(media, "videoResolution", None),
                    hdr=_hdr(media),
                )
            )
    return out


def _id_tag(item, cfg: Config) -> str | None:
    if not cfg.naming.id_tag_in_folder:
        return None
    try:
        for g in item.guids:
            scheme, _, value = g.id.partition("://")
            if scheme in ("tmdb", "tvdb", "imdb"):
                return f"{scheme}-{value}"
    except Exception:  # noqa: BLE001
        pass
    return None


def merge_episodes(items: list[PlexItem]) -> list[PlexItem]:
    """Plex lists a multi-episode file once per episode; fold them into one item."""
    merged: dict[tuple, PlexItem] = {}
    out: list[PlexItem] = []
    for it in items:
        if it.kind != "episode" or not it.files:
            out.append(it)
            continue
        key = (it.library, it.show_title, it.season, it.files[0].path)
        if key in merged:
            merged[key].episodes.extend(it.episodes)
        else:
            merged[key] = it
            out.append(it)
    return out


def _wait_for_scans(server, timeout: float = 900) -> None:
    deadline = time.monotonic() + timeout
    while any(s.refreshing for s in server.library.sections()) and time.monotonic() < deadline:
        time.sleep(5)


def load_items(cfg: Config, server=None) -> list[PlexItem]:
    if server is None:
        from plexapi.server import PlexServer

        server = PlexServer(cfg.plex_url, cfg.plex_token)
    _wait_for_scans(server)
    items: list[PlexItem] = []
    for section in server.library.sections():
        if section.type not in ("movie", "show"):
            continue
        if library_excluded(cfg, section.title, section.key):
            log.info("excluded library %s", section.title)
            continue
        roots = list(section.locations)
        if section.type == "movie":
            for m in section.all():
                if title_excluded(cfg, m.title):
                    continue
                files = _files(m, None, roots)
                if not files:
                    continue
                try:
                    edition = getattr(m, "editionTitle", None)
                except NotFound:
                    log.warning("item %s disappeared from Plex while reading; skipped", m.title)
                    continue
                items.append(
                    PlexItem(
                        library=section.title,
                        kind="movie",
                        root=_root_for(files[0].path, roots),
                        title=m.title,
                        year=m.year,
                        edition=edition,
                        id_tag=_id_tag(m, cfg),
                        files=files,
                    )
                )
        else:
            shows = {s.ratingKey: s for s in section.all()}
            for ep in section.searchEpisodes():
                show = shows.get(ep.grandparentRatingKey)
                if title_excluded(cfg, ep.grandparentTitle):
                    continue
                files = _files(ep, None, roots)
                if not files or ep.parentIndex is None or ep.index is None:
                    continue
                items.append(
                    PlexItem(
                        library=section.title,
                        kind="episode",
                        root=_root_for(files[0].path, roots),
                        title=ep.title or "",
                        show_title=ep.grandparentTitle,
                        show_year=getattr(show, "year", None),
                        season=ep.parentIndex,
                        episodes=[(ep.index, ep.title or "")],
                        id_tag=_id_tag(show, cfg) if show else None,
                        files=files,
                    )
                )
    return merge_episodes(items)
