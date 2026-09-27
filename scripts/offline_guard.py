"""A process-wide guard that fails loudly on any attempt to reach the network.

Offline measurements and comparisons over the owner's cached answers run inside
:func:`no_network`: an empty ``AUDD_API_TOKEN``, ``IDEA_ENGINE_SHAZAM=off`` for this process only,
and every socket connection or name lookup to anything but this machine raises
:class:`NetworkAttempt` (loopback stays open: asyncio's own self-pipe uses it on Windows).  Every
refused attempt is also counted in :data:`BLOCKED`, so a caller can prove the count was zero even
if some library caught the exception.
"""

from __future__ import annotations

import os
import socket
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

#: Every refused destination, in order, for the life of the process.
BLOCKED: list[str] = []
_LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost", "0.0.0.0", ""})


class NetworkAttempt(RuntimeError):
    """Raised by the guard: this run must never reach any network."""


def _host(address: Any) -> str:
    if isinstance(address, tuple) and address:
        return str(address[0])
    return str(address)


@contextmanager
def no_network() -> Iterator[list[str]]:
    """Refuse every non-loopback connection and lookup for the whole block.

    Yields THIS block's own list of refused attempts (it starts empty, whatever earlier blocks in
    the process refused); :data:`BLOCKED` keeps the process-wide record.
    """

    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_create = socket.create_connection
    original_lookup = socket.getaddrinfo

    attempts: list[str] = []

    def refuse(where: str) -> None:
        BLOCKED.append(where)
        attempts.append(where)
        raise NetworkAttempt(f"network access attempted during an offline run: {where}")

    def connect(self: socket.socket, address: Any) -> Any:
        if _host(address) not in _LOOPBACK:
            refuse(_host(address))
        return original_connect(self, address)

    def connect_ex(self: socket.socket, address: Any) -> Any:
        if _host(address) not in _LOOPBACK:
            refuse(_host(address))
        return original_connect_ex(self, address)

    def create_connection(address: Any, *args: Any, **kwargs: Any) -> Any:
        if _host(address) not in _LOOPBACK:
            refuse(_host(address))
        return original_create(address, *args, **kwargs)

    def getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
        if str(host or "") not in _LOOPBACK:
            refuse(str(host))
        return original_lookup(host, *args, **kwargs)

    saved_env = {name: os.environ.get(name) for name in ("AUDD_API_TOKEN", "IDEA_ENGINE_SHAZAM")}
    socket.socket.connect = connect  # type: ignore[method-assign]
    socket.socket.connect_ex = connect_ex  # type: ignore[method-assign]
    socket.create_connection = create_connection  # type: ignore[assignment]
    socket.getaddrinfo = getaddrinfo  # type: ignore[assignment]
    os.environ["AUDD_API_TOKEN"] = ""
    os.environ["IDEA_ENGINE_SHAZAM"] = "off"
    try:
        yield attempts
    finally:
        socket.socket.connect = original_connect  # type: ignore[method-assign]
        socket.socket.connect_ex = original_connect_ex  # type: ignore[method-assign]
        socket.create_connection = original_create  # type: ignore[assignment]
        socket.getaddrinfo = original_lookup  # type: ignore[assignment]
        for name, value in saved_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
