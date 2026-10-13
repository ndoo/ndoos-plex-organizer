from __future__ import annotations

import os
import posixpath
from dataclasses import dataclass, field

from .apply import Journal, prune_empty_dirs
from .delugeidx import DelugeApi, DelugeIndex, wait_for
from .pathmap import PathMapper


@dataclass
class Move:
    src: str  # Plex path of a file or directory
    dest: str  # Plex path it will have afterwards
    torrents: list[str] = field(default_factory=list)
    reason: str = ""


def _files_under(local: str, plex: str) -> list[str]:
    if os.path.isfile(local):
        return [plex]
    out = []
    for d, _, names in os.walk(local):
        rel = os.path.relpath(d, local)
        out += [posixpath.join(plex, rel, n) if rel != "." else posixpath.join(plex, n) for n in names]
    return out


def plan_moves(units: list[str], dest_dir: str, mapper: PathMapper, index: DelugeIndex | None) -> list[Move]:
    moves = []
    for src in units:
        m = Move(src, posixpath.join(dest_dir, posixpath.basename(src)))
        local = mapper.to_local(src)
        if not os.path.exists(local):
            m.reason = "source missing"
        elif os.path.exists(mapper.to_local(m.dest)):
            m.reason = "destination exists"
        else:
            src_d = mapper.to_deluge(src).rstrip("/")
            owners = set()
            for f in _files_under(local, src):
                hit = index.lookup(mapper.to_deluge(f)) if index else None
                if hit:
                    owners.add(hit[0])
            for tid in sorted(owners):
                t = index.torrents[tid]
                inside = all(t.abs_path(rel) == src_d or t.abs_path(rel).startswith(src_d + "/") for _, rel in t.files)
                if t.busy:
                    m.reason = f"torrent {tid[:8]} busy"
                elif not inside:
                    m.reason = f"torrent {tid[:8]} has files outside {src}"
                elif posixpath.dirname(src_d) != t.save_path:
                    m.reason = f"torrent {tid[:8]} is nested below its save path"
            m.torrents = sorted(owners)
        moves.append(m)
    return moves


def apply_moves(moves: list[Move], mapper: PathMapper, index: DelugeIndex | None, api: DelugeApi | None,
                journal: Journal) -> int:
    done = 0
    for m in moves:
        if m.reason:
            continue
        dest_parent = posixpath.dirname(m.dest)
        os.makedirs(mapper.to_local(dest_parent), exist_ok=True)
        new_sp = mapper.to_deluge(dest_parent)
        for tid in m.torrents:
            t = index.torrents[tid]
            journal.write({"type": "torrent", "id": tid, "old_save_path": t.save_path, "new_save_path": new_sp,
                           "files": [[i, rel, rel] for i, rel in t.files]})
            api.move_storage(tid, new_sp)
            wait_for(api, tid, lambda x: x.save_path == new_sp)
        src_local, dst_local = mapper.to_local(m.src), mapper.to_local(m.dest)
        if os.path.exists(src_local):  # whatever no torrent owned
            if not m.torrents:
                journal.write({"type": "fs", "src": src_local, "dst": dst_local})
                os.rename(src_local, dst_local)
            else:
                for f in _files_under(src_local, m.src):
                    s = mapper.to_local(f)
                    d = mapper.to_local(posixpath.join(m.dest, posixpath.relpath(f, m.src)))
                    os.makedirs(posixpath.dirname(d), exist_ok=True)
                    journal.write({"type": "fs", "src": s, "dst": d})
                    os.rename(s, d)
        done += 1
    prune_empty_dirs([posixpath.dirname(m.src) for m in moves], mapper)
    return done
