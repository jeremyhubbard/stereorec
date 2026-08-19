#!/usr/bin/env python3
"""check_service_health.py -- notice, repeatedly, when stereorec.service is down.

Run every few minutes by systemd/stereorec-health-check.timer. Restart=always
plus stereorec.service's OnFailure= diagnostics (see log_failure_report.py)
handle and explain most unexpected stops, but neither one keeps reminding
anyone if the service is still down 20 minutes later: OnFailure= fires once,
and a StartLimitBurst exhaustion (Result=start-limit-hit) means systemd has
stopped even trying to restart it. This script is the "LEDs are dark and
staying dark" field symptom made loud and repeating in the logs -- purely
detection, not a fix. See README_pi4.md's "Diagnosing watchdog/crash
restarts" section.

Usage: check_service_health.py   (no arguments)
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import shutil
import subprocess
import sys

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_DIR)

from stereorec.config import Config  # noqa: E402
from stereorec.usb_manager import resolve_mount  # noqa: E402
from stereorec.util import ensure_dir  # noqa: E402

logger = logging.getLogger("check_service_health")

LOG_FILENAME = "stereorec-health.log"
HEALTH_LOG_SUBDIR = "health_logs"
SERVICE_NAME = "stereorec"
SHOW_PROPS = "ActiveState,SubState,Result,InactiveEnterTimestamp,NRestarts"


def setup_logging(config: Config) -> None:
    root = logging.getLogger()
    root.setLevel(getattr(logging, config.log_level.upper(), logging.INFO))
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)

    try:
        ensure_dir(config.fallback_log_dir)
        path = os.path.join(config.fallback_log_dir, LOG_FILENAME)
        handler = logging.handlers.RotatingFileHandler(
            path, maxBytes=config.log_max_bytes, backupCount=config.log_backup_count
        )
        handler.setFormatter(fmt)
        root.addHandler(handler)
    except OSError as exc:
        logger.warning("Could not attach RAM log handler: %s", exc)


def flush_log_to_usb(config: Config) -> None:
    mount = resolve_mount(config)
    if not mount:
        return
    src = os.path.join(config.fallback_log_dir, LOG_FILENAME)
    if not os.path.isfile(src):
        return
    dest_dir = os.path.join(mount, "STEREOREC", HEALTH_LOG_SUBDIR)
    try:
        ensure_dir(dest_dir)
        shutil.copyfile(src, os.path.join(dest_dir, LOG_FILENAME))
    except OSError as exc:
        logger.warning("Could not copy health log to USB: %s", exc)


def _systemctl_show(props: str) -> dict:
    try:
        result = subprocess.run(
            ["systemctl", "show", SERVICE_NAME, "-p", props],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        logger.warning("systemctl show failed: %s", exc)
        return {}
    out = {}
    for line in result.stdout.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            out[key] = value
    return out


def main() -> int:
    config = Config.load()
    setup_logging(config)

    props = _systemctl_show(SHOW_PROPS)
    active_state = props.get("ActiveState", "unknown")
    sub_state = props.get("SubState", "unknown")
    result = props.get("Result", "unknown")

    if active_state == "active":
        # INFO, not DEBUG: a silent log here would make an empty
        # stereorec-health.log ambiguous between "this checker never ran" and
        # "ran every few minutes and stereorec was fine every time" -- exactly
        # the ambiguity check_for_update.py had for its own no-op path.
        logger.info("stereorec.service is active (sub_state=%s)", sub_state)
        flush_log_to_usb(config)
        return 0

    since = props.get("InactiveEnterTimestamp") or "unknown"
    nrestarts = props.get("NRestarts", "unknown")

    if result == "start-limit-hit":
        logger.critical(
            "stereorec.service is DOWN and will NOT auto-restart: Result=start-limit-hit "
            "(systemd gave up after too many failures in a short window). Inactive since %s, "
            "NRestarts=%s. Needs `systemctl reset-failed %s && systemctl start %s` (or a "
            "reboot) to recover.",
            since,
            nrestarts,
            SERVICE_NAME,
            SERVICE_NAME,
        )
    else:
        logger.warning(
            "stereorec.service is not active (ActiveState=%s SubState=%s Result=%s "
            "NRestarts=%s) since %s",
            active_state,
            sub_state,
            result,
            nrestarts,
            since,
        )

    flush_log_to_usb(config)
    return 1


if __name__ == "__main__":
    sys.exit(main())
