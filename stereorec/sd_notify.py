"""Minimal systemd notify-socket client: readiness + watchdog pings.

This talks to systemd over a Unix domain socket local to this machine (the
address comes from the ``NOTIFY_SOCKET`` environment variable systemd sets on
the service process) -- it is not network I/O, and every call is a no-op when
that variable isn't set (e.g. not running under systemd at all).
"""

from __future__ import annotations

import logging
import os
import socket
from typing import Optional

logger = logging.getLogger(__name__)


def _socket_address() -> Optional[str]:
    address = os.environ.get("NOTIFY_SOCKET")
    if not address:
        return None
    return address


def notify(message: str) -> bool:
    address = _socket_address()
    if not address:
        return False
    if address.startswith("@"):
        address = "\0" + address[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.sendto(message.encode("utf-8"), address)
        return True
    except OSError as exc:
        logger.warning("sd_notify send failed: %s", exc)
        return False


def ready() -> bool:
    return notify("READY=1")


def watchdog_ping() -> bool:
    return notify("WATCHDOG=1")


def watchdog_interval_s() -> Optional[float]:
    """Half of WATCHDOG_USEC (systemd's own recommended ping cadence), or None."""
    raw = os.environ.get("WATCHDOG_USEC")
    if not raw:
        return None
    try:
        microseconds = int(raw)
    except ValueError:
        return None
    if microseconds <= 0:
        return None
    return (microseconds / 1_000_000.0) / 2.0
