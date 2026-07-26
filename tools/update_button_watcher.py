#!/usr/bin/env python3
"""update_button_watcher.py -- GPIO button that triggers an on-demand update check.

Optional hardware: wire a momentary button between the configured GPIO pin
(default GPIO17 / physical pin 11 -- free on this build: GPIO3 is the shutdown
button, GPIO18 is the NeoPixel data line) and GND.

Runs as its own long-lived systemd service
(systemd/stereorec-update-button.service) since it just watches a pin for the
life of the process. It never touches stereorec directly -- only ever through
`systemctl start stereorec-update.service`, the same oneshot unit the update
timer uses. Pressing the button while an update is already running is
harmless: systemd treats `start` on an already-active/-activating oneshot
unit as a no-op, so this can't spawn overlapping update runs.

Configure a different pin via STEREOREC_UPDATE_BUTTON_PIN if GPIO17 is needed
for something else.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading

logger = logging.getLogger("update_button_watcher")

DEFAULT_PIN = 17
UPDATE_SERVICE = "stereorec-update.service"
DEBOUNCE_S = 0.2


def trigger_update_check() -> None:
    logger.info("Update button pressed -- triggering %s", UPDATE_SERVICE)
    try:
        subprocess.run(["systemctl", "start", UPDATE_SERVICE], timeout=10)
    except (subprocess.SubprocessError, OSError) as exc:
        logger.error("Failed to start %s: %s", UPDATE_SERVICE, exc)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    try:
        from gpiozero import Button
    except ImportError:
        logger.error("gpiozero not available -- update button watcher cannot run")
        return 1

    pin = int(os.environ.get("STEREOREC_UPDATE_BUTTON_PIN", DEFAULT_PIN))
    try:
        button = Button(pin, pull_up=True, bounce_time=DEBOUNCE_S)
    except Exception:
        logger.exception("Failed to initialize button on GPIO%d", pin)
        return 1

    button.when_pressed = trigger_update_check
    logger.info("Watching GPIO%d for update-check button presses", pin)

    threading.Event().wait()  # block forever; systemd stops us via SIGTERM
    return 0


if __name__ == "__main__":
    sys.exit(main())
