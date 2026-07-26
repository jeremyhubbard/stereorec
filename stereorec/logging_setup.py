"""Rotating logs: a RAM/USB fallback handler plus a USB-attached handler.

Steady-state recording should never wear the SD card, so the fallback handler
defaults to a tmpfs directory (``/run/stereorec``) rather than the SD card, and
is detached entirely once the USB drive's own log handler is attached. Any
fallback lines written before the USB mounted are copied onto the USB drive
via :func:`flush_fallback_to_usb` so they aren't lost when the tmpfs clears on
the next reboot/power cycle.
"""

from __future__ import annotations

import glob
import logging
import logging.handlers
import os
import shutil
from typing import Optional

from stereorec.config import Config
from stereorec.util import ensure_dir

logger = logging.getLogger(__name__)

_fallback_handler: Optional[logging.Handler] = None
_usb_handler: Optional[logging.Handler] = None
_config: Optional[Config] = None


def _make_rotating_handler(path: str, config: Config) -> logging.Handler:
    ensure_dir(os.path.dirname(path))
    handler = logging.handlers.RotatingFileHandler(
        path, maxBytes=config.log_max_bytes, backupCount=config.log_backup_count
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s [%(threadName)s] %(name)s: %(message)s")
    )
    return handler


def _attach_fallback(config: Config) -> None:
    global _fallback_handler
    if config.disable_fallback_log or _fallback_handler is not None:
        return
    try:
        path = os.path.join(config.fallback_log_dir, config.log_filename)
        _fallback_handler = _make_rotating_handler(path, config)
        logging.getLogger().addHandler(_fallback_handler)
    except OSError as exc:
        logger.warning("Could not attach fallback log handler: %s", exc)
        _fallback_handler = None


def _detach_fallback() -> None:
    global _fallback_handler
    if _fallback_handler is None:
        return
    logging.getLogger().removeHandler(_fallback_handler)
    _fallback_handler.close()
    _fallback_handler = None


def init_logging(config: Config) -> None:
    global _config
    _config = config
    root = logging.getLogger()
    root.setLevel(getattr(logging, config.log_level.upper(), logging.INFO))

    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(console)

    _attach_fallback(config)


def attach_usb_log(session_dir: str) -> None:
    global _usb_handler
    if _config is None or _usb_handler is not None:
        return
    try:
        path = os.path.join(session_dir, "logs", _config.log_filename)
        _usb_handler = _make_rotating_handler(path, _config)
        logging.getLogger().addHandler(_usb_handler)
    except OSError as exc:
        logger.warning("Could not attach USB log handler: %s", exc)
        _usb_handler = None
        return

    flush_fallback_to_usb(session_dir)

    if _config.detach_fallback_when_usb_present:
        _detach_fallback()


def detach_usb_log() -> None:
    global _usb_handler
    if _usb_handler is not None:
        logging.getLogger().removeHandler(_usb_handler)
        _usb_handler.close()
        _usb_handler = None
    if _config is not None and _config.detach_fallback_when_usb_present:
        _attach_fallback(_config)


def flush_fallback_to_usb(session_dir: str) -> None:
    """Copy any accumulated tmpfs fallback log files onto the USB session dir."""
    if _config is None or _config.disable_fallback_log:
        return
    fallback_dir = _config.fallback_log_dir
    base = _config.log_filename
    pattern = os.path.join(fallback_dir, base + "*")
    matches = glob.glob(pattern)
    if not matches:
        return
    dest_dir = os.path.join(session_dir, "logs")
    try:
        ensure_dir(dest_dir)
        for src in matches:
            name = "stereorec.fallback" + os.path.basename(src)[len(base):]
            dest = os.path.join(dest_dir, name)
            shutil.copyfile(src, dest)
    except OSError as exc:
        logger.warning("Could not flush fallback logs to USB: %s", exc)
