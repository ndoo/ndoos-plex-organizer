from __future__ import annotations

import logging
import socket
import struct

log = logging.getLogger(__name__)

FALLBACK_HOSTS = ("host.docker.internal",)


def _tcp_ok(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def gateway_ip(route_file: str = "/proc/net/route") -> str | None:
    """Default gateway of this container, which is the Docker host on a bridge network."""
    try:
        with open(route_file) as fh:
            next(fh)
            for line in fh:
                f = line.split()
                if f[1] == "00000000":
                    return socket.inet_ntoa(struct.pack("<L", int(f[2], 16)))
    except (OSError, StopIteration, ValueError, IndexError):
        pass
    return None


def candidates(host: str, gateway: str | None = None) -> list[str]:
    out = [host, *FALLBACK_HOSTS]
    gw = gateway if gateway is not None else gateway_ip()
    if gw:
        out.append(gw)
    out.append("127.0.0.1")
    return list(dict.fromkeys(out))


def pick_host(host: str, port: int, *, connect=_tcp_ok, gateway: str | None = None) -> str:
    """Return the first reachable host: the configured name (a container name on a shared network),
    then the Docker host (for Plex/Deluge running with host networking), then localhost."""
    for h in candidates(host, gateway):
        if connect(h, port):
            if h != host:
                log.info("%s:%s not reachable; using %s instead", host, port, h)
            return h
    return host
