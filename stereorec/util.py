"""Durable filesystem primitives and small hardware-reading helpers."""

from __future__ import annotations

import json
import os
import shutil
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
