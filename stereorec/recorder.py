"""Continuous session video writer.

Writes one file per recording run into the session directory. A run that is
safely stopped and later resumed within the same session (stall/low-space/
thermal recovery) starts a new, separately-numbered file rather than trying to
append to an already-finalized one -- the simplest way to guarantee zero
footage loss with Picamera2/ffmpeg's standard recording API.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Callable, Optional

from stereorec.camera_manager import CameraManager
from stereorec.config import Config
from stereorec.state_manager import StateManager

logger = logging.getLogger(__name__)

_MONITOR_INTERVAL_S = 5.0


class Recorder:
    def __init__(
        self,
        camera_manager: CameraManager,
        config: Config,
        on_saved: Optional[Callable[[], None]] = None,
        on_fault: Optional[Callable[[str], None]] = None,
    ):
        self.camera_manager = camera_manager
        self.config = config
        self._on_saved = on_saved
        self._on_fault = on_fault
        self._current_path: Optional[str] = None
        self._recording = False
        self._last_size: Optional[int] = None
        self._monitor_stop = threading.Event()
        self._monitor_thread: Optional[threading.Thread] = None

    def start(self, session_dir: str, state_manager: StateManager) -> bool:
        picam2 = self.camera_manager.picam2
        if picam2 is None:
            logger.warning("Camera not open -- cannot start recording")
            return False
        try:
            from picamera2.encoders import H264Encoder
            from picamera2.outputs import FfmpegOutput
        except ImportError:
            logger.warning("picamera2 encoders/outputs unavailable -- cannot record")
            return False

        filename = self._next_filename(session_dir)
        path = os.path.join(session_dir, filename)
        try:
            encoder = H264Encoder(
                bitrate=self.config.bitrate, iperiod=self.config.keyframe_interval_frames
            )
            if self.config.prefer_hardware_encoder:
                logger.info("Recording via Picamera2 H264Encoder (V4L2 hardware path on Pi 4)")

            if self.config.video_container == "mp4":
                # FfmpegOutput passes output_filename through to the ffmpeg command
                # line verbatim, so extra muxer flags can be prefixed onto it --
                # fragmented MP4 keeps a crashed recording mostly playable, similar
                # in spirit to MPEG-TS.
                output_spec = f"-movflags frag_keyframe+empty_moov {path}"
            else:
                output_spec = path
            output = FfmpegOutput(output_spec)

            picam2.start_recording(encoder, output)
        except Exception:
            logger.exception("Failed to start recording")
            if self._on_fault:
                self._on_fault("start_failed")
            return False

        self._current_path = path
        self._recording = True
        self._last_size = None
        state_manager.register_video_file(filename)
        state_manager.set_recording_active(True)

        self._monitor_stop.clear()
        self._monitor_thread = threading.Thread(
            target=self._monitor_loop, name="recorder", args=(state_manager,), daemon=True
        )
        self._monitor_thread.start()
        logger.info("Recording started: %s", path)
        return True

    def _next_filename(self, session_dir: str) -> str:
        ext = self.config.video_container
        prefix = self.config.video_filename_prefix
        index = 1
        while True:
            name = f"{prefix}.{ext}" if index == 1 else f"{prefix}_{index}.{ext}"
            if not os.path.exists(os.path.join(session_dir, name)):
                return name
            index += 1

    def _monitor_loop(self, state_manager: StateManager) -> None:
        while not self._monitor_stop.wait(_MONITOR_INTERVAL_S):
            try:
                size = os.path.getsize(self._current_path)
            except OSError:
                logger.error("Recording output missing: %s", self._current_path)
                if self._on_fault:
                    self._on_fault("output_missing")
                return

            if self._last_size is not None and size <= self._last_size:
                logger.error("Recording output not growing: %s", self._current_path)
                if self._on_fault:
                    self._on_fault("no_growth")
                return

            self._last_size = size
            state_manager.touch()
            if self._on_saved:
                self._on_saved()

    def stop(self, state_manager: Optional[StateManager] = None) -> None:
        self._monitor_stop.set()
        if self._monitor_thread is not None:
            self._monitor_thread.join(timeout=5)
            self._monitor_thread = None

        if self._recording:
            picam2 = self.camera_manager.picam2
            if picam2 is not None:
                try:
                    picam2.stop_recording()
                    logger.info("Recording finalized: %s", self._current_path)
                except Exception:
                    logger.exception("Error stopping recording")
            self._recording = False

        if state_manager is not None:
            state_manager.set_recording_active(False)

    @property
    def is_recording(self) -> bool:
        return self._recording
