from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

DEFAULT_VIDEO_EXTS = (".mkv", ".mp4", ".m4v", ".avi", ".mov", ".wmv", ".ts", ".m2ts", ".mpg", ".mpeg", ".webm")
DISC_DIRS = {"BDMV", "VIDEO_TS", "AUDIO_TS", "CERTIFICATE", "HVDVD_TS", "ADV_OBJ"}
DEFAULT_SIDECAR_EXTS = (".srt", ".ass", ".ssa", ".sub", ".idx", ".sup", ".vtt", ".nfo", ".jpg", ".png")


@dataclass
class PathMap:
    plex: str
    local: str | None = None
    deluge: str | None = None


@dataclass
class DelugeConfig:
    host: str = "127.0.0.1"
    port: int = 58846
    username: str = "localclient"
    password: str = ""
    disabled: bool = False


@dataclass
class Excludes:
    libraries: list[str] = field(default_factory=list)
    paths: list[str] = field(default_factory=list)
    titles: list[str] = field(default_factory=list)
    torrents: list[str] = field(default_factory=list)
    torrent_labels: list[str] = field(default_factory=list)


@dataclass
class Naming:
    id_tag_in_folder: bool = False
    leftovers_dir: str = ".leftovers"
    leftover_globs: tuple[str, ...] = ("*sample*",)
    part_paths: tuple[str, ...] = ()
    video_exts: tuple[str, ...] = DEFAULT_VIDEO_EXTS
    sidecar_exts: tuple[str, ...] = DEFAULT_SIDECAR_EXTS


@dataclass
class Config:
    plex_url: str = "http://127.0.0.1:32400"
    plex_token: str = ""
    deluge: DelugeConfig | None = None
    path_maps: list[PathMap] = field(default_factory=list)
    exclude: Excludes = field(default_factory=Excludes)
    include_libraries: list[str] = field(default_factory=list)
    naming: Naming = field(default_factory=Naming)
    state_dir: str = "/config"
    settle_seconds: int = 600
    watch_interval: int = 900
    refresh_plex: bool = True


def _secret(env: str, value: str) -> str:
    path = os.environ.get(env + "_FILE")
    if path:
        return Path(path).read_text().strip()
    return os.environ.get(env, value)


def _plex_token_from_prefs(path: str) -> str:
    m = re.search(r'PlexOnlineToken="([^"]+)"', Path(path).read_text())
    if not m:
        raise ValueError(f"no PlexOnlineToken in {path}")
    return m.group(1)


def _deluge_password_from_auth(path: str, username: str) -> str:
    for line in Path(path).read_text().splitlines():
        parts = line.strip().split(":")
        if len(parts) >= 2 and parts[0] == username:
            return parts[1]
    raise ValueError(f"user {username!r} not found in {path}")


def load(path: str | os.PathLike | None) -> Config:
    raw: dict = {}
    if path:
        raw = yaml.safe_load(Path(path).read_text()) or {}
    plex = raw.get("plex", {})
    cfg = Config(
        plex_url=os.environ.get("PLEX_URL", plex.get("url", Config.plex_url)),
        plex_token=_secret("PLEX_TOKEN", plex.get("token", "")),
        path_maps=[PathMap(**m) for m in raw.get("path_maps", [])],
        include_libraries=list(raw.get("include", {}).get("libraries", [])),
        state_dir=os.environ.get("STATE_DIR", raw.get("state_dir", Config.state_dir)),
        settle_seconds=int(raw.get("settle_seconds", Config.settle_seconds)),
        watch_interval=int(raw.get("watch_interval", Config.watch_interval)),
        refresh_plex=bool(raw.get("refresh_plex", True)),
    )
    prefs = os.environ.get("PLEX_PREFERENCES_FILE", plex.get("preferences_file"))
    if not cfg.plex_token and prefs:
        cfg.plex_token = _plex_token_from_prefs(prefs)
    ex = raw.get("exclude", {})
    cfg.exclude = Excludes(**{k: list(ex.get(k, [])) for k in Excludes.__dataclass_fields__})
    nm = raw.get("naming", {})
    cfg.naming = Naming(
        id_tag_in_folder=bool(nm.get("id_tag_in_folder", False)),
        leftovers_dir=nm.get("leftovers_dir", Naming.leftovers_dir),
        leftover_globs=tuple(nm.get("leftover_globs", Naming.leftover_globs)),
        part_paths=tuple(nm.get("part_paths", ())),
        video_exts=tuple(nm.get("video_exts", DEFAULT_VIDEO_EXTS)),
        sidecar_exts=tuple(nm.get("sidecar_exts", DEFAULT_SIDECAR_EXTS)),
    )
    dl = raw.get("deluge")
    host = os.environ.get("DELUGE_HOST", (dl or {}).get("host"))
    if dl is not None or host:
        dl = dl or {}
        cfg.deluge = DelugeConfig(
            host=host or DelugeConfig.host,
            port=int(os.environ.get("DELUGE_PORT", dl.get("port", DelugeConfig.port))),
            username=os.environ.get("DELUGE_USERNAME", dl.get("username", DelugeConfig.username)),
            password=_secret("DELUGE_PASSWORD", dl.get("password", "")),
            disabled=bool(dl.get("disabled", False)),
        )
        auth = os.environ.get("DELUGE_AUTH_FILE", dl.get("auth_file"))
        if not cfg.deluge.password and auth:
            cfg.deluge.password = _deluge_password_from_auth(auth, cfg.deluge.username)
    return cfg
