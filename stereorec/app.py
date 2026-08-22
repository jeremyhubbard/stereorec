"""The orchestrator: state machine, watchdog, and recovery.

There is exactly one thread that mutates the recording pipeline and state --
this orchestrator's own main-loop thread. Every worker (USB poller, frame
health monitor, recorder monitor, thermal monitor) only sets thread-safe flags
or logs; the orchestrator drains those flags and drives state each tick. The
one deliberate exception is the thermal status LED, which the thermal thread
updates directly -- it's a status light, not part of the recording pipeline.
"""

from __future__ import annotations

import datetime
import logging
import os
import signal
import threading
import time
from typing import Optional

from stereorec import logging_setup, sd_notify
from stereorec.camera_manager import CameraManager
from stereorec.config import Config
from stereorec.led_manager import LedManager
from stereorec.recorder import Recorder
from stereorec.state_manager import StateManager, read_previous
from stereorec.states import State, transition_allowed
from stereorec.thermal_manager import ZONE_DANGER, ZONE_NORMAL, ThermalManager
from stereorec.usb_manager import UsbManager
from stereorec.util import (
    ensure_dir,
    free_space_mb,
    read_cpu_temp_c,
    read_git_version,
    read_recent_journal,
    read_recent_kernel_log,
    read_throttled_flags,
)

logger = logging.getLogger(__name__)

SESSION_ROOT_NAME = "STEREOREC"
JOURNAL_TAIL_SECONDS = 10


