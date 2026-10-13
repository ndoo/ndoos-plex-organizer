from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class MediaFile:
    path: str
    media_index: int = 0
    part_index: int = 0
    part_count: int = 1
    resolution: str | None = None
    hdr: str | None = None


@dataclass
class PlexItem:
    """One movie, or one episode file (multi-episode files carry several episodes)."""

    library: str
    kind: str  # "movie" | "episode"
    root: str  # Plex library location containing the files
    title: str
    year: int | None = None
    show_title: str | None = None
    show_year: int | None = None
    season: int | None = None
    episodes: list[tuple[int, str]] = field(default_factory=list)
    edition: str | None = None
    id_tag: str | None = None
    files: list[MediaFile] = field(default_factory=list)


@dataclass
class Op:
    """A single planned file move, expressed in Plex paths."""

    library: str
    root: str
    src: str
    dst: str
    title: str
    owner: str = "free"  # "free" | torrent id
    action: str = "move"  # move | skip
    reason: str = ""
    sidecar: bool = False

    @property
    def runnable(self) -> bool:
        return self.action == "move"
