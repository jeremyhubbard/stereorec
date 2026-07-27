"""Picamera2 wrapper: camera bring-up/enumeration plus the frame-health monitor.

Picamera2 being unavailable (e.g. off-Pi) must never crash the process -- every
method degrades to logging a warning and returning failure instead of raising.

Note on aspect ratio: on this HAT, the ArduChip packs both stereo eyes
anamorphically into a standard single-sensor-shaped frame (e.g. 2028x1520,
itself 4:3) rather than reporting a visibly wide combined frame -- so there is
no reliable width:height self-check that distinguishes "combiner active" from
"combiner inactive" here. The recorder therefore just records the raw captured
frame as-is; unsqueezing each eye back to its correct proportions is a
deliberate offline step (see tools/correct_aspect.py), not something done live.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

from stereorec.config import Config

logger = logging.getLogger(__name__)

try:
    from picamera2 import Picamera2

    PICAMERA2_AVAILABLE = True
except ImportError:
    Picamera2 = None  # type: ignore[assignment]
    PICAMERA2_AVAILABLE = False


class CameraManager:
    def __init__(self, config: Config):
        self.config = config
        self.picam2 = None
        self.frame_size: Optional[tuple] = None
        self._frame_lock = threading.Lock()
        self._frame_count = 0
        self._last_frame_time = 0.0
        self._stall_callback: Optional[Callable[[], None]] = None
        self._health_stop = threading.Event()
        self._health_thread: Optional[threading.Thread] = None
        self._stopping = threading.Event()

    def set_stall_callback(self, callback: Callable[[], None]) -> None:
        self._stall_callback = callback

    def prepare_to_stop(self) -> None:
        """Suppress stall detection once an intentional stop has begun.

        Frames genuinely stop arriving while stop_recording() finalizes the
        output, which is expected -- without this, the frame-health monitor
        logs a stream of misleading "camera stall" warnings for the whole
        (potentially long) flush window.
        """
        self._stopping.set()

    def open(self) -> bool:
        if not PICAMERA2_AVAILABLE:
            logger.warning("Picamera2 not available -- camera subsystem degraded")
            return False
        try:
            camera_info = Picamera2.global_camera_info()
            logger.info("Detected cameras: %s", camera_info)
            if len(camera_info) != self.config.expected_camera_count:
                logger.error(
                    "Expected %d camera(s), found %d -- refusing to start "
                    "(the single-stream stereo assumption would silently capture "
                    "one sensor otherwise)",
                    self.config.expected_camera_count,
                    len(camera_info),
                )
                return False

            tuning = None
            if self.config.tuning_file:
                try:
                    tuning = Picamera2.load_tuning_file(self.config.tuning_file)
                except OSError as exc:
                    logger.warning("Could not load tuning file %s: %s", self.config.tuning_file, exc)

            self.picam2 = Picamera2(camera_num=self.config.camera_num, tuning=tuning)

            for i, mode in enumerate(self.picam2.sensor_modes):
                logger.info("sensor_modes[%d]: %s", i, mode)

            if self.config.sensor_mode_index is not None:
                mode = self.picam2.sensor_modes[self.config.sensor_mode_index]
                size = mode["size"]
                video_config = self.picam2.create_video_configuration(
                    main={"size": size}, raw={"size": size}
                )
            else:
                size = (self.config.frame_width, self.config.frame_height)
                video_config = self.picam2.create_video_configuration(main={"size": size})

            self.picam2.configure(video_config)
            self.frame_size = video_config["main"]["size"]
            logger.info("Configured capture size: %s (recorded as-is, raw)", self.frame_size)

            self.picam2.pre_callback = self._pre_callback
            self._last_frame_time = time.monotonic()
            self._frame_count = 0
            self._stopping.clear()
            self.picam2.start()

            self._health_stop.clear()
            self._health_thread = threading.Thread(
                target=self._health_loop, name="frame-health", daemon=True
            )
            self._health_thread.start()
            return True
        except Exception:
            logger.exception("Camera open failed")
            self.picam2 = None
            return False

    def _pre_callback(self, request) -> None:
        with self._frame_lock:
            self._frame_count += 1
            self._last_frame_time = time.monotonic()

    def _health_loop(self) -> None:
        while not self._health_stop.is_set():
            if not self._stopping.is_set():
                with self._frame_lock:
                    elapsed = time.monotonic() - self._last_frame_time
                if elapsed > self.config.frame_stall_threshold_s and self._stall_callback:
                    logger.warning("No frame for %.1fs -- camera stall", elapsed)
                    self._stall_callback()
            self._health_stop.wait(self.config.frame_monitor_interval_s)

    def close(self) -> None:
        self._health_stop.set()
        if self._health_thread is not None:
            self._health_thread.join(timeout=5)
            self._health_thread = None
        if self.picam2 is not None:
            close_start = time.monotonic()
            try:
                self.picam2.close()
            except Exception:
                logger.exception("Error closing camera")
            logger.debug("picam2.close() took %.2fs", time.monotonic() - close_start)
            self.picam2 = None