class RecorderApp:
    def __init__(self, config: Config):
        self.config = config
        self._want_record = config.auto_start
        self._running = False
        self._shutdown_done = False

        self._state = State.BOOTING
        self.session_id: Optional[str] = None
        self.session_dir: Optional[str] = None
        self.state_manager: Optional[StateManager] = None

        self._usb_was_present = False
        self._low_space_warned = False
        self._restart_attempts = 0
        self._last_recovery_attempt = 0.0
        self._last_watchdog_ping = 0.0

        self._pending_lock = threading.Lock()
        self._stall_pending = False
        self._fault_pending = False
        self._fault_reason: Optional[str] = None
        self._thermal_zone = ZONE_NORMAL

        self.usb_manager = UsbManager(config)
        self.camera_manager = CameraManager(config)
        self.camera_manager.set_stall_callback(self._on_stall)
        self.recorder = Recorder(self.camera_manager, config, on_fault=self._on_fault)
        self.thermal_manager = ThermalManager(config, on_zone_change=self._on_thermal_zone_change)
        self.led_manager = LedManager(config)

    # -- entry point ---------------------------------------------------

    def run(self) -> int:
        self.session_id = datetime.datetime.now().strftime(self.config.session_dirname_format)
        logger.info("StereoRec starting, session_id=%s", self.session_id)
        logger.info("Running version %s", read_git_version())

        self.led_manager.open()
        self._set_state(State.BOOTING)
        self.usb_manager.start()
        self.thermal_manager.start()

        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)
        self._running = True

        exit_code = 0
        try:
            while self._running:
                start = time.monotonic()
                try:
                    self._tick()
                except Exception:
                    logger.exception("Tick failed -- continuing")
                elapsed = time.monotonic() - start
                time.sleep(max(0.0, self.config.main_loop_interval_s - elapsed))
        except Exception:
            logger.exception("Fatal orchestrator error -- exiting for systemd to restart")
            self._set_state(State.ERROR)
            exit_code = 1
        finally:
            self._shutdown()
        return exit_code

    def _handle_signal(self, signum, frame) -> None:
        logger.info("Received signal %s -- shutting down gracefully", signum)
        self._running = False

    # -- the tick --------------------------------------------------------

    def _tick(self) -> None:
        self._drain_commands()
        self._handle_faults()
        self._drive_state()
        self._pet_watchdog()

    def _drain_commands(self) -> None:
        # No command queue is wired up in this build -- intent is driven
        # entirely by auto_start. Kept as a distinct step for future use.
        pass

    def _handle_faults(self) -> None:
        with self._pending_lock:
            stall = self._stall_pending
            self._stall_pending = False
            fault = self._fault_pending
            fault_reason = self._fault_reason
            self._fault_pending = False
            self._fault_reason = None
        thermal_danger = self.thermal_manager.pop_danger_pending()

        if not (stall or fault or thermal_danger):
            return

        if thermal_danger:
            logger.warning("Thermal danger zone reached -- safely stopping recording")
        elif stall:
            # Silence the frame-health thread *before* the slow dmesg capture below --
            # otherwise it can fire one more stray warning against the dying camera
            # while dmesg is still running, which gets processed on the next tick
            # against the newly-reopened camera and kills a fresh recording for a
            # fault that belonged to the previous one.
            self.camera_manager.prepare_to_stop()
            logger.warning("Camera stall detected -- recovering (%s)", self._diagnostics_snapshot())
            self._log_kernel_log_tail()
            self._log_recent_journal()
            self._schedule_followup_journal_capture()
        elif fault:
            logger.warning(
                "Recording fault (%s) -- recovering (%s)", fault_reason, self._diagnostics_snapshot()
            )
            self._log_kernel_log_tail()
            self._log_recent_journal()
            self._schedule_followup_journal_capture()

        self._ensure_stopped()
        self._set_state(State.RECOVERING)

    def _log_kernel_log_tail(self) -> None:
        """Best-effort dmesg tail, logged alongside a stall/fault.

        Catches USB/xhci or CSI/unicam driver messages that would otherwise
        never surface in this app's own log -- the two most likely causes of
        a frame-capture stall that temp/throttle/free-space can't rule in or
        out on their own.
        """
        tail = read_recent_kernel_log()
        if tail:
            logger.warning("Recent kernel log (dmesg -T, newest last):\n%s", tail)

    def _log_recent_journal(self) -> None:
        """Best-effort journal tail, logged alongside a stall/fault.

        The one place that ever surfaces libcamera's own stderr output during
        a LIBCAMERA_LOG_LEVELS debugging session -- that never reaches
        stereorec.log otherwise. Some overlap with lines this app just logged
        is expected, since StandardOutput=journal mirrors them there too.

        This only ever looks *backward* from whenever it happens to run --
        see _schedule_followup_journal_capture() for the other side.
        """
        tail = read_recent_journal(seconds=JOURNAL_TAIL_SECONDS)
        if tail:
            logger.warning(
                "Recent journal, last %ds (unit=stereorec):\n%s", JOURNAL_TAIL_SECONDS, tail
            )

    def _schedule_followup_journal_capture(self) -> None:
        """Capture a second journal window a few seconds after a stall/fault.

        _log_recent_journal() runs inline in the fault-handling path and can
        only look backward from that moment -- it never sees anything logged
        during the teardown/reopen sequence that follows. Waiting here would
        delay recovery (the opposite of what prepare_to_stop() above is for),
        so this runs on its own daemon thread instead, off the main tick loop.
        """

        def _capture() -> None:
            time.sleep(JOURNAL_TAIL_SECONDS)
            tail = read_recent_journal(seconds=JOURNAL_TAIL_SECONDS)
            if tail:
                logger.warning(
                    "Recent journal, follow-up +%ds, last %ds (unit=stereorec):\n%s",
                    JOURNAL_TAIL_SECONDS,
                    JOURNAL_TAIL_SECONDS,
                    tail,
                )

        threading.Thread(target=_capture, name="journal-followup", daemon=True).start()

    def _diagnostics_snapshot(self) -> str:
        """Best-effort system snapshot to log alongside stall/fault events.

        Cheap, non-fatal reads only -- this runs on the hot fault path, and
        the whole point is narrowing down *why* a stall happened (storage
        hiccup vs. under-voltage vs. thermal) from the next log we get.
        """
        temp = read_cpu_temp_c()
        throttled = read_throttled_flags()
        free_mb = free_space_mb(self.session_dir) if self.session_dir else None
        return "cpu_temp={} throttled={} free_mb={}".format(
            f"{temp:.1f}C" if temp is not None else "unknown",
            throttled if throttled else "unknown",
            f"{free_mb:.0f}" if free_mb is not None else "unknown",
        )

    def _drive_state(self) -> None:
        usb_present = self.usb_manager.is_present
        if usb_present and not self._usb_was_present:
            self._on_usb_mounted()
        elif not usb_present and self._usb_was_present:
            self._on_usb_removed()
        self._usb_was_present = usb_present

        if not self._want_record:
            self._ensure_stopped()
            self._set_state(State.IDLE)
            return

        if not usb_present:
            self._ensure_stopped()
            self._set_state(State.RECOVERING)
            return

        if self.session_dir is None:
            self._set_state(State.RECOVERING)
            return

        free_mb = free_space_mb(self.session_dir)
        if free_mb < self.config.low_space_warn_mb:
            if not self._low_space_warned:
                logger.warning(
                    "LOW_SPACE: %.0f MB free (warn threshold %d MB)",
                    free_mb,
                    self.config.low_space_warn_mb,
                )
                self._low_space_warned = True
        else:
            self._low_space_warned = False

        if free_mb < self.config.min_free_mb:
            self._ensure_stopped()
            self._set_state(State.IDLE)
            return

        with self._pending_lock:
            thermal_zone = self._thermal_zone
        if thermal_zone == ZONE_DANGER:
            self._ensure_stopped()
            self._set_state(State.RECOVERING)
            return

        self._ensure_recording()

    def _pet_watchdog(self) -> None:
        interval = sd_notify.watchdog_interval_s()
        if interval is None:
            return
        now = time.monotonic()
        if now - self._last_watchdog_ping >= interval:
            sd_notify.watchdog_ping()
            self._last_watchdog_ping = now

    # -- pipeline control --------------------------------------------------

    def _ensure_recording(self) -> None:
        if self._state == State.RECORDING and self.recorder.is_recording:
            return

        now = time.monotonic()
        if now - self._last_recovery_attempt < self.config.recovery_retry_interval_s:
            return
        self._last_recovery_attempt = now

        if self.camera_manager.picam2 is None:
            if not self.camera_manager.open():
                self._register_restart_failure()
                return

        if self.recorder.start(self.session_dir, self.state_manager):
            self._restart_attempts = 0
            self._set_state(State.RECORDING)
        else:
            self._register_restart_failure()

    def _register_restart_failure(self) -> None:
        self._restart_attempts += 1
        if self._restart_attempts >= self.config.max_camera_restart_attempts:
            logger.error("Max restart attempts reached -- entering ERROR (still self-healing)")
            self._set_state(State.ERROR)
            self._restart_attempts = 0
        else:
            self._set_state(State.RECOVERING)

    def _ensure_stopped(self) -> None:
        if self.recorder.is_recording:
            self.camera_manager.prepare_to_stop()
            self.recorder.stop(self.state_manager)
        if self.camera_manager.picam2 is not None:
            self.camera_manager.close()

    # -- USB session lifecycle --------------------------------------------

    def _on_usb_mounted(self) -> None:
        logger.info("USB present at %s", self.usb_manager.mount_path)
        if self.session_dir is None:
            try:
                root = os.path.join(self.usb_manager.mount_path, SESSION_ROOT_NAME)
                session_dir = os.path.join(root, self.session_id)
                ensure_dir(session_dir)
                self._check_previous_sessions(root, session_dir)
                state_manager = StateManager(session_dir, self.session_id, os.getpid())
                state_manager.transition(self._state)
                self.session_dir = session_dir
                self.state_manager = state_manager
            except OSError:
                logger.exception("Failed to set up session directory on USB")
                return
        logging_setup.attach_usb_log(self.session_dir)

    def _on_usb_removed(self) -> None:
        logger.warning("USB removed")
        logging_setup.detach_usb_log()

    def _check_previous_sessions(self, root: str, current_session_dir: str) -> None:
        try:
            entries = os.listdir(root)
        except OSError:
            return
        for entry in entries:
            path = os.path.join(root, entry)
            if path == current_session_dir or not os.path.isdir(path):
                continue
            prev = read_previous(path)
            if prev and prev.get("recording_active"):
                logger.warning(
                    "Previous session %s ended while recording_active=true (unclean "
                    "shutdown); its files are left untouched",
                    entry,
                )

    # -- callbacks from worker threads (flags only, no pipeline mutation) --

    def _on_stall(self) -> None:
        with self._pending_lock:
            self._stall_pending = True

    def _on_fault(self, reason: str) -> None:
        with self._pending_lock:
            self._fault_pending = True
            self._fault_reason = reason

    def _on_thermal_zone_change(self, zone: str) -> None:
        with self._pending_lock:
            self._thermal_zone = zone
        self.led_manager.set_thermal_zone(zone)

    # -- state + shutdown ---------------------------------------------------

    def _set_state(self, new_state: State) -> None:
        if not transition_allowed(self._state, new_state):
            logger.warning("Rejected invalid transition %s -> %s", self._state, new_state)
            return
        self._state = new_state
        if self.state_manager is not None:
            self.state_manager.transition(new_state)
        self.led_manager.set_state(new_state)

    def _shutdown(self) -> None:
        if self._shutdown_done:
            return
        self._shutdown_done = True
        logger.info("Shutting down")
        self._ensure_stopped()
        self.led_manager.close()
        self.usb_manager.stop()
        self.thermal_manager.stop()
        self._set_state(State.SHUTDOWN)
        logging.shutdown()
