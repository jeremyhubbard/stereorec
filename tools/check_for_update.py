#!/usr/bin/env python3
"""check_for_update.py -- fetch/pull a newer commit and restart stereorec.

Run as root (same as stereorec.service) by systemd/stereorec-update.timer (every
few minutes) or on demand via `systemctl start stereorec-update.service` / the
optional GPIO button watcher (tools/update_button_watcher.py).

Assumption this design leans on: Ethernet is only ever connected to this device
during development, never during an unattended field recording -- so it's safe
to treat "a newer commit exists" as "safe to stop recording, update, and
restart" without any live in-process pause/resume. See README_pi4.md's
"Auto-updating over Ethernet" section.

Flow:
  1. git fetch (short timeout) -- offline/unreachable just means nothing to do.
  2. Compare HEAD to the upstream ref; equal means already up to date.
  3. Otherwise: light the LED update color, stop stereorec.service, git pull
     --ff-only, pip install -r requirements.txt, python -m py_compile sanity
     check. On any failure, git reset --hard back to the pre-update commit.
     Either way, blank the LED and restart stereorec.service, then copy this
     run's log onto the USB drive if it's currently mounted.
"""

from __future__ import annotations

import glob
import json
import logging
import logging.handlers
import os
import shutil
import subprocess
import sys

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_DIR)

from stereorec.config import Config  # noqa: E402
from stereorec.led_manager import LedManager  # noqa: E402
from stereorec.usb_manager import resolve_mount  # noqa: E402
from stereorec.util import ensure_dir  # noqa: E402

logger = logging.getLogger("check_for_update")

LOG_FILENAME = "stereorec-update.log"
UPDATE_LOG_SUBDIR = "update_logs"
SERVICE_NAME = "stereorec"
FETCH_TIMEOUT_S = 15


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
    dest_dir = os.path.join(mount, "STEREOREC", UPDATE_LOG_SUBDIR)
    try:
        ensure_dir(dest_dir)
        shutil.copyfile(src, os.path.join(dest_dir, LOG_FILENAME))
    except OSError as exc:
        logger.warning("Could not copy update log to USB: %s", exc)


def run_git(args, timeout: float):
    return subprocess.run(
        ["git", "-C", REPO_DIR] + args, capture_output=True, text=True, timeout=timeout
    )


def git_fetch() -> bool:
    try:
        result = run_git(["fetch", "--quiet"], timeout=FETCH_TIMEOUT_S)
        return result.returncode == 0
    except (subprocess.SubprocessError, OSError) as exc:
        logger.debug("git fetch failed: %s", exc)
        return False


def git_rev_parse(ref: str):
    try:
        result = run_git(["rev-parse", ref], timeout=10)
    except (subprocess.SubprocessError, OSError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def git_pull_ff_only() -> bool:
    try:
        result = run_git(["pull", "--ff-only"], timeout=60)
    except (subprocess.SubprocessError, OSError) as exc:
        logger.error("git pull failed: %s", exc)
        return False
    if result.returncode != 0:
        logger.error("git pull --ff-only failed: %s", result.stderr.strip())
    return result.returncode == 0


def git_reset_hard(commit: str) -> bool:
    try:
        result = run_git(["reset", "--hard", commit], timeout=30)
    except (subprocess.SubprocessError, OSError) as exc:
        logger.error("git reset --hard failed: %s", exc)
        return False
    return result.returncode == 0


def _venv_bin(name: str) -> str:
    candidate = os.path.join(REPO_DIR, "venv", "bin", name)
    return candidate if os.path.exists(candidate) else name


def pip_install_requirements() -> bool:
    pip_bin = _venv_bin("pip")
    requirements = os.path.join(REPO_DIR, "requirements.txt")
    try:
        result = subprocess.run(
            [pip_bin, "install", "-r", requirements], capture_output=True, text=True, timeout=180
        )
    except (subprocess.SubprocessError, OSError) as exc:
        logger.error("pip install error: %s", exc)
        return False
    if result.returncode != 0:
        logger.error("pip install failed: %s", result.stderr.strip())
    return result.returncode == 0


def py_compile_check() -> bool:
    python_bin = _venv_bin("python")
    targets = glob.glob(os.path.join(REPO_DIR, "stereorec", "*.py")) + glob.glob(
        os.path.join(REPO_DIR, "tools", "*.py")
    )
    try:
        result = subprocess.run(
            [python_bin, "-m", "py_compile"] + targets, capture_output=True, text=True, timeout=60
        )
    except (subprocess.SubprocessError, OSError) as exc:
        logger.error("py_compile error: %s", exc)
        return False
    if result.returncode != 0:
        logger.error("py_compile failed: %s", result.stderr.strip())
    return result.returncode == 0


def _load_json_keys(path: str):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        logger.debug("Could not read %s for config-key check: %s", path, exc)
        return None
    return set(data.keys()) if isinstance(data, dict) else None


def check_new_config_keys() -> None:
    """Warn (never modify) if config.example.json gained fields config.json lacks."""
    example_keys = _load_json_keys(os.path.join(REPO_DIR, "config.example.json"))
    live_keys = _load_json_keys(os.path.join(REPO_DIR, "config.json"))
    if example_keys is None or live_keys is None:
        return
    new_keys = sorted(example_keys - live_keys)
    if new_keys:
        logger.warning(
            "config.example.json has new field(s) not in your config.json: %s -- "
            "review config.example.json and update config.json if you want them",
            ", ".join(new_keys),
        )


def systemctl(action: str) -> bool:
    try:
        result = subprocess.run(
            ["systemctl", action, SERVICE_NAME], capture_output=True, text=True, timeout=120
        )
    except (subprocess.SubprocessError, OSError) as exc:
        logger.error("systemctl %s %s failed: %s", action, SERVICE_NAME, exc)
        return False
    if result.returncode != 0:
        logger.error("systemctl %s %s failed: %s", action, SERVICE_NAME, result.stderr.strip())
    return result.returncode == 0


def main() -> int:
    config = Config.load()
    setup_logging(config)

    if not git_fetch():
        logger.debug("Offline or fetch failed -- nothing to do")
        flush_log_to_usb(config)
        return 0

    local = git_rev_parse("HEAD")
    remote = git_rev_parse("@{u}")
    if not local or not remote:
        logger.warning("Could not determine local/upstream commit -- skipping")
        flush_log_to_usb(config)
        return 1

    if local == remote:
        logger.debug("Already up to date (%s)", local[:8])
        flush_log_to_usb(config)
        return 0

    logger.info("Update available: %s -> %s", local[:8], remote[:8])
    previous_commit = local

    led = LedManager(config)
    if led.open():
        led.set_updating()

    if not systemctl("stop"):
        logger.error("Failed to stop %s.service -- aborting update", SERVICE_NAME)
        led.close()
        flush_log_to_usb(config)
        return 1

    ok = git_pull_ff_only()
    if ok:
        ok = pip_install_requirements()
    if ok:
        ok = py_compile_check()

    if ok:
        logger.info("Update applied successfully: now at %s", remote[:8])
        check_new_config_keys()
    else:
        logger.error("Update failed sanity checks -- rolling back to %s", previous_commit[:8])
        git_reset_hard(previous_commit)

    led.close()
    systemctl("start")

    flush_log_to_usb(config)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
