"""Durable filesystem primitives and small hardware-reading helpers."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from typing import Any, Optional

THERMAL_ZONE_PATH = "/sys/class/thermal/thermal_zone0/temp"


def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def _fsync_dir(dirpath: str) -> None:
    dirfd = os.open(dirpath, os.O_RDONLY)
    try:
        os.fsync(dirfd)
    finally:
        os.close(dirfd)


def atomic_write_bytes(path: str, data: bytes) -> None:
    """Write ``data`` to ``path`` durably: temp file + fsync, rename, fsync dir."""
    dirpath = os.path.dirname(path) or "."
    ensure_dir(dirpath)
    fd, tmp_path = tempfile.mkstemp(dir=dirpath, prefix=".tmp-", suffix=".part")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    _fsync_dir(dirpath)


def atomic_write_json(path: str, obj: Any) -> None:
    atomic_write_bytes(path, json.dumps(obj, indent=2, sort_keys=True).encode("utf-8"))


def free_space_mb(path: str) -> float:
    usage = shutil.disk_usage(path)
    return usage.free / (1024 * 1024)


def read_cpu_temp_c() -> Optional[float]:
    """Read the SoC temperature in Celsius, or None if no thermal zone is readable."""
    try:
        with open(THERMAL_ZONE_PATH, "r", encoding="utf-8") as fh:
            millidegrees = int(fh.read().strip())
        return millidegrees / 1000.0
    except (OSError, ValueError):
        return None


def read_throttled_flags() -> Optional[str]:
    """Best-effort read of the RPi under-voltage/throttling bitmask via ``vcgencmd``.

    Returns just the hex bitmask, e.g. ``"0x50005"`` (any nonzero bit means an
    under-voltage, frequency-capping, or throttling event has occurred either
    now or since boot -- see the "Bit" table in vcgencmd's docs), or None if
    vcgencmd isn't available (e.g. off-Pi, not on PATH, or unexpected output).
    """
    try:
        result = subprocess.run(
            ["vcgencmd", "get_throttled"],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    output = result.stdout.strip()
    _, _, value = output.partition("=")
    return value or None


def read_recent_kernel_log(max_lines: int = 20) -> Optional[str]:
    """Best-effort tail of the kernel ring buffer (``dmesg -T``), newest last.

    Meant to be called right at stall/fault detection to catch USB/xhci or
    CSI/unicam driver messages timed close to the event -- those don't show
    up anywhere else in this app's own logging. Returns None off-Pi, without
    permission, or if dmesg isn't on PATH.
    """
    try:
        result = subprocess.run(
            ["dmesg", "-T"],
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    if not lines:
        return None
    return "\n".join(lines[-max_lines:])
