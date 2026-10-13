from __future__ import annotations

import logging
import os
import posixpath
import re
import shutil
import subprocess
from dataclasses import dataclass, field

from .config import Config
from .delugeidx import DelugeIndex
from .model import PlexItem
from .pathmap import PathMapper

log = logging.getLogger(__name__)

_VOLUME = re.compile(r"\.(rar|r\d{2,3})$", re.I)
_PART = re.compile(r"\.part(\d+)\.rar$", re.I)
MIN_MATCH_BYTES = 1_000_000


@dataclass
class ArchiveSet:
    directory: str  # Plex path
    main: str  # first volume file name
    files: list[str] = field(default_factory=list)  # every volume + sfv
    contents: list[tuple[str, int]] = field(default_factory=list)  # (path in archive, size)
    action: str = ""  # delete-redundant | extract | skip
    reason: str = ""


def find_sets(local_dir: str, names: list[str]) -> list[ArchiveSet]:
    vols = [n for n in names if _VOLUME.search(n)]
    mains = [n for n in vols if n.lower().endswith(".rar") and not (_PART.search(n) and int(_PART.search(n).group(1)) > 1)]
    sets = []
    for main in mains:
        stem = re.sub(r"\.part\d+\.rar$|\.rar$", "", main, flags=re.I)
        members = [n for n in names if n.startswith(stem + ".") and (_VOLUME.search(n) or n.lower().endswith(".sfv"))]
        sets.append(ArchiveSet(local_dir, main, sorted(set(members + [main]))))
    return sets


def parse_listing(text: str) -> list[tuple[str, int]]:
    """Parse `7z l -slt` output into (path, size) for files."""
    out, cur = [], {}
    for line in text.splitlines() + [""]:
        if not line.strip():
            if cur.get("Path") and cur.get("Folder") != "+" and "Size" in cur and cur.get("Type") is None:
                out.append((cur["Path"], int(cur["Size"] or 0)))
            cur = {}
            continue
        k, sep, v = line.partition(" = ")
        if sep:
            cur[k.strip()] = v.strip()
    return out


def list_archive(path: str) -> list[tuple[str, int]]:
    res = subprocess.run(["7z", "l", "-slt", "-ba", path], capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip() or res.stdout.strip()[-200:])
    return parse_listing(res.stdout)


def size_index(items: list[PlexItem], mapper: PathMapper, cfg: Config) -> dict[int, list[str]]:
    index: dict[int, list[str]] = {}
    for root in sorted({it.root for it in items}):
        for d, _, names in os.walk(mapper.to_local(root)):
            for n in names:
                if posixpath.splitext(n)[1].lower() in cfg.naming.video_exts and not _VOLUME.search(n):
                    p = os.path.join(d, n)
                    try:
                        index.setdefault(os.path.getsize(p), []).append(p)
                    except OSError:
                        pass
    return index


def plan(cfg: Config, items: list[PlexItem], mapper: PathMapper, deluge: DelugeIndex | None,
         lister=list_archive) -> list[ArchiveSet]:
    sizes = size_index(items, mapper, cfg)
    out: list[ArchiveSet] = []
    for root in sorted({it.root for it in items}):
        for d, _, names in os.walk(mapper.to_local(root)):
            for s in find_sets(d, names):
                main_path = os.path.join(d, s.main)
                if deluge is not None:
                    plex_main = posixpath.join(root, os.path.relpath(d, mapper.to_local(root)), s.main)
                    if deluge.lookup(mapper.to_deluge(plex_main)):
                        s.action, s.reason = "skip", "owned by a torrent"
                        out.append(s)
                        continue
                try:
                    s.contents = lister(main_path)
                except Exception as e:  # noqa: BLE001
                    s.action, s.reason = "skip", f"cannot list: {e}"
                    out.append(s)
                    continue
                big = [(p, sz) for p, sz in s.contents if sz >= MIN_MATCH_BYTES]
                if big and all(sz in sizes for _, sz in big):
                    s.action, s.reason = "delete-redundant", "same-size copy exists"
                elif not s.contents:
                    s.action, s.reason = "skip", "empty archive"
                else:
                    s.action = "extract"
                out.append(s)
    return out


def apply(sets: list[ArchiveSet], extract=None) -> tuple[int, int, list[str]]:
    extracted = deleted = 0
    problems: list[str] = []
    extract = extract or _extract
    for s in sets:
        if s.action not in ("extract", "delete-redundant"):
            continue
        if s.action == "extract":
            try:
                extract(os.path.join(s.directory, s.main), s.directory)
                bad = [p for p, sz in s.contents
                       if not os.path.exists(os.path.join(s.directory, p))
                       or (sz and os.path.getsize(os.path.join(s.directory, p)) != sz)]
            except Exception as e:  # noqa: BLE001
                _remove_empty_outputs(s)
                problems.append(f"{s.directory}/{s.main}: {e}")
                continue
            if bad:
                _remove_empty_outputs(s)
                problems.append(f"{s.directory}/{s.main}: extracted files missing or wrong size: {bad[:3]}")
                continue
            fix_vobsub(s)
            extracted += 1
        for f in s.files:
            try:
                os.remove(os.path.join(s.directory, f))
                deleted += 1
            except FileNotFoundError:
                pass
    return extracted, deleted, problems


def _remove_empty_outputs(s: ArchiveSet) -> None:
    """A failed extraction can leave zero-byte placeholders; remove the ones it created."""
    for p, _ in s.contents:
        full = os.path.join(s.directory, p)
        if os.path.isfile(full) and os.path.getsize(full) == 0:
            os.remove(full)


def fix_vobsub(s: ArchiveSet) -> None:
    """Scene releases hide a VobSub .sub file as '<name>.rar' next to '<name>.idx'; restore the name."""
    for p, _ in s.contents:
        full = os.path.join(s.directory, p)
        if p.lower().endswith(".rar") and os.path.isfile(full):
            stem = full[:-4]
            if os.path.exists(stem + ".idx"):
                with open(full, "rb") as fh:
                    if not fh.read(4).startswith(b"Rar!"):
                        os.replace(full, stem + ".sub")


def _extract(archive: str, dest: str) -> None:
    if shutil.which("unrar"):
        cmd = ["unrar", "x", "-y", "-o+", archive, dest.rstrip("/") + "/"]
    else:
        cmd = ["7z", "x", "-y", f"-o{dest}", archive]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError((res.stderr or res.stdout).strip()[-300:])
