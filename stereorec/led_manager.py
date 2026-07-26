"""NeoPixel status LEDs: pixel 0 mirrors the state machine, pixel 1 (if present)
mirrors the thermal zone. Disabled or unavailable hardware degrades to a
logged no-op everywhere -- a flaky strip must never affect recording.
"""

from __future__ import annotations

import logging
from typing import Optional

from stereorec.config import Config
from stereorec.states import State

logger = logging.getLogger(__name__)

try:
    import board
    import neopixel

    NEOPIXEL_AVAILABLE = True
except (ImportError, NotImplementedError):
    board = None  # type: ignore[assignment]
    neopixel = None  # type: ignore[assignment]
    NEOPIXEL_AVAILABLE = False

STATE_PIXEL_INDEX = 0
THERMAL_PIXEL_INDEX = 1


class LedManager:
    def __init__(self, config: Config):
        self.config = config
        self._strip = None

    def _active(self) -> bool:
        return self.config.led_enabled and NEOPIXEL_AVAILABLE

    def open(self) -> bool:
        if not self.config.led_enabled:
            return False
        if not NEOPIXEL_AVAILABLE:
            logger.warning("NeoPixel libraries not available -- LED status disabled")
            return False
        try:
            pin = getattr(board, f"D{self.config.led_gpio_pin}")
            order = getattr(neopixel, self.config.led_pixel_order)
            self._strip = neopixel.NeoPixel(
                pin,
                self.config.led_count,
                brightness=self.config.led_brightness,
                pixel_order=order,
                auto_write=False,
            )
            logger.info(
                "LED strip initialized: %d pixel(s) on GPIO%d",
                self.config.led_count,
                self.config.led_gpio_pin,
            )
            return True
        except Exception:
            logger.exception("Failed to initialize LED strip")
            self._strip = None
            return False

    def set_state(self, state: State) -> None:
        if self._strip is None:
            return
        color = self.config.led_state_colors.get(state.name, (0, 0, 0))
        self._set_pixel(STATE_PIXEL_INDEX, color)

    def set_thermal_zone(self, zone: str) -> None:
        if self._strip is None or self.config.led_count < 2:
            return
        color = self.config.led_thermal_colors.get(zone, (0, 0, 0))
        self._set_pixel(THERMAL_PIXEL_INDEX, color)

    def set_updating(self) -> None:
        """Show the update-in-progress color on the state pixel.

        Used by tools/check_for_update.py while stereorec.service is stopped
        for a stop -> pull -> restart cycle, so nothing else is driving pixel 0.
        """
        if self._strip is None:
            return
        self._set_pixel(STATE_PIXEL_INDEX, self.config.led_update_color)

    def _set_pixel(self, index: int, color) -> None:
        try:
            self._strip[index] = tuple(color)
            self._strip.show()
        except Exception:
            logger.exception("Failed to update LED pixel %d", index)

    def blank(self) -> None:
        if self._strip is None:
            return
        try:
            self._strip.fill((0, 0, 0))
            self._strip.show()
        except Exception:
            logger.exception("Failed to blank LED strip")

    def close(self) -> None:
        self.blank()
        self._strip = None
