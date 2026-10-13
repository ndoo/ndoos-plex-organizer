from __future__ import annotations

import posixpath
import time
from dataclasses import dataclass, field
from typing import Protocol

BUSY_STATES = {"Checking", "Moving", "Error", "Allocating"}
FIELDS = ["name", "save_path", "state", "progress", "label", "files"]


class DelugeApi(Protocol):
    def torrents_status(self) -> dict[str, dict]: ...
    def torrent_status(self, tid: str) -> dict: ...
    def move_storage(self, tid: str, dest: str) -> None: ...
    def rename_files(self, tid: str, renames: list[tuple[int, str]]) -> None: ...
    def deselect_files(self, tid: str, indices: list[int]) -> None: ...


class DelugeRpc:
    """Thin wrapper over deluge-client, exposing only what the organiser needs."""

    def __init__(self, host: str, port: int, username: str, password: str):
        from deluge_client import DelugeRPCClient

        from .net import pick_host

        self.client = DelugeRPCClient(pick_host(host, port), port, username, password, decode_utf8=True)
        self.client.connect()

    def torrents_status(self) -> dict[str, dict]:
        return self.client.call("core.get_torrents_status", {}, FIELDS)

    def torrent_status(self, tid: str) -> dict:
        return self.client.call("core.get_torrent_status", tid, FIELDS)

    def move_storage(self, tid: str, dest: str) -> None:
        self.client.call("core.move_storage", [tid], dest)

    def rename_files(self, tid: str, renames: list[tuple[int, str]]) -> None:
        self.client.call("core.rename_files", tid, [[i, p] for i, p in renames])


    def deselect_files(self, tid: str, indices: list[int]) -> None:
        prios = list(self.client.call("core.get_torrent_status", tid, ["file_priorities"])["file_priorities"])
        for i in indices:
            prios[i] = 0
        self.client.call("core.set_torrent_options", [tid], {"file_priorities": prios})


@dataclass
class Torrent:
    id: str
    name: str
    save_path: str
    state: str
    progress: float
    label: str
    files: list[tuple[int, str]] = field(default_factory=list)  # (index, path relative to save_path)

    def abs_path(self, rel: str) -> str:
        return posixpath.join(self.save_path, rel)

    @property
    def busy(self) -> bool:
        return self.state in BUSY_STATES

    @property
    def complete(self) -> bool:
        return self.progress >= 100


class DelugeIndex:
    def __init__(self, torrents: list[Torrent]):
        self.torrents = {t.id: t for t in torrents}
        self.by_path: dict[str, tuple[str, int]] = {}
        for t in torrents:
            for idx, rel in t.files:
                self.by_path[t.abs_path(rel)] = (t.id, idx)

    @classmethod
    def from_api(cls, api: DelugeApi) -> "DelugeIndex":
        out = []
        for tid, st in api.torrents_status().items():
            out.append(_torrent(tid, st))
        return cls(out)

    def lookup(self, deluge_path: str) -> tuple[str, int] | None:
        return self.by_path.get(deluge_path)


def _torrent(tid: str, st: dict) -> Torrent:
    return Torrent(
        id=tid,
        name=st.get("name", ""),
        save_path=st.get("save_path", "").rstrip("/"),
        state=st.get("state", ""),
        progress=float(st.get("progress", 0)),
        label=st.get("label", "") or "",
        files=[(f["index"], f["path"]) for f in st.get("files", [])],
    )


def wait_for(api: DelugeApi, tid: str, predicate, timeout: float = 600, interval: float = 1.0) -> Torrent:
    deadline = time.monotonic() + timeout
    while True:
        t = _torrent(tid, api.torrent_status(tid))
        if t.state != "Moving" and predicate(t):
            return t
        if time.monotonic() > deadline:
            raise TimeoutError(f"torrent {tid} did not reach expected state (state={t.state})")
        time.sleep(interval)
