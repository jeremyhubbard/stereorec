"""Labelled-drive detection and hotplug polling.

Resolves the STEREOREC-labelled USB drive via /dev/disk/by-label + /proc/mounts,
falling back to scanning mount_roots. A write-probe on every poll also catches a
stale/half-unmounted drive that still shows up on paper.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Callable, Optional

from stereorec.config import Config

logger = logging.getLogger(__name__)

_PROBE_FILENAME = ".stereorec_write_probe"


class UsbManager:
    def __init__(
        self,
        config: Config,
        on_mounted: Optional[Callable[[str], None]] = None,
        on_removed: Optional[Callable[[], None]] = None,
    ):
        self.config = config
        self._on_mounted = on_mounted
        self._on_removed = on_removed
        self.is_present = False
        self.mount_path: Optional[str] = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="usb-poller", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._poll_once()
            except Exception:
                logger.exception("USB poll iteration failed")
            self._stop.wait(self.config.usb_poll_interval_s)

    def _poll_once(self) -> None:
        candidate = self._resolve_mount()
        writable = candidate is not None and self._write_probe(candidate)

        if writable and not self.is_present:
            self.is_present = True
            self.mount_path = candidate
            logger.info("USB mounted at %s", candidate)
            if self._on_mounted:
                self._on_mounted(candidate)
        elif not writable and self.is_present:
            old_path = self.mount_path
            self.is_present = False
            self.mount_path = None
            logger.warning("USB removed (was at %s)", old_path)
            if self._on_removed:
                self._on_removed()
        elif writable and self.is_present and candidate != self.mount_path:
            logger.info("USB remounted at %s (was %s)", candidate, self.mount_path)
            self.mount_path = candidate

    def _resolve_mount(self) -> Optional[str]:
        return self._resolve_by_label() or self._scan_mount_roots()

    def _resolve_by_label(self) -> Optional[str]:
        link = os.path.join("/dev/disk/by-label", self.config.usb_label)
        try:
            device = os.path.realpath(link)
        except OSError:
            return None
        if not os.path.exists(device):
            return None
        try:
            with open("/proc/mounts", "r", encoding="utf-8") as fh:
                for line in fh:
                    parts = line.split()
                    if len(parts) >= 2:
                        try:
                            if os.path.realpath(parts[0]) == device:
                                return parts[1]
                        except OSError:
                            continue
        except OSError:
            return None
        return None

    def _scan_mount_roots(self) -> Optional[str]:
        label = self.config.usb_label
        for root in self.config.mount_roots:
            direct = os.path.join(root, label)
            if os.path.isdir(direct):
                return direct
            try:
                for entry in os.listdir(root):
                    candidate = os.path.join(root, entry, label)
                    if os.path.isdir(candidate):
                        return candidate
            except OSError:
                continue
        return None

    def _write_probe(self, path: str) -> bool:
        probe_path = os.path.join(path, _PROBE_FILENAME)
        try:
            with open(probe_path, "wb") as fh:
                fh.write(b"ok")
            os.unlink(probe_path)
            return True
        except OSError:
            return False
