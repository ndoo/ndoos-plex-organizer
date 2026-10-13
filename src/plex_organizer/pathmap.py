from __future__ import annotations

from .config import PathMap


def _under(path: str, prefix: str) -> bool:
    prefix = prefix.rstrip("/")
    return path == prefix or path.startswith(prefix + "/")


class PathMapper:
    """Translates Plex-visible paths to local (this process) and Deluge-visible paths."""

    def __init__(self, maps: list[PathMap]):
        self.maps = sorted(maps, key=lambda m: len(m.plex), reverse=True)

    def _translate(self, path: str, attr: str) -> str:
        for m in self.maps:
            if _under(path, m.plex):
                target = getattr(m, attr) or m.plex
                return target.rstrip("/") + path[len(m.plex.rstrip("/")):]
        return path

    def to_local(self, plex_path: str) -> str:
        return self._translate(plex_path, "local")

    def to_deluge(self, plex_path: str) -> str:
        return self._translate(plex_path, "deluge")

    def local_to_plex(self, local_path: str) -> str:
        for m in sorted(self.maps, key=lambda m: len(m.local or m.plex), reverse=True):
            l = (m.local or m.plex).rstrip("/")
            if _under(local_path, l):
                return m.plex.rstrip("/") + local_path[len(l):]
        return local_path

    def deluge_to_plex(self, deluge_path: str) -> str:
        for m in sorted(self.maps, key=lambda m: len(m.deluge or m.plex), reverse=True):
            d = (m.deluge or m.plex).rstrip("/")
            if _under(deluge_path, d):
                return m.plex.rstrip("/") + deluge_path[len(d):]
        return deluge_path

    def deluge_to_local(self, deluge_path: str) -> str:
        return self.to_local(self.deluge_to_plex(deluge_path))
