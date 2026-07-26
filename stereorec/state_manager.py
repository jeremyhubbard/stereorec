"""Thread-safe persistence of state.json for crash recovery."""

from __future__ import annotations

import datetime
import json
import logging
import os
import threading
import time
from typing import List, Optional

from stereorec.states import State, transition_allowed
from stereorec.util import atomic_write_json

logger = logging.getLogger(__name__)

STATE_FILENAME = "state.json"
_TOUCH_INTERVAL_S = 5.0


class StateManager:
    """Owns state.json for one session. Only the orchestrator thread should call this."""

    def __init__(self, session_dir: str, session_id: str, pid: int):
        self._lock = threading.Lock()
        self.session_dir = session_dir
        self.session_id = session_id
        self.pid = pid
        self.state = State.BOOTING
        self.recording_active = False
        self.video_files: List[str] = []
        self.current_video_file: Optional[str] = None
        self._last_touch = 0.0
        self._write()

    def transition(self, new_state: State) -> bool:
        with self._lock:
            if not transition_allowed(self.state, new_state):
                logger.warning("Rejected invalid transition %s -> %s", self.state, new_state)
                return False
            if self.state != new_state:
                logger.info("State %s -> %s", self.state.value, new_state.value)
                self.state = new_state
                self._write()
            return True

    def set_recording_active(self, active: bool) -> None:
        with self._lock:
            if self.recording_active != active:
                self.recording_active = active
                self._write()

    def register_video_file(self, filename: str) -> None:
        with self._lock:
            if filename not in self.video_files:
                self.video_files.append(filename)
            self.current_video_file = filename
            self._write()

    def touch(self) -> None:
        """Periodic durable rewrite while recording, throttled to avoid USB churn."""
        with self._lock:
            now = time.monotonic()
            if now - self._last_touch >= _TOUCH_INTERVAL_S:
                self._last_touch = now
                self._write()

    def _write(self) -> None:
        data = {
            "session_id": self.session_id,
            "state": self.state.value,
            "video_files": list(self.video_files),
            "current_video_file": self.current_video_file,
            "recording_active": self.recording_active,
            "updated_at": datetime.datetime.now().isoformat(timespec="seconds"),
            "pid": self.pid,
        }
        try:
            atomic_write_json(os.path.join(self.session_dir, STATE_FILENAME), data)
        except OSError as exc:
            logger.error("Failed to write %s: %s", STATE_FILENAME, exc)


def read_previous(session_dir: str) -> Optional[dict]:
    """Load a prior session's state.json for boot-time observability, or None."""
    path = os.path.join(session_dir, STATE_FILENAME)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None
