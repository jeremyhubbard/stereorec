"""CPU temperature monitor: drives the thermal status LED and, in the danger
zone, flags the orchestrator to safely stop recording before a thermal-throttle
event can risk corrupting the encode.
"""

from __future__ import annotations

import logging
import threading
from typing import Callable, Optional

from stereorec.config import Config
from stereorec.util import read_cpu_temp_c

logger = logging.getLogger(__name__)

ZONE_NORMAL = "normal"
ZONE_WARNING = "warning"
ZONE_DANGER = "danger"


class ThermalManager:
    def __init__(self, config: Config, on_zone_change: Optional[Callable[[str], None]] = None):
        self.config = config
        self._on_zone_change = on_zone_change
        self.zone = ZONE_NORMAL
        self._danger_pending = False
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="thermal-monitor", daemon=True)
        self._warned_unavailable = False

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)

    def pop_danger_pending(self) -> bool:
        """Atomically read-and-clear the danger-zone flag, like the other fault flags."""
        with self._lock:
            pending = self._danger_pending
            self._danger_pending = False
            return pending

    def _run(self) -> None:
        while not self._stop.is_set():
            temp = read_cpu_temp_c()
            if temp is None:
                if not self._warned_unavailable:
                    logger.warning("No readable thermal zone -- thermal monitoring disabled")
                    self._warned_unavailable = True
            else:
                self._update_zone(temp)
            self._stop.wait(self.config.temp_poll_interval_s)

    def _update_zone(self, temp: float) -> None:
        new_zone = self.zone
        if self.zone == ZONE_DANGER:
            if temp < self.config.temp_danger_c - self.config.temp_recovery_hysteresis_c:
                new_zone = ZONE_WARNING if temp >= self.config.temp_warning_c else ZONE_NORMAL
        elif temp >= self.config.temp_danger_c:
            new_zone = ZONE_DANGER
        elif temp >= self.config.temp_warning_c:
            new_zone = ZONE_WARNING
        else:
            new_zone = ZONE_NORMAL

        if new_zone != self.zone:
            logger.info("Thermal zone %s -> %s (%.1fC)", self.zone, new_zone, temp)
            self.zone = new_zone
            if new_zone == ZONE_DANGER:
                with self._lock:
                    self._danger_pending = True
            if self._on_zone_change:
                self._on_zone_change(new_zone)
