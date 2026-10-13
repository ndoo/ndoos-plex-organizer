from __future__ import annotations

import json
import logging
import os
import posixpath
import re
import time
from collections import defaultdict
from dataclasses import dataclass

from .config import Config
from .delugeidx import DelugeApi, DelugeIndex, wait_for
from .model import Op
from .pathmap import PathMapper

log = logging.getLogger(__name__)


@dataclass
class Result:
    moved: int = 0
    torrents: int = 0
    failed: int = 0


class Journal:
    def __init__(self, path: str):
        self.path = path

    def write(self, rec: dict) -> None:
        rec["ts"] = time.time()
        with open(self.path, "a") as fh:
            fh.write(json.dumps(rec) + "\n")

    def read(self) -> list[dict]:
        if not os.path.exists(self.path):
            return []
        with open(self.path) as fh:
            return [json.loads(line) for line in fh if line.strip()]


def _prune_empty(local_dir: str, stop_at: str) -> None:
    stop = stop_at.rstrip("/")
    d = local_dir.rstrip("/")
    while d.startswith(stop + "/"):
        try:
            os.rmdir(d)
        except OSError:
            return
        d = posixpath.dirname(d)


def _apply_fs(op: Op, mapper: PathMapper, journal: Journal) -> None:
    src, dst = mapper.to_local(op.src), mapper.to_local(op.dst)
    if os.path.exists(dst):
        raise FileExistsError(dst)
    os.makedirs(posixpath.dirname(dst), exist_ok=True)
    journal.write({"type": "fs", "src": src, "dst": dst})
    os.rename(src, dst)
    _prune_empty(posixpath.dirname(src), mapper.to_local(op.root))


def _torrent_targets(cfg: Config, mapper: PathMapper, index: DelugeIndex, tid: str, tops: list[Op], root_d: str):
    t = index.torrents[tid]
    by_src = {o.src: o for o in tops}
    out = []
    for idx, rel in t.files:
        op = by_src.get(mapper.deluge_to_plex(t.abs_path(rel)))
        if op is None:
            target = posixpath.join(cfg.naming.leftovers_dir, rel)
        else:
            target = posixpath.relpath(mapper.to_deluge(op.dst), root_d)
        out.append((idx, rel, target))
    return t, out


def _apply_torrent(
    cfg: Config, mapper: PathMapper, index: DelugeIndex, api: DelugeApi, journal: Journal, tid: str, tops: list[Op]
) -> None:
    root_d = mapper.to_deluge(tops[0].root).rstrip("/")
    t, files = _torrent_targets(cfg, mapper, index, tid, tops, root_d)
    old_sp = t.save_path
    if old_sp != root_d:
        for _, rel, _ in files:
            if os.path.exists(mapper.deluge_to_local(posixpath.join(root_d, rel))):
                raise FileExistsError(f"{tid}: {posixpath.join(root_d, rel)} exists; refusing move_storage")
    journal.write(
        {"type": "torrent", "id": tid, "old_save_path": old_sp, "new_save_path": root_d,
         "files": [[i, rel, tgt] for i, rel, tgt in files]}
    )
    if old_sp != root_d:
        api.move_storage(tid, root_d)
        wait_for(api, tid, lambda x: x.save_path == root_d)
    renames = [(i, tgt) for i, rel, tgt in files if rel != tgt]
    if renames:
        api.rename_files(tid, renames)
        want = {i: tgt for i, _, tgt in files}
        wait_for(api, tid, lambda x: all(want.get(i) == p for i, p in x.files))
    for _, rel, _ in files:
        _prune_empty(posixpath.dirname(mapper.deluge_to_local(posixpath.join(old_sp, rel))), mapper.to_local(tops[0].root))
        _prune_empty(posixpath.dirname(mapper.deluge_to_local(posixpath.join(root_d, rel))), mapper.to_local(tops[0].root))


def apply_plan(
    cfg: Config,
    ops: list[Op],
    mapper: PathMapper,
    index: DelugeIndex | None,
    api: DelugeApi | None,
    journal: Journal,
    *,
    limit: int | None = None,
) -> Result:
    res = Result()
    free = [o for o in ops if o.action == "move" and o.owner == "free"]
    groups: dict[str, list[Op]] = defaultdict(list)
    for o in ops:
        if o.owner != "free":
            groups[o.owner].append(o)
    owned = {tid: g for tid, g in groups.items() if any(o.action == "move" for o in g)}
    if (owned and (index is None or api is None)):
        raise RuntimeError("torrent-owned files in plan but no Deluge connection")

    done = 0
    for tid, tops in owned.items():
        if limit is not None and done >= limit:
            return res
        try:
            _apply_torrent(cfg, mapper, index, api, journal, tid, tops)
            res.torrents += 1
            res.moved += sum(1 for o in tops if o.action == "move")
        except Exception:  # noqa: BLE001 - keep going, report
            log.exception("torrent %s failed", tid)
            res.failed += 1
        done += 1
    for op in free:
        if limit is not None and done >= limit:
            break
        try:
            _apply_fs(op, mapper, journal)
            res.moved += 1
        except Exception:  # noqa: BLE001
            log.exception("move failed: %s", op.src)
            res.failed += 1
        done += 1
    return res


def undo(journal: Journal, api: DelugeApi | None, mapper: PathMapper | None = None, match: str | None = None) -> int:
    n = 0
    for rec in reversed(journal.read()):
        if match and not re.search(match, rec.get("src", "") if rec["type"] == "fs" else " ".join(r[1] for r in rec["files"])):
            continue
        if rec["type"] == "fs":
            if os.path.exists(rec["dst"]) and not os.path.exists(rec["src"]):
                os.makedirs(posixpath.dirname(rec["src"]), exist_ok=True)
                os.rename(rec["dst"], rec["src"])
                n += 1
        elif rec["type"] == "torrent":
            if api is None:
                raise RuntimeError("journal has torrent records but no Deluge connection")
            tid = rec["id"]
            back = [(i, rel) for i, rel, tgt in rec["files"] if rel != tgt]
            if back:
                api.rename_files(tid, back)
                want = {i: rel for i, rel, _ in rec["files"]}
                wait_for(api, tid, lambda x: all(want.get(i) == p for i, p in x.files))
            if rec["old_save_path"] != rec["new_save_path"]:
                api.move_storage(tid, rec["old_save_path"])
                wait_for(api, tid, lambda x: x.save_path == rec["old_save_path"])
            n += 1
    return n


JUNK = {".DS_Store", "Thumbs.db", "desktop.ini"}


def prune_empty_dirs(roots: list[str], mapper: PathMapper) -> int:
    """Remove directories left empty (or holding only OS junk) under each library root."""
    n = 0
    for root in sorted(set(roots)):
        local_root = mapper.to_local(root).rstrip("/")
        for d, _, files in os.walk(local_root, topdown=False):
            if d == local_root:
                continue
            try:
                if files and not set(files) <= JUNK:
                    continue
                for f in files:
                    os.remove(os.path.join(d, f))
                os.rmdir(d)
                n += 1
            except OSError:
                pass
    return n
