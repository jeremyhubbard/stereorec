"""All tunables for the recorder, with JSON/env override support."""

from __future__ import annotations

import dataclasses
import json
import os
from typing import Dict, Optional, Tuple

_TRUE_STRINGS = {"1", "true", "yes", "on"}


def _env_bool(value: str) -> bool:
    return value.strip().lower() in _TRUE_STRINGS


@dataclasses.dataclass
class Config:
    # Storage / USB
    usb_label: str = "STEREOREC"
    mount_roots: Tuple[str, ...] = ("/media", "/mnt", "/run/media")
    min_free_mb: int = 1024
    low_space_warn_mb: int = 2048
    usb_poll_interval_s: float = 2.0
    safe_mode_stop_on_usb_loss: bool = True

    # Camera / encoder
    frame_width: int = 2560
    frame_height: int = 720
    framerate: int = 30
    bitrate: int = 12_000_000
    keyframe_interval_frames: int = 30
    prefer_hardware_encoder: bool = True
    camera_num: int = 0
    expected_camera_count: int = 1
    tuning_file: Optional[str] = None
    sensor_mode_index: Optional[int] = None

    # Video output
    video_container: str = "ts"
    video_filename_prefix: str = "video"

    # Health / recovery
    frame_stall_threshold_s: float = 4.0
    frame_monitor_interval_s: float = 1.0
    max_camera_restart_attempts: int = 5
    recovery_retry_interval_s: float = 3.0

    # Thermal
    temp_warning_c: float = 70.0
    temp_danger_c: float = 80.0
    temp_recovery_hysteresis_c: float = 5.0
    temp_poll_interval_s: float = 5.0

    # Status LEDs
    led_enabled: bool = False
    led_gpio_pin: int = 18
    led_count: int = 2
    led_brightness: float = 0.2
    led_pixel_order: str = "GRB"
    led_state_colors: Dict[str, Tuple[int, int, int]] = dataclasses.field(
        default_factory=lambda: {
            "BOOTING": (0, 0, 60),
            "IDLE": (0, 60, 0),
            "RECORDING": (60, 0, 0),
            "RECOVERING": (60, 60, 0),
            "ERROR": (60, 0, 60),
            "SHUTDOWN": (0, 0, 0),
        }
    )
    led_thermal_colors: Dict[str, Tuple[int, int, int]] = dataclasses.field(
        default_factory=lambda: {
            "normal": (0, 0, 0),
            "warning": (60, 40, 0),
            "danger": (60, 0, 0),
        }
    )
    led_update_color: Tuple[int, int, int] = (0, 60, 60)

    # Loop / logging / misc
    main_loop_interval_s: float = 0.5
    log_filename: str = "stereorec.log"
    log_max_bytes: int = 5 * 1024 * 1024
    log_backup_count: int = 5
    log_level: str = "INFO"
    fallback_log_dir: str = "/run/stereorec"
    disable_fallback_log: bool = False
    detach_fallback_when_usb_present: bool = True
    session_dirname_format: str = "%Y%m%d_%H%M%S"
    auto_start: bool = True

    @classmethod
    def load(cls) -> "Config":
        config = cls()
        config._apply_json_overrides()
        config._apply_env_overrides()
        return config

    def _apply_json_overrides(self) -> None:
        path = os.environ.get("STEREOREC_CONFIG")
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return
        if not isinstance(data, dict):
            return
        valid_fields = {f.name for f in dataclasses.fields(self)}
        for key, value in data.items():
            if key in valid_fields:
                setattr(self, key, value)

    def _apply_env_overrides(self) -> None:
        if "STEREOREC_USB_LABEL" in os.environ:
            self.usb_label = os.environ["STEREOREC_USB_LABEL"]
        if "STEREOREC_LOG_LEVEL" in os.environ:
            self.log_level = os.environ["STEREOREC_LOG_LEVEL"]
        if "STEREOREC_AUTOSTART" in os.environ:
            self.auto_start = _env_bool(os.environ["STEREOREC_AUTOSTART"])
