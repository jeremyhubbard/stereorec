# StereoRec

A self-contained, high-reliability stereo video recorder for the
**Raspberry Pi 4** with an **Arducam synchronized dual-camera (side-by-side) system**,
using **Picamera2**. Designed for long-duration unattended operation through power loss,
USB removal, camera faults, and process crashes.
 
## Design priority (most important first)

1. **Never lose completed recorded footage.**
2. Detect and recover from failures automatically.
3. Continue recording with minimal interruption.
4. Maintain accurate session metadata.
5. Optimize performance only after reliability requirements are met.

Every design decision below is in service of that ordering. The most important property —
losing as little recorded footage as possible — is pursued through durable, crash-resilient
writes rather than by hoping nothing goes wrong. (The trade-offs of the current single-file
approach, and an alternative that hardens it further, are discussed under
[Potential improvements](#potential-improvements).)

---

## How it works

### Side-by-side stereo as a single stream
The Arducam Camarray/stereo HAT hardware-syncs both sensors and multiplexes them into
**one** wide CSI frame (left + right side-by-side). Picamera2 therefore sees a single
camera, and we encode a single H.264 stream — no synchronization between two encoders
required. On startup the camera manager logs the libcamera enumeration and the advertised
`sensor_modes`, and **refuses to start** if more than one camera appears (the single-stream
assumption would otherwise silently capture one sensor). The combined resolution comes from
`frame_width`/`frame_height` in `config.py` (placeholder `2560x720`) or, preferably, an
exact advertised mode pinned via `sensor_mode_index` — see *Camera setup* below.

### Continuous recording (one-or-more files per session)
Recording writes a **continuous video file**, directly into the session directory on the
USB drive:

```
STEREOREC/20260530_213100/video.ts
```

* **One encoder runs for the whole recording run** — the stream is not broken into pieces
  on a timer, so no gaps are introduced while recording normally.
* **MPEG-TS** is the default container for crash resilience: a TS file truncated by a power
  cut is still largely playable up to the point of truncation. The container is configurable
  via `video_container` (`"ts"` default, or `"mp4"` using fragmented MP4 so a crash still
  leaves a mostly-playable file — see the Configuration section).
* Progress and status are persisted to `state.json` (below) so a crash can be reconciled on
  the next boot.
* **A session can produce more than one video file.** If recording is safely stopped and
  later resumed *within the same boot* — a camera stall recovers, free space comes back
  above `min_free_mb`, or the CPU cools back out of the thermal danger zone (see
  [Thermal protection](#thermal-protection)) — the recorder starts a fresh, separately
  numbered file (`video.ts`, then `video_2.ts`, `video_3.ts`, ...) rather than trying to
  append to one that's already been finalized. Each file is independently playable, and
  none of them are ever overwritten, so this never costs completed footage; `state.json`
  lists every file produced during the session.

> An alternative recording model with a stronger "never lose completed footage" guarantee —
> and the trade-offs that come with it — is discussed under
> [Potential improvements](#potential-improvements).

### Session layout on the USB drive

```
STEREOREC/
└── 20260530_213100/            # session_id = boot timestamp
    ├── state.json              # persistent state for crash recovery
    ├── video.ts                # the session recording (video_2.ts, ... if resumed)
    └── logs/
        └── stereorec.log       # rotating (plus any flushed RAM fallback logs)
```

`state.json` (rewritten durably on every state transition, on `recording_active` changes,
and periodically while recording):
```json
{
  "session_id": "20260530_213100",
  "state": "RECORDING",
  "video_files": ["video.ts"],
  "current_video_file": "video.ts",
  "recording_active": true,
  "updated_at": "2026-05-30T21:45:03",
  "pid": 1234
}
```

### Crash recovery on boot
Every start uses a **new** `session_id` (boot timestamp) and never resumes into a previous
session folder. Before settling into `IDLE`, the orchestrator inspects past runs for
observability (`_setup_session` in `app.py`):

* A prior `state.json` showing `recording_active: true` is logged as a warning (the previous
  run ended while it was still recording), but it changes nothing — previous session
  directories and their files are left untouched.
* Because the video is written in a crash-resilient container, an unclean shutdown leaves at
  most the **tail** of the current (last) video file incomplete; every earlier file in the
  session, and everything written before the crash in the last one, remains playable.

### State machine
The six states (`stereorec/states.py`) and what each means:

| State | Meaning |
|---|---|
| `BOOTING` | Initial state; subsystems coming up. Left once boot completes. |
| `IDLE` | Healthy and ready, but not recording (no intent, no USB, or low space). |
| `RECORDING` | Camera streaming and the recorder is writing the session video file. |
| `RECOVERING` | A fault was detected (USB loss, stall, encoder fault) or recording is wanted but conditions aren't yet met; the orchestrator is retrying. |
| `ERROR` | Recovery attempts exhausted. The orchestrator still keeps self-healing (the attempt counter is reset) so it can return to `RECORDING` if hardware recovers. |
| `SHUTDOWN` | Terminal; graceful shutdown in progress. Reachable from any state. |

Transitions are **not** strictly linear. The allowed set (enforced by `transition_allowed`;
any other transition is rejected and logged) is:

```
BOOTING    → IDLE, RECORDING, RECOVERING, ERROR
IDLE       → RECORDING, RECOVERING, ERROR
RECORDING  → IDLE, RECOVERING, ERROR
RECOVERING → RECORDING, IDLE, ERROR
ERROR      → IDLE, RECOVERING, RECORDING
*          → SHUTDOWN        (always allowed)
self → self                  (always allowed, treated as a no-op)
```

All transitions are validated, logged, and thread-safe. Only the orchestrator thread
mutates state, so there is no silent drift. `state.json` is rewritten durably on every
transition, on `recording_active` changes, and periodically while recording.

### Threading model
There is exactly **one** thread that mutates the recording pipeline and state — the
orchestrator (`RecorderApp._main_loop` in `app.py`). Every other thread only sets
thread-safe flags or enqueues commands, which the orchestrator drains each tick. This is
the core invariant that prevents state drift.

| Thread (name) | Owner | Responsibility | How it talks to the orchestrator |
|---|---|---|---|
| main / orchestrator | `app.py` | the single tick loop: drains commands, handles faults, drives state, pets the watchdog | — |
| `usb-poller` | `usb_manager.py` | polls for the labelled drive every `usb_poll_interval_s` | `on_mounted` / `on_removed` callbacks (the orchestrator reads `is_present` on its own thread to act) |
| `frame-health` | `camera_manager.py` | a monitor that flags a stall if no frame arrives for `frame_stall_threshold_s` | `set_stall_callback` → sets `_stall_pending` flag |
| `recorder` | `recorder.py` | the session video write loop | `on_saved` / `on_fault` callbacks → sets `_fault_pending` flag |

The camera's own `pre_callback` (run by Picamera2 inside libcamera's thread) also fires per
frame; it does nothing but bump a counter + timestamp for the frame-health monitor.

### The orchestrator tick
`_main_loop` runs every `main_loop_interval_s` (0.5 s) and performs, in order:

1. `_drain_commands()` — apply any queued control commands and set the record intent
   (`_want_record`). In this build the intent is driven by `auto_start`, so the queue is
   normally empty.
2. `_handle_faults()` — atomically read+clear the `_stall_pending` / `_fault_pending` flags;
   if either is set, enter `RECOVERING` and tear down the pipeline.
3. `_drive_state()` — reconcile actual state toward intent: if USB is absent → safe-stop and
   `IDLE`/`RECOVERING`; if a session dir isn't built yet → build it; gate on free space;
   then `_ensure_recording()` or `_ensure_stopped()` depending on `_want_record`.
4. Pet the systemd watchdog (`WATCHDOG=1`) at half the `WatchdogSec` interval. If any step
   above hangs, the pings stop and systemd restarts the process.

The whole body is wrapped so a tick exception is logged and the loop continues; a fatal
loop error transitions to `ERROR` and the process exits non-zero for systemd to restart.

### Recovery model
Worker threads (USB poller, frame-health monitor) never touch the pipeline directly;
they raise thread-safe flags / enqueue commands. The orchestrator drains these each tick
and drives **actual** state toward the **intent** (`_want_record`) given current
conditions. On any fault:

| Fault | Detection | Action |
|---|---|---|
| USB removed | poller marks mount gone (write-probe also catches stale mounts) | safe-stop (finalizing the current file), `RECOVERING`, auto-resume into a **new** video file on reinsert |
| Camera stall | frame-health monitor: no frame for `frame_stall_threshold_s` | `CAMERA_STALL`, restart camera pipeline, resume into a new video file |
| Encoder/recorder fault | recorder loop exception / empty output | `CAMERA_ERROR`, tear down + retry (bounded, then `ERROR` but keeps self-healing) |
| Process crash / power loss | systemd `Restart=always`; boot reconciliation | start a new session; every already-finalized file stays playable, at most the last one is truncated at the crash point |
| Process hang | systemd `WatchdogSec` (sd_notify pings stop) | systemd restarts the process |
| Low disk space | `shutil.disk_usage` gate vs `min_free_mb` | refuse to start / stop recording (keeping every recorded file), notify `LOW_SPACE`, return to `IDLE` |
| Thermal danger zone | CPU temp ≥ `temp_danger_c` (see [Thermal protection](#thermal-protection)) | safely finalize the current file, `RECOVERING`, auto-resume into a new file once cooled below `temp_danger_c - temp_recovery_hysteresis_c` |

---

## Repository layout

```
RasPiCam/
├── stereorec/
│   ├── __init__.py          # package marker + __version__ + design-priority docstring
│   ├── __main__.py          # entry point: python -m stereorec
│   ├── app.py               # orchestrator: state machine, watchdog, recovery
│   ├── config.py            # all tunables (+ JSON/env overrides)
│   ├── states.py            # state machine + transition rules
│   ├── state_manager.py     # thread-safe persistent state.json
│   ├── usb_manager.py       # labelled-drive detection + hotplug polling
│   ├── camera_manager.py    # Picamera2 wrapper + frame-health monitor
│   ├── recorder.py          # continuous session video writer (multi-file-aware)
│   ├── thermal_manager.py   # CPU temperature monitor (warning/danger zones)
│   ├── led_manager.py       # NeoPixel status LEDs (state + thermal indicator)
│   ├── sd_notify.py         # systemd readiness + watchdog pings
│   ├── logging_setup.py     # rotating logs (USB + RAM fallback)
│   └── util.py              # durable/atomic filesystem primitives
├── tools/
│   ├── correct_aspect.py       # offline anamorphic aspect-ratio correction
│   ├── check_for_update.py     # git fetch/pull + restart, see Auto-updating over Ethernet
│   └── update_button_watcher.py  # optional GPIO button -> on-demand update check
├── systemd/
│   ├── stereorec.service
│   ├── stereorec-update.service   # one-shot: runs check_for_update.py
│   ├── stereorec-update.timer     # triggers the above every few minutes
│   └── stereorec-update-button.service  # optional: runs update_button_watcher.py
├── .gitignore
├── config.example.json
├── requirements.txt
└── README.md
```

---

## First-time setup & camera bring-up

Do this once on a fresh Raspberry Pi OS install to confirm the hardware works **before**
deploying the service. Steps 1–2 overlap with the **Installation** section below — if you do
bring-up first you can skip re-installing those packages.

### 1. OS and Python version
* **Raspberry Pi OS Bookworm or Trixie, 64-bit** (either release works). Use the **system
  Python** that ships with the OS — **Python 3.11** on Bookworm, **Python 3.13** on Trixie.
  Do *not* install a separate Python interpreter: Picamera2/libcamera bindings are compiled
  against the system Python and won't be visible to an unrelated build. The code targets
  **Python ≥ 3.9** (uses `from __future__ import annotations` and PEP 585 generics), so both
  releases are fine.
  ```bash
  python3 --version          # 3.11.x on Bookworm, 3.13.x on Trixie
  sudo apt update && sudo apt full-upgrade -y && sudo reboot
  ```

### 2. System packages
```bash
sudo apt install -y python3-picamera2 ffmpeg python3-venv git
```
Picamera2 and libcamera **must** come from apt, never pip — they ship compiled bindings
matched to the system libcamera. (Package names are identical on Bookworm and Trixie.)

### 3. Arducam driver / device tree
The synchronized stereo HAT needs Arducam's Camarray driver plus a model-specific
`dtoverlay=imx477` and `camera_auto_detect=0` line in `/boot/firmware/config.txt` so both sensors are combined into one
side-by-side stream. Follow Arducam's install guide for your exact HAT, then reboot. Full
details and how to confirm the combiner is active are in
[Camera setup](#camera-setup-arducam-camarray--stereo-hat) below.

### 4. Verify the camera with libcamera tools (no Python)
```bash
# List cameras + sensor modes. For the stereo HAT you want ONE camera with a WIDE mode.
rpicam-hello --list-cameras            # older OS: libcamera-hello --list-cameras

# 5-second preview — confirms frames actually flow:
rpicam-hello -t 5000

# Record a 5-second test clip and inspect the combined frame:
rpicam-vid -t 5000 --width 2560 --height 720 -o /tmp/test.h264
ffprobe /tmp/test.h264
```
A correctly combined stereo frame is **much wider** than a single sensor (≈2.6:1 or more).
If you only see a ~4:3 / 16:9 mode, the combiner isn't active — fix that before continuing.

### 5. Verify with Picamera2 (Python)
```bash
python3 - <<'PY'
from picamera2 import Picamera2
print("cameras:", [c.get("Model") for c in Picamera2.global_camera_info()])
p = Picamera2()
for i, m in enumerate(p.sensor_modes):
    print(i, m["size"], m.get("fps"), m.get("format"))
p.close()
PY
```
Copy the wide combined size into `frame_width`/`frame_height` (or set `sensor_mode_index`)
in config.json. You can also just run the app — it logs enumeration + `sensor_modes[i]` on
startup and warns if the frame is too narrow to be a real stereo pair:
```bash
python3 -m stereorec        # watch the sensor_modes / aspect-ratio lines, then Ctrl-C
```

### 6. Project virtualenv
The recorder itself needs **no extra pip packages** — Picamera2 and ffmpeg come from apt. A
virtualenv still keeps any optional tooling (and the NeoPixel libraries below) isolated:
```bash
python3 -m venv --system-site-packages ~/stereorec-venv
source ~/stereorec-venv/bin/activate
```
`--system-site-packages` is **required** so the venv can still import the apt-installed
picamera2/libcamera. On recent Raspberry Pi OS (both Bookworm and Trixie) the system Python
is "externally managed" (PEP 668), so always install into a venv rather than system-wide.

Make sure your user can access the camera (Raspberry Pi OS usually adds the desktop user to
`video` automatically):
```bash
sudo usermod -aG video "$USER"     # then log out/in
```

### 7. (Optional) NeoPixel status LEDs — Raspberry Pi 4
> **The recorder drives these LEDs itself** (`stereorec/led_manager.py`), as a live status
> indicator, when `led_enabled: true` in config.json — see
> [Status LEDs](#status-leds) in the Configuration section. Do the wiring/bring-up bring-up
> below first, using the standalone `led_test.py` to confirm the wiring independently of the
> recorder, then move on to **7b** to verify the integrated indicator.

On the Pi 4 the standard Adafruit method works using hardware PWM/DMA on **GPIO18**:
```bash
source ~/stereorec-venv/bin/activate
pip install adafruit-blinka adafruit-circuitpython-neopixel rpi_ws281x
sudo usermod -aG gpio "$USER"          # then log out/in
```
Wire the LED **DIN** to **GPIO18 (physical pin 12)**, share ground with the Pi, and power the
strip from a suitable **5V** supply (for anything beyond a few LEDs, level-shift the 3.3V data
line up to 5V). Test — the `rpi_ws281x` PWM/DMA backend **requires root**:
```python
# led_test.py
import board, neopixel
pixels = neopixel.NeoPixel(board.D18, 2, pixel_order=neopixel.GRB, auto_write=True)
pixels.fill((0, 40, 0))     # dim green
```
```bash
sudo ~/stereorec-venv/bin/python led_test.py
```
(`2` above matches the recorder's default `led_count`; use your actual strip length if
different.) Note: GPIO18 is also the PWM audio line — if onboard audio is enabled it can
conflict; set `dtparam=audio=off` in `/boot/firmware/config.txt` if the LEDs misbehave.

### 7b. Verify the integrated status indicator
With the wiring confirmed above, enable it in the recorder itself:
```json
{ "led_enabled": true, "led_gpio_pin": 18, "led_count": 2 }
```
Then run the app **as root** (same requirement as `led_test.py`, and the default for the
systemd service — see [Installation](#installation-raspberry-pi-4-raspberry-pi-os-bookworm-or-trixie)):
```bash
sudo /opt/stereorec/venv/bin/python -m stereorec
```
Confirm pixel 0 follows the state machine live — blue during `BOOTING`, green in `IDLE`, red
once `RECORDING` starts, yellow if forced into `RECOVERING` (e.g. pull the USB) — and, if
`led_count >= 2`, pixel 1 reflects the thermal zone (off/amber/red — see
[Thermal protection](#thermal-protection)). Both should go dark within `TimeoutStopSec` of
Ctrl-C/`SIGTERM` (the graceful shutdown blanks the strip while the Pi still has power).

---

## Installation (Raspberry Pi 4, Raspberry Pi OS Bookworm or Trixie)

```bash
# 1) System packages (Picamera2, ffmpeg, gpiozero come from apt, not pip; git to clone).
sudo apt update
sudo apt install -y python3-picamera2 ffmpeg python3-venv python3-gpiozero bluez git

# 2) Deploy the code -- a git clone, not a plain copy, so it can be updated later with
#    `git pull` (see Auto-updating over Ethernet below).
sudo git clone https://github.com/jeremyhubbard/stereorec.git /opt/stereorec
sudo cp /opt/stereorec/config.example.json /opt/stereorec/config.json   # then edit

# 3) Virtualenv that can still see the apt-installed picamera2/libcamera.
#    Also installs the NeoPixel LED packages (adafruit-blinka, neopixel, rpi_ws281x) --
#    only actually used if led_enabled: true in config.json.
sudo python3 -m venv --system-site-packages /opt/stereorec/venv
sudo /opt/stereorec/venv/bin/pip install -r requirements.txt

# 4) Label the USB drive STEREOREC (do this once, ERASES the partition's label only).
#    For FAT32:   sudo fatlabel /dev/sda1 STEREOREC
#    For exFAT:   sudo exfatlabel /dev/sda1 STEREOREC
#    For ext4:    sudo e2label  /dev/sda1 STEREOREC
#    (exFAT/ext4 recommended: FAT32 caps a single file at 4 GB, and a long recording
#     run will exceed that.)

# 5) Install and enable the service. Point ExecStart at the venv python if using one:
#    edit systemd/stereorec.service -> ExecStart=/opt/stereorec/venv/bin/python -m stereorec
#    No User= is set, so it runs as root by default (needed for the NeoPixel PWM/DMA
#    backend and for camera/GPIO access); its RuntimeDirectory=stereorec line creates
#    /run/stereorec (tmpfs) automatically for the RAM fallback_log_dir default -- no
#    manual log-directory setup needed.
sudo cp systemd/stereorec.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now stereorec
journalctl -u stereorec -f

# 6) Set up the power button (recommended for an unattended/enclosed deployment) --
#    see "GPIO shutdown button" under Safe shutdown & power-down below.

# 7) (Development devices only -- see Auto-updating over Ethernet below) enable
#    periodic update checks, and optionally the on-demand GPIO button watcher:
sudo cp systemd/stereorec-update.service systemd/stereorec-update.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now stereorec-update.timer
sudo cp systemd/stereorec-update-button.service /etc/systemd/system/   # optional
sudo systemctl enable --now stereorec-update-button.service            # optional
```

### Run on boot (final deployment)
For an unattended device, the app should start automatically on every power-up. This is
handled by **systemd** — the right tool for a headless, must-stay-running service: it starts
at boot, restarts on crash (`Restart=always`), pets the watchdog (`WatchdogSec=30`), and
logs to the journal.
(`rc.local`, `cron @reboot`, and desktop-autostart all lack restart supervision and/or need
a GUI session, so they are not used here.)

The unit ships ready for this: `WantedBy=multi-user.target` means it launches on a normal
headless boot once *enabled*. The one command that makes it boot-persistent is
`systemctl enable`:

```bash
sudo systemctl enable --now stereorec   # enable = start on every boot; --now also starts it now
```

Verify it survives a reboot:

```bash
systemctl is-enabled stereorec   # -> enabled
systemctl status stereorec       # -> active (running)
sudo reboot                      # then re-check status to confirm it came up unattended
```

Notes for the boot path:
* **No USB at boot is fine** — the app boots to `IDLE` and resumes on hotplug, so startup
  never depends on the drive being present.
* **`auto_start: true`** (the default in [`config.py`](stereorec/config.py)) means recording
  begins automatically once the `STEREOREC` drive is detected.

### Running manually (development / first run)
```bash
cd /opt/stereorec
PYTHONPATH=/opt/stereorec /opt/stereorec/venv/bin/python -m stereorec
```

By default the app starts recording **automatically on boot** (`auto_start: true` in
[`config.py`](stereorec/config.py)); set `STEREOREC_AUTOSTART=0` (or `"auto_start": false` in
config.json) to disable it and stay in `IDLE`.

With auto-start on and no drive yet, the app waits in `RECOVERING` and begins recording the
moment the `STEREOREC` USB is detected.

### Stopping the recorder without powering off the Pi
To end recording/the app while leaving the Pi itself running:

```bash
sudo systemctl stop stereorec
```
This sends the service `SIGTERM` — the same graceful path as a full shutdown (see below): the
current video file is finalized, the camera is closed, LEDs are blanked, and logs are flushed,
all without touching system power. `stop` alone doesn't affect autostart, so it will start
again on the next reboot; to also prevent that, use `sudo systemctl disable stereorec` (or
`disable --now` to stop *and* disable in one command). Bring it back with
`sudo systemctl start stereorec` (or `enable --now` to re-enable autostart and start it).

If you're running it manually in the foreground (`python -m stereorec` in a terminal) instead
of via systemd, `Ctrl-C` triggers the same graceful shutdown handler.

### Safe shutdown & power-down
Cutting power mid-write is what truncates the recording (and can leave the USB filesystem
dirty). To end the recording cleanly and halt **before** power is removed, trigger a normal
shutdown and let it finish.

**No app changes are needed.** A systemd shutdown sends the service `SIGTERM`
(`KillSignal=SIGTERM`, `TimeoutStopSec=90` in the unit); the app's signal handler runs its
graceful `_shutdown()`, which finalizes the recording, closes the camera, and flushes logs,
after which the OS unmounts the USB. `sudo poweroff` already does exactly this — the button
below just makes it a one-press action in the field.

**GPIO shutdown button (built into Raspberry Pi OS) — the recommended way to power this
device on and off.** For an unattended/enclosed deployment with no keyboard or display, this
one device-tree line is the whole solution: no app code is needed, it gives both a clean
poweroff *and* power-on-from-halt from the same button, and it composes with everything above
since a clean poweroff is just a normal systemd shutdown (`stereorec` gets `SIGTERM` like any
other stop). Add one line to `/boot/firmware/config.txt` (on Bullseye it's `/boot/config.txt`):
```
dtoverlay=gpio-shutdown
```
* Wire a **momentary button** between **GPIO3 (pin 5)** and **GND (pin 6)**.
* Press → clean `poweroff` (service gets SIGTERM → recording finalized → USB unmounted → halt).
* The same GPIO3 button also **powers the Pi back on** from halt.
* Procedure: press → wait for the green **ACT LED to stop** (or ~15–20 s) → *then* cut power.
* GPIO3 is I²C1 SCL; if you use I²C, pick another pin with
  `dtoverlay=gpio-shutdown,gpio_pin=27` (only GPIO3 gives power-on-from-halt).

For **unattended / accidental** power loss (nobody to press the button), use a UPS or
supercapacitor HAT that raises a GPIO on input-power loss; a small listener runs
`sudo shutdown -h now` and the UPS holds power until the halt completes.

#### Blanking the NeoPixel LEDs on shutdown
The button triggers a normal systemd shutdown, so `stereorec`'s `SIGTERM` handler runs
**while the Pi still has power** — the window in which the strip can be sent an "all off"
frame. This is handled automatically: `RecorderApp._shutdown()` (`stereorec/app.py`) calls
`led_manager.close()` right after finalizing the recording and closing the camera, which
blanks every pixel before the process exits. No separate `leds-off.service` or extra script
is needed.

> **Caveat:** this only helps while the Pi still has power (during the stop phase, before the
> halt / before you cut power). If power is yanked abruptly the LEDs simply lose their supply
> and go dark anyway — so blanking matters mainly when the LED 5V rail can outlive the Pi's
> control and would otherwise freeze on its last color.

### Disabling WiFi/Bluetooth to save power
StereoRec is a standalone, offline device — recording is written straight to the USB drive
and read back later by pulling the drive, and the systemd `sd_notify` socket used for
readiness/watchdog pings is local IPC, not network (see [Configuration](#configuration)) — so
neither radio is needed at runtime. Disabling both saves power, which matters most on
battery/UPS-backed deployments. Add to `/boot/firmware/config.txt` (on Bullseye,
`/boot/config.txt`) and reboot:

```
dtoverlay=disable-wifi
dtoverlay=disable-bt
```

This disables the radios at the hardware/driver level (more thorough than just stopping
services — it actually powers them down), and needs a reboot to take effect. Confirm with
`ip link` (no `wlan0`) and `hciconfig` or `bluetoothctl list` (no controller).

If you'd rather keep the radios available but idle most of the time instead of permanently
disabling them, `rfkill` is a lighter, no-reboot alternative that persists across reboots:
```bash
sudo rfkill block wifi
sudo rfkill block bluetooth
```

> Installation still installs `bluez` (the Bluetooth stack) via apt even though nothing in
> `stereorec/` uses it — harmless to leave installed alongside `disable-bt` (the package
> just has nothing to talk to), but it can be dropped from that `apt install` line too if
> Bluetooth is permanently off.

### Auto-updating over Ethernet
> **Assumption this leans on: Ethernet is only ever connected to this device during
> development, never during an unattended field recording.** That's what makes it safe to
> treat "a newer commit exists" as "safe to stop recording, update, and restart" without any
> live in-process pause/resume or way to defer an update mid-recording. If a deployment ever
> needs ethernet plugged in during real field recording, disable this first (see below) — the
> WiFi/Bluetooth radios being off doesn't help here, since this uses the wired interface.

`/opt/stereorec` is a git working tree (see Installation, step 2), so it can pick up new
commits pushed to its GitHub remote. `systemd/stereorec-update.timer` runs
`tools/check_for_update.py` every 5 minutes (`OnUnitActiveSec=5min`; negligible cost when
offline since the check just times out fast):

1. `git fetch` (15s timeout) — no network/unreachable just means "nothing to do."
2. Compare local `HEAD` to the upstream ref — equal means already up to date.
3. Otherwise: light pixel 0 with `led_update_color` (cyan by default — see
   [Status LEDs](#configuration)), `systemctl stop stereorec` (graceful — finalizes any
   in-progress recording exactly like a manual stop), `git pull --ff-only`,
   `pip install -r requirements.txt`, then a `python -m py_compile` sanity check.
   * **Success:** blank the LED and `systemctl start stereorec` — recording resumes
     automatically via `auto_start` in a new session, and the main app's own startup sequence
     immediately takes the strip back over. It also compares `config.example.json`'s keys
     against your (gitignored, never-modified) `config.json` and logs a warning listing any
     new fields the example has that yours doesn't — a pull can add new tunables, but it can
     never see or touch your actual config, so this is the only way you'd otherwise find out
     one exists.
   * **Failure:** log the error, `git reset --hard` back to the pre-update commit, blank the
     LED, and restart on the known-good code anyway (auto-rollback, not a stuck/stopped
     device) — the bad commit is left for you to fix in the repo.

The updater's own logs never touch the SD card either: it writes to
`<fallback_log_dir>/stereorec-update.log` (RAM/tmpfs) and copies that file onto
`<mount>/STEREOREC/update_logs/stereorec-update.log` at the end of every run if the USB
drive is currently mounted — same wear-reduction goal as
[Reducing SD-card wear](#reducing-sd-card-wear).

**On-demand check.** Instead of waiting up to 5 minutes:
```bash
sudo systemctl start stereorec-update.service    # runs one check immediately
```
or wire an optional momentary button between **GPIO17 (physical pin 11)** and **GND**, and
enable `stereorec-update-button.service` (Installation step 7) — pressing it runs the same
on-demand check via `tools/update_button_watcher.py`. GPIO17 is free on this build (GPIO3 is
the shutdown button, GPIO18 is the LED data line); override the pin with
`STEREOREC_UPDATE_BUTTON_PIN` if it's needed for something else.

**Checking status:**
```bash
systemctl list-timers stereorec-update.timer   # last/next run
journalctl -u stereorec-update -e              # last check's log
git -C /opt/stereorec log -1 --oneline         # commit currently deployed
```

**Disabling for a field deployment** (in addition to simply not connecting ethernet):
```bash
sudo systemctl disable --now stereorec-update.timer
sudo systemctl disable --now stereorec-update-button.service   # if the button was enabled
```

### Camera setup (Arducam Camarray / stereo HAT)
This build assumes the HAT enumerates as a **single** camera producing one **combined
side-by-side** frame. The Camarray's on-board ArduChip must be told to *concatenate* both
sensors; without its driver/overlay it falls back to passing through a **single sensor**,
and `--list-cameras` will show stock single-sensor modes (for IMX477: `1332x990`,
`2028x1080`, `2028x1520`, `4056x2160`, `4056x3040`). That is the most common setup mistake.

```bash
# 1) Install Arducam's Camarray support for YOUR exact HAT per Arducam's current docs
#    (typically their install script). Then set the overlay in /boot/firmware/config.txt
#    (on a Pi 4 running Bullseye the file is /boot/config.txt):
#        camera_auto_detect=0
#        dtoverlay=imx477          # 12MP IMX477 stereo CamArray: the ArduChip presents
#                                  # BOTH sensors as ONE wide imx477 camera
#    Then reboot. (A different CamArray sensor needs a different overlay — check the docs.)

# 2) Confirm the COMBINED stream appears (note the much wider mode) and the modes:
rpicam-hello --list-cameras        # (or libcamera-hello --list-cameras)
```

> The `imx477` / `camera_auto_detect=0` lines above are from Arducam's CamArray quick-start
> for the 12MP IMX477 synchronized stereo kit; confirm the exact overlay for your board in
> [Arducam's docs](https://docs.arducam.com/Raspberry-Pi-Camera/Multi-Camera-CamArray/quick-start/).

How to tell the combiner is active:
* The advertised mode is **much wider** than a single sensor — two ~4:3 IMX477 images
  side-by-side give roughly an **8:3 (~2.67:1)** aspect ratio. (Camarray usually
  *downscales* the combined frame for MIPI bandwidth, so expect a reduced size, not a
  literal `8112x3040`.)
* The app's startup log shows that wide size in `sensor_modes[i]`, and **no**
  `aspect ratio ... does NOT look like a combined side-by-side stereo frame` warning.

Then:
* Set `frame_width`/`frame_height` to that **real advertised combined size**, or pin it
  with `sensor_mode_index` (read the indices from the `sensor_modes[i]` log lines).
* If `--list-cameras` shows **two** cameras instead of one combined stream, this
  single-stream build refuses to start (`expected_camera_count`) — that's the separate
  dual-camera hardware path and needs a different architecture.
* If your module ships a custom tuning JSON, point `tuning_file` at it.

**Getting each eye undistorted (aspect ratio).** Each eye occupies half the combined width
at full height, so the combined frame must be **twice a single eye's aspect ratio**:

```
combined_aspect = 2 × (eye_width / eye_height)
each eye = (combined_width / 2) × combined_height   ← must match the sensor's native shape
```

The IMX477 is a **4:3** sensor, so each eye stays correct only when the combined frame is
**8:3 (≈2.667:1)**. Choose a combined size whose half-width : height is 4:3:

| Combined | Per eye | Per-eye aspect | Result |
|---|---|---|---|
| `1920×720` | 960×720 | 4:3 | ✅ correct |
| `4056×1520` | 2028×1520 | 4:3 | ✅ correct (full readout) |
| `2560×720` (config default) | 1280×720 | 16:9 | ⚠️ each eye stretched/cropped |

If the halves look horizontally stretched or vertically squished, the combined aspect isn't
8:3 — e.g. the default `2560×720` is **32:9**, which gives 16:9 eyes. Whatever you pick **must
still be an advertised combined mode** (above); a non-advertised size makes libcamera
scale/crop and distort the split. The ArduChip's real output is authoritative: read
`sensor_modes[i]` and check `width/2 : height` — ≈4:3 means the eyes are geometric; 16:9 means
the combiner is cropping to widescreen by design.

**In practice, on this HAT the ArduChip reports combined frames anamorphically** — packed
into a standard single-sensor-shaped mode (e.g. `2028×1520`, itself 4:3) rather than a
visibly wide one; see the next section. The recorder makes no attempt to correct this live:
it records the raw captured frame as-is (see *Continuous recording* above), and correction is
a deliberate **offline** step run afterward on the recorded file:

```bash
python3 tools/correct_aspect.py /media/pi/STEREOREC/<session>/video.ts
# or with an explicit target width / squeeze factor:
python3 tools/correct_aspect.py <video> --target-width 5404
python3 tools/correct_aspect.py <video> --squeeze 2.665
```

It stretches the frame's width via ffmpeg's `scale` filter (re-encoding, since the geometry
changes) to un-squeeze the two eyes back to their correct proportions. The default squeeze
factor (`2.665`) was empirically validated for a `2028×1520` capture correcting to
`5404×1520`; recompute it for your own captured size/HAT if different. Doing this live during
recording was deliberately ruled out: it would add a per-frame resize cost and move the
encoder off the sensor-mode-matched resolutions the hardware-encoder throughput table below
is based on.

> **No automated aspect-ratio self-check.** An earlier design of this recorder warned
> automatically when the captured frame looked "too narrow" to be a combined stereo pair,
> on the assumption that a genuine combined frame is always visibly wide. On this HAT that
> assumption doesn't hold: the ArduChip packs both eyes **anamorphically** into a standard
> single-sensor-shaped frame (see *Frame rate, resolution & bit depth* below and
> *Getting each eye undistorted* immediately below) — a perfectly valid combined capture can
> report a narrow, 4:3-looking size. Because a narrow frame is therefore not reliable
> evidence the combiner is inactive, the recorder does not attempt that check; confirm the
> combiner is active by cross-checking `sensor_modes[i]` against Arducam's documented modes
> for your exact HAT instead, and via the `rpicam-hello`/Picamera2 steps in *First-time setup*
> above.

### Frame rate, resolution & bit depth (>50 fps)
Frame rate is capped by the **sensor mode** (resolution × raw bit depth), not just the
`framerate` control. This HAT's `imx477` exposes three bit-depth families, each covering all
resolutions — max fps for each, from `rpicam-hello --list-cameras`:

| Resolution | 8-bit | 10-bit | 12-bit |
|---|---|---|---|
| `1332×990` (binned) | 147.9 | 120.5 | 101.7 |
| `2028×1080` | 92.3 | 74.7 | 62.8 |
| `2028×1520` | 66.4 | 53.8 | 45.2 |
| `4056×2160` | 24.3 | 19.6 | 16.4 |
| `4056×3040` (full) | 17.4 | 14.0 | 11.7 |

For a given resolution, **lower bit depth = higher fps**, so exceeding 50 fps means trading
down resolution *or* bit depth. At `2028×1520`, **10-bit reaches 53.8 fps** while **12-bit
tops out at 45.2 fps** — so 10-bit is what lets that resolution clear 50 fps (8-bit → 66.4).
`2028×1080` gives more headroom (10-bit → 74.7 fps), and `1332×990` runs 100+ fps at any depth.
The bit depth is the **raw sensor** readout; the recorded H.264 is 8-bit either way, so
dropping 12-bit → 10-bit to gain fps costs nothing in the final video's color.

The ArduChip delivers **both eyes packed into one of these standard 4:3 frames**, which you
reframe to the target stereo aspect (see *Getting each eye undistorted* above) downstream — so
these 4:3 modes *are* the combined stereo frames, not a single sensor.

**Selecting it.** Pin the exact mode and set `framerate`. The `sensor_modes` *index* (not the
`rpicam-hello` order) is what `config.py` wants — enumerate them in Python first (bring-up
step 5) to find the 10-bit `2028×1520` entry, then:
```json
{ "sensor_mode_index": <index of the 10-bit 2028×1520 mode>, "framerate": 50 }
```
Keep `framerate` at or below the mode's ceiling (≤~53 for 2028×1520 10-bit); higher requests
are clamped.

> **Pi 4 hardware encoder — measured.** The often-cited ~1080p ceiling is a throughput
> guideline, not a hard frame-size cap. Measured on this hardware (all comfortably on the
> **hardware** encoder):
>
> | Mode | ~Pixels/sec | h264 clock | CPU |
> |---|---|---|---|
> | `2028×1520` @ ~50 fps (10-bit) | ~166 MP/s | 312 MHz | ~20% |
> | `4056×3040` @ ~11.7 fps (12-bit) | ~144 MP/s | 500 MHz | ~10% |
>
> Since `4056×3040` is the **largest** sensor mode and it encodes in hardware, every
> resolution the camera offers is within the encoder's **frame-size** capability. The real
> ceiling is **throughput (resolution × fps)** plus clock headroom — note the clock already
> rose to 500 MHz at full resolution. The untested corner is **high-fps 8-bit** modes
> (`2028×1080` @ 92 fps, `2028×1520` @ 66 fps, `4056×3040` @ 17 fps ≈ 200–215 MP/s), ~25–30%
> more throughput than measured above — very likely fine, but spot-check those with the runtime
> probes below before assuming.
>
> Two limits move **downstream** of the encoder: **USB write throughput** must sustain the
> `bitrate` in real time, and a very large frame (e.g. `4056×3040`) is well beyond standard
> H.264 levels — the Pi will encode it, but many players/decoders won't open a frame that big,
> so verify your playback/merge chain.

### Verifying the hardware encoder is being used
The encoded file **does not record** whether hardware or software H.264 was used, so you check
at **runtime, in a separate terminal (or SSH session), while a recording is active** — these
are live-state probes and read as "idle" when nothing is recording. (Running as the systemd
service, the recording is already going in the background, so one extra shell is enough;
running manually in the foreground, use a second terminal.)

```bash
# 1) H.264 hardware clock — non-zero while the HW encoder runs, 0 if it isn't (i.e. software).
vcgencmd measure_clock h264            # e.g. frequency(28)=333333000 (active) vs =0 (idle/SW)

# 2) Is the hardware encoder's V4L2 node open by the recorder?
v4l2-ctl --list-devices                # find "bcm2835-codec-encode" → usually /dev/video11
sudo fuser -v /dev/video11             # (or: sudo lsof /dev/video11) shows the holding PID

# 3) CPU load — HW encode = low CPU; software x264 pins a core near 100% (and may drop frames).
htop                                   # or: top -H
```

If, during an active recording, the h264 clock stays at 0, nothing holds `/dev/video11`, and a
core is maxed, you're on **software** encoding — drop to a smaller / lower-fps mode until the
hardware path engages. For reference, `2028×1520` 10-bit @ ~50 fps was measured here at
**~312 MHz h264 clock and ~20% CPU** — i.e. comfortably on the hardware encoder.

### Auto-mounting the USB
The app finds the drive by label under `/media/*`, `/mnt`, `/run/media/*`, or via
`/dev/disk/by-label/STEREOREC` + `/proc/mounts`. On desktop Raspberry Pi OS, udisks
auto-mounts to `/media/pi/STEREOREC`. For headless setups, either rely on udisks or add an
`/etc/fstab` entry:

```fstab
LABEL=STEREOREC  /media/pi/STEREOREC  exfat  defaults,nofail,uid=pi,gid=pi,x-systemd.automount  0  0
```

`nofail` is important so a missing drive never blocks boot — the app handles absence by
staying in `IDLE`.

### Verifying the USB filesystem
Check what filesystem is actually on the drive (matters for the FAT32 4 GB limit and the
`fsync` durability assumption in [Operational notes](#operational-notes--limitations)):

```bash
lsblk -f                       # FSTYPE, LABEL, UUID for every partition
blkid /dev/sda1                # TYPE="exfat" LABEL="STEREOREC" UUID="..."
findmnt /media/pi/STEREOREC    # FSTYPE column once it's mounted
df -T /media/pi/STEREOREC      # -T prints the filesystem type
```

Check its health/integrity — **unmount first**, never `fsck` a mounted filesystem (stop the
service first too, since it holds the mount open while recording):

```bash
sudo systemctl stop stereorec
sudo umount /media/pi/STEREOREC
sudo fsck.exfat -n /dev/sda1   # exFAT, -n = dry-run/read-only check
sudo fsck.ext4  -n /dev/sda1   # ext4
sudo fsck.vfat  -n /dev/sda1   # FAT32
```

Drop `-n` (add `-y` for ext4/vfat) to actually repair rather than just report. `dmesg | tail`
after an unclean shutdown or a suspected bad unmount often shows kernel-level filesystem
errors even before you run `fsck`. Re-run `systemctl start stereorec` once you're done.

---

## Post-processing

Each session recording is already a directly-playable file (or files — see *Continuous
recording* above) — there is no assembly step. Play or inspect them with any standard tool:

```bash
ffprobe /media/pi/STEREOREC/20260530_213100/video.ts   # container/stream info + duration
ffplay  /media/pi/STEREOREC/20260530_213100/video.ts   # quick playback check
```

If a particular player struggles with the MPEG-TS container, repackage to MP4 without
re-encoding (fast, lossless):

```bash
ffmpeg -i /media/pi/STEREOREC/20260530_213100/video.ts -c copy -fflags +genpts out.mp4
```

If the recording still looks anamorphically squeezed (see *Getting each eye undistorted* in
Camera setup below), run it through `tools/correct_aspect.py` to un-squeeze it:

```bash
python3 tools/correct_aspect.py /media/pi/STEREOREC/20260530_213100/video.ts
```

---

## Thermal protection
The recorder may run enclosed and in indirect sunlight, so `stereorec/thermal_manager.py`
polls the SoC temperature (`/sys/class/thermal/thermal_zone0/temp`) every
`temp_poll_interval_s` and tracks three zones:

| Zone | Trigger | Effect |
|---|---|---|
| `normal` | below `temp_warning_c` | thermal LED off. |
| `warning` | ≥ `temp_warning_c` | thermal LED shows the warning color; recording is unaffected. |
| `danger` | ≥ `temp_danger_c` | thermal LED shows the danger color, **and** the current recording is safely finalized** (not discarded) and the orchestrator moves to `RECOVERING`. |

Recording auto-resumes into a **new** video file once the temperature drops back below
`temp_danger_c - temp_recovery_hysteresis_c` (the hysteresis gap prevents rapidly flapping
in and out of the danger zone right at the threshold). This is treated like the other
faults in the [Recovery model](#recovery-model) table — footage up to the safe-stop is never
lost, only the small window while the CPU is too hot to trust the encode is skipped.

If `led_enabled: true` and `led_count >= 2`, pixel 1 shows the live thermal zone (see
[Status LEDs](#7-optional-neopixel-status-leds--raspberry-pi-4) / **7b**); with fewer than 2
pixels only the state indicator (pixel 0) is shown, but the safe-stop behavior and logging
still apply regardless of whether LEDs are enabled at all.

Tune `temp_warning_c` / `temp_danger_c` / `temp_recovery_hysteresis_c` / `temp_poll_interval_s`
in `config.json` — see [Configuration](#configuration) below. On a dev machine with no
readable thermal zone, the monitor logs one warning and reports `normal` permanently rather
than failing.

---

## Configuration

Defaults live in the `Config` dataclass in `stereorec/config.py`. `Config.load()` applies
overrides in this order (later wins):

1. **Defaults** baked into the dataclass.
2. **JSON file** pointed to by `STEREOREC_CONFIG` (see `config.example.json`). Any key that
   matches a dataclass field is applied; unknown keys are ignored. A malformed/unreadable
   file is silently ignored — config can never crash startup.
3. **Per-field env vars** (string-typed): `STEREOREC_USB_LABEL`, `STEREOREC_LOG_LEVEL`,
   and the boolean `STEREOREC_AUTOSTART`
   (`1`/`true`/`yes`/`on`).

#### Full field reference (default in parentheses)

**Storage / USB**
| Field | Default | Purpose |
|---|---|---|
| `usb_label` | `"STEREOREC"` | Filesystem label of the target drive. |
| `mount_roots` | `("/media","/mnt","/run/media")` | Roots scanned to resolve the labelled mount. |
| `min_free_mb` | `1024` | Refuse to start / stop recording below this free space. |
| `low_space_warn_mb` | `2048` | Emit `LOW_SPACE` below this free space. |
| `usb_poll_interval_s` | `2.0` | Hotplug poll interval. |
| `safe_mode_stop_on_usb_loss` | `true` | Reserved flag documenting the safe-stop-on-USB-loss policy (the orchestrator always safe-stops on USB loss). |

**Camera / encoder**
| Field | Default | Purpose |
|---|---|---|
| `frame_width` / `frame_height` | `2560` / `720` | Combined capture size. Should match a real advertised sensor mode (on this HAT, typically one of the anamorphic 4:3 modes — see *Camera setup*). |
| `framerate` | `30` | Capture FrameRate control. |
| `bitrate` | `12000000` | H.264 target bitrate (bits/sec). |
| `keyframe_interval_frames` | `30` | Encoder `iperiod`; one IDR/sec keeps the stream seekable. |
| `prefer_hardware_encoder` | `true` | Informational — Picamera2's `H264Encoder` already uses the Pi 4's V4L2 hardware path; there's no separate software-encoder class this switches to. |
| `camera_num` | `0` | libcamera camera index to open. |
| `expected_camera_count` | `1` | Refuse to start unless libcamera reports exactly this many cameras. |
| `tuning_file` | `null` | Optional Arducam libcamera tuning JSON (abs path or resolvable name). |
| `sensor_mode_index` | `null` | Pin an exact entry from `picam2.sensor_modes`; `null` = use requested size. |

**Video output**
| Field | Default | Purpose |
|---|---|---|
| `video_container` | `"ts"` | `"ts"` (MPEG-TS, crash-resilient) or `"mp4"` (fragmented MP4 via `-movflags frag_keyframe+empty_moov`, so a crash still leaves a mostly-playable file). |
| `video_filename_prefix` | `"video"` | Base filename; `<prefix>.<ext>`, then `<prefix>_2.<ext>`, `<prefix>_3.<ext>`, ... on each resume within a session. |

**Health / recovery**
| Field | Default | Purpose |
|---|---|---|
| `frame_stall_threshold_s` | `4.0` | No frame for this long → `CAMERA_STALL`. |
| `frame_monitor_interval_s` | `1.0` | Frame-health monitor poll interval. |
| `max_camera_restart_attempts` | `5` | Consecutive recovery attempts before `ERROR`. |
| `recovery_retry_interval_s` | `3.0` | Minimum spacing between recovery attempts. |

**Thermal** (see [Thermal protection](#thermal-protection))
| Field | Default | Purpose |
|---|---|---|
| `temp_warning_c` | `70.0` | ≥ this → warning zone (thermal LED only). |
| `temp_danger_c` | `80.0` | ≥ this → danger zone: thermal LED **and** safe-stop recording. |
| `temp_recovery_hysteresis_c` | `5.0` | Must drop below `temp_danger_c - hysteresis` before auto-resuming, to avoid flapping. |
| `temp_poll_interval_s` | `5.0` | Thermal monitor poll interval. |

**Status LEDs** (see [NeoPixel status LEDs](#7-optional-neopixel-status-leds--raspberry-pi-4))
| Field | Default | Purpose |
|---|---|---|
| `led_enabled` | `false` | Drive the NeoPixel strip as a live state/thermal indicator. |
| `led_gpio_pin` | `18` | BCM GPIO pin (`board.D<pin>`). |
| `led_count` | `2` | Pixel 0 = state, pixel 1 = thermal zone (thermal indicator needs `>= 2`). |
| `led_brightness` | `0.2` | 0–1 brightness scale. |
| `led_pixel_order` | `"GRB"` | Matches the wiring test's `led_test.py`. |
| `led_state_colors` | see `config.py` | `{state_name: (r,g,b)}` — defaults: `BOOTING` blue, `IDLE` green, `RECORDING` red, `RECOVERING` yellow, `ERROR` magenta, `SHUTDOWN` off. |
| `led_thermal_colors` | see `config.py` | `{"normal"/"warning"/"danger": (r,g,b)}` — defaults: off / amber / red. |
| `led_update_color` | `(0,60,60)` cyan | Pixel 0 color shown by `tools/check_for_update.py` while an update is in progress — see [Auto-updating over Ethernet](#auto-updating-over-ethernet). |

**Loop / logging / misc**
| Field | Default | Purpose |
|---|---|---|
| `main_loop_interval_s` | `0.5` | Orchestrator tick period. |
| `log_filename` | `"stereorec.log"` | Log file name (USB + fallback). |
| `log_max_bytes` | `5242880` | Rotating handler size cap. |
| `log_backup_count` | `5` | Rotating backups kept. |
| `log_level` | `"INFO"` | Root logger level. |
| `fallback_log_dir` | `"/run/stereorec"` | RAM (tmpfs) dir used before/without USB — see [Reducing SD-card wear](#reducing-sd-card-wear). |
| `disable_fallback_log` | `false` | Skip the fallback handler entirely (console/journald + USB only). |
| `detach_fallback_when_usb_present` | `true` | Detach the RAM fallback handler once the USB log attaches; reattach on removal. |
| `session_dirname_format` | `"%Y%m%d_%H%M%S"` | `strftime` for `session_id`. |
| `auto_start` | `true` | Begin recording on boot without a separate `START` event. |

---

## Testing & fault-injection

You can exercise most logic on a dev machine (Picamera2 absent → those subsystems
degrade gracefully and log warnings) but full recording requires the Pi + cameras.

### 0. Quick import / syntax check (any machine)
```bash
python3 -m py_compile stereorec/*.py tools/*.py
```

### 1. USB removal / reinsertion
* **Physical:** while `RECORDING`, pull the thumb drive.
  Expect within `usb_poll_interval_s`: log `USB removed`, the recorder safe-stops,
  `USB_NOT_FOUND`, state → `RECOVERING`. Reinsert → state resumes `RECORDING` into a **new**
  video file. Verify the file written before removal is intact and plays up to the removal
  point, and that `state.json`'s `video_files` lists both.
* **Simulated (no hardware):** point at a fake mount and toggle it:
  ```bash
  sudo mkdir -p /mnt/STEREOREC && sudo chown pi:pi /mnt/STEREOREC
  STEREOREC_CONFIG=./config.json python3 -m stereorec   # set mount_roots to ["/mnt"]
  # In another shell, "remove" by making it unwritable, "reinsert" by restoring:
  chmod a-w /mnt/STEREOREC      # detected as removed (write-probe fails)
  chmod u+w /mnt/STEREOREC      # detected as reinserted
  ```

### 2. Camera stall / failure
* **Stall (silent):** simulate frames stopping. Easiest is to lower the threshold and
  briefly starve the pipeline, or temporarily monkeypatch the frame callback. A clean way
  is to add a hidden test hook, but you can also unplug/disable the camera:
  ```bash
  # Disable the camera at runtime to force stall detection:
  sudo modprobe -r <camera_module>     # or physically disconnect the CSI cable
  ```
  Expect: `CAMERA_STALL` logged after `frame_stall_threshold_s`, state →
  `RECOVERING`, pipeline restart attempts. Re-enable → recording resumes.
* **Encoder failure:** set an absurd resolution/bitrate the encoder rejects, or revoke
  write perms on the session dir mid-recording; the recorder logs the fault, raises
  `CAMERA_ERROR`, and the orchestrator retries.

### 3. Process crash / power-loss recovery
* **Crash:** kill the process hard while recording:
  ```bash
  sudo systemctl kill -s SIGKILL stereorec      # or: kill -9 <pid>
  ```
  systemd (`Restart=always`) restarts it. On boot a **new** session starts; the previous
  session's file is left in place and remains playable up to the crash point.
* **Power loss:** pull power mid-recording. After reboot, confirm the prior session's file
  is playable up to the truncation point.
* **Hang (watchdog):** simulate a hang to prove `WatchdogSec` works:
  ```bash
  sudo systemctl kill -s SIGSTOP stereorec      # freeze the process
  # Within WatchdogSec (30s) systemd should kill + restart it.
  journalctl -u stereorec -e
  ```

### 4. Low disk space
* **Real:** fill the drive until free space < `min_free_mb`.
* **Simulated with a small loopback "USB":**
  ```bash
  # Create a 64 MB exFAT image labelled STEREOREC and mount it.
  dd if=/dev/zero of=/tmp/usb.img bs=1M count=64
  mkfs.exfat -n STEREOREC /tmp/usb.img
  sudo mkdir -p /mnt/STEREOREC
  sudo mount -o loop,uid=$(id -u),gid=$(id -g) /tmp/usb.img /mnt/STEREOREC
  # Set mount_roots=["/mnt"] and min_free_mb high (e.g. 60) to trigger LOW_SPACE/refusal.
  ```
  Expect: recording refused or stopped with `LOW_SPACE`, state returns to `IDLE`; the
  recorded file remains safe.

### 5. Thermal danger simulation
* Temporarily lower `temp_warning_c` / `temp_danger_c` (e.g. to a few degrees below the
  current idle CPU temp) in `config.json` and restart. Expect: the thermal LED (pixel 1, if
  `led_enabled`) shows warning then danger colors, a log line noting the danger zone, the
  current recording safely finalized (not discarded), state → `RECOVERING`. Restore the
  normal thresholds (or let the CPU cool) and confirm recording resumes into a new video
  file once back below `temp_danger_c - temp_recovery_hysteresis_c`.
* Real thermal stress (e.g. `stress-ng --cpu 4`) exercises the same path end-to-end if you'd
  rather not touch the thresholds.

### 6. Auto-update (dev machines with ethernet only)
* **Happy path:** push a trivial commit (e.g. a comment change) to the GitHub remote, then
  `sudo systemctl start stereorec-update.service` for an immediate check (or wait for the
  timer). Expect: pixel 0 turns the `led_update_color` cyan, `journalctl -u stereorec-update
  -f` shows fetch → stop → pull → pip install → py_compile → start, `stereorec` restarts into
  a new session, and `git -C /opt/stereorec log -1` shows the new commit.
* **Rollback:** push a commit with a deliberate syntax error, trigger a check the same way.
  Expect: the py_compile step fails, the log shows a rollback to the previous commit, and
  `stereorec` restarts successfully anyway (`git -C /opt/stereorec log -1` shows the *old*
  commit — confirms the rollback, not just a restart on broken code).
* **Button (if wired):** press the GPIO17 button and confirm the same check runs immediately
  (`journalctl -u stereorec-update -f`), and that a second press while one is already running
  doesn't start an overlapping run (`systemctl status stereorec-update.service` shows only one
  active invocation).

### Verifying the recording
```bash
ffprobe /media/pi/STEREOREC/<session>/video.ts     # container/stream info + duration
ffplay  /media/pi/STEREOREC/<session>/video.ts     # quick playback check
ls -la  /media/pi/STEREOREC/<session>/             # session dir contents (video.ts, video_2.ts, ...)
cat     /media/pi/STEREOREC/<session>/state.json   # video_files should match what's on disk
```

---

## Reducing SD-card wear

Maintaining the SD card's long-term health is a priority for an unattended deployment, so the
recorder never writes its steady-state logs there. The recorder never writes **video,
`state.json`, or (in steady state) logs** to the SD card — those all go to the USB drive, or,
before/without a USB drive, to RAM.

`stereorec/logging_setup.py` implements three levers, all on by default:
1. **`fallback_log_dir` defaults to `/run/stereorec`** — a tmpfs (RAM-backed) directory, not
   the SD card. The systemd unit's `RuntimeDirectory=stereorec` creates it automatically.
2. **`detach_fallback_when_usb_present: true`** (default) — once the USB log attaches
   (`attach_usb_log`), the RAM fallback handler is detached, so steady-state recording writes
   logs only to the USB drive; `detach_usb_log` reattaches the RAM fallback when the USB goes
   away, so logging continues uninterrupted.
3. **`disable_fallback_log: true`** disables the fallback handler entirely (console/journald +
   USB only), for anyone who wants to opt out of the RAM fallback altogether.

Because the RAM fallback is volatile (cleared on reboot/power loss), `attach_usb_log` also
calls `flush_fallback_to_usb`, which copies any log files still sitting in `fallback_log_dir`
onto the USB drive (as `<session>/logs/stereorec.fallback*`) as soon as it mounts — so
whatever accumulated in RAM before the drive was available (typically just the boot window)
is preserved rather than lost to the next power cycle.

journald (`StandardError=journal`) is a separate potential SD writer if persistent journaling
is enabled system-wide; optionally set `Storage=volatile` in `/etc/systemd/journald.conf` to
keep that off the SD card too.

**Resulting behavior:** once the `STEREOREC` USB mounts, no log writes hit the SD card at all.
The SD is touched only during the boot window before the USB mounts (and while it's absent),
and even then the writes land on tmpfs (RAM), not flash — `disable_fallback_log: true` removes
even that.

---

## Potential improvements

> **Status: design discussion — nothing changed in code.** These are directions to
> investigate **if you are seeing missing chunks of footage or corrupted video**, especially
> at regular intervals. They revisit the recording model; read alongside
> [Continuous recording](#continuous-recording-one-file-per-session).

### Segmented recording as a reliability upgrade
The current build records **one continuous file per session**. An alternative is to split
recording into many fixed-length (e.g. 60-second) **independently-playable MPEG-TS segments**:

```
segment_000001.ts
segment_000002.ts
segment_000003.ts
```

Each segment would be encoded by a fresh encoder (starting on a keyframe/IDR), written to a
`*.part` temp file, and only **atomically renamed** to its final `segment_NNNNNN.ts` name once
complete (with `fsync(file)` → `rename()` → `fsync(dir)` for durability), then appended to a
`manifest.json`. The payoff is a stronger guarantee: a crash / USB pull / encoder fault could
only ever lose the *current* `.part` segment — every previously completed segment is already
durable and never overwritten.

### The costs that guarantee brings
That reliability is not free, and its mechanics can *look exactly like* the corruption /
missing-footage problems you may be chasing:

1. **A gap at every segment boundary.** A fresh encoder per segment means `stop_encoder()` at
   the end of one segment and `start_encoder()` for the next. Frames that arrive in the short
   window between those two calls are **not written to any file** — a small dropout every
   `segment_seconds`. The Pi 4's hardware H.264 encoder makes that restart cheaper than on a
   Pi 5, but the gap still exists at every boundary. **Regularly spaced** missing frames are
   the signature of this.
2. **A fault discards the whole in-progress segment.** On USB loss, camera stall, or encoder
   fault the current `.part` is thrown away. A single transient stall therefore costs up to a
   full `segment_seconds` of video — which reads as a "big missing segment."
3. **Join artifacts on merge.** Independently-encoded TS segments concatenated with
   stream-copy can glitch some players at the joins (timestamp discontinuities), needing a
   `+genpts` flag or a re-encode.

### Are we solving a problem we actually have?
Segmentation optimizes for **surviving unclean power loss with near-zero footage loss.** If
this deployment rarely loses power unexpectedly (stable supply / UPS / graceful shutdown),
that guarantee may be buying very little while the costs above would actively degrade the
footage. Worth deciding explicitly rather than adopting by default.

### Options to investigate (roughly increasing effort)
* **A — Longer segments (if segmenting).** Larger `segment_seconds` (e.g. 300–600) → fewer
  boundaries → fewer boundary gaps and fewer merge joins. Trade-off: a discarded partial loses
  more video.
* **B — Keep the partial instead of discarding it.** On fault, finalize (save) the current
  in-progress file rather than deleting it — a truncated TS is still largely playable. Removes
  the "lose a whole chunk per stall" behavior.
* **C — Split without restarting the encoder.** Let one continuous encode be split at keyframe
  boundaries by the muxer (ffmpeg's `-f segment` / `segment_time`, or Picamera2 output
  splitting) instead of tearing the encoder down each segment. Gets crash-resilient chunks
  **without** the per-boundary gap. Most promising if boundary dropouts are the real issue.
* **D — Keep the continuous single file (today's model).** Simplest output, no boundary gaps
  or join artifacts — at the cost of the strong "never lose completed footage on power loss"
  guarantee (a crash can still cost the tail of the file).

### Confirm the cause before changing anything
* Compare the recorded duration against wall-clock: **evenly spaced** dropouts point to
  boundary gaps (only relevant if you have adopted segmenting); **large holes** point to
  fault-driven restarts.
* `grep` the logs for `CAMERA_STALL`, `Failed to publish`, and `produced no/invalid data` to
  see which failure mode is actually firing.
* `ffprobe` the file (or individual chunks) around a bad spot: corruption *inside* the stream
  implicates the encoder / CPU load; corruption only *at joins* implicates a boundary path.

---

## Operational notes & limitations

* **Reliability over connectivity / performance.** Picamera2 being absent will not crash the process; subsystems degrade and log.
* **FAT32 4 GB limit** caps a single video file at 4 GB — a long recording run will exceed it; prefer exFAT/ext4. (Multi-file-per-session on recovery, above, does not work around this — each individual file is still capped.)
* **Filesystem durability** assumes the USB honors `fsync`. Cheap drives that lie about
  flush can still lose the tail of the recording on power loss; the design minimizes but
  cannot fully eliminate that on dishonest hardware.
* **New session per boot.** `session_id` is the boot timestamp, so each start creates a
  fresh session folder; previous sessions are left intact, never resumed into.
* **Runs as root.** The systemd unit has no `User=` directive, so the service runs as root —
  required for the NeoPixel `rpi_ws281x` PWM/DMA backend and for reliable camera/GPIO access.
* **Anamorphic capture.** The recorder writes the raw ArduChip frame as captured, without
  live aspect-ratio correction; see *Getting each eye undistorted* and `tools/correct_aspect.py`
  if the footage needs to be un-squeezed afterward.
* **Thermal safe-stop is footage-preserving but not seamless.** A sustained danger-zone
  temperature ends the current file and waits for the CPU to cool before resuming into a new
  one — by design, since the alternative is risking the encode during a thermal-throttle event.
* **Auto-update stops and restarts recording.** If `stereorec-update.timer` is enabled and the
  device is on a network with a newer commit available, it *will* stop the current recording
  (safely, finalizing the file), update, and restart — see
  [Auto-updating over Ethernet](#auto-updating-over-ethernet). This is only safe under this
  build's explicit assumption that ethernet is connected during development, not during an
  unattended field recording; disable the timer before any deployment where that might not hold.
