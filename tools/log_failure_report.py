#!/usr/bin/env python3
"""log_failure_report.py -- capture a diagnostic bundle when stereorec.service fails.

Run by systemd/stereorec-failure-report@.service, which stereorec.service triggers
via OnFailure= any time it stops with Result != success (watchdog timeout, OOM
kill, an uncaught fatal exception exit, ...). Restart=always still brings the
service back up on its own -- this runs alongside that restart, not instead of
it, purely to capture *why* before the evidence ages out: by the time systemd
notices the failure the offending process is already gone (frozen or dead), so
it can't log its own cause. Only systemd's own unit state and the journal/dmesg
still have that context, and only briefly.

Usage: log_failure_report.py <failed-unit-name>   (systemd passes %i automatically)
"""

from __future__ import annotations

import datetime
import logging
import os
import shutil
import subprocess
import sys

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_DIR)

from stereorec.config import Config  # noqa: E402
from stereorec.usb_manager import resolve_mount  # noqa: E402
from stereorec.util import (  # noqa: E402
    ensure_dir,
    free_space_mb,
    read_cpu_temp_c,
    read_recent_kernel_log,
    read_throttled_flags,
)

logger = logging.getLogger("log_failure_report")

REPORT_SUBDIR = "failure_reports"
JOURNAL_LINES = 200
KERNEL_LOG_LINES = 60

SYSTEMCTL_SHOW_PROPS = ",".join(
    [
        "Result",  # the authoritative "why": success/watchdog/oom-kill/signal/exit-code/...
        "ActiveState",
        "SubState",
        "ExecMainStatus",
        "ExecMainCode",
        "ExecMainPID",
        "NRestarts",
        "ActiveEnterTimestamp",
        "InactiveEnterTimestamp",
    ]
)


def _run(cmd, timeout: float) -> str:
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (subprocess.SubprocessError, OSError) as exc:
        return f"<failed to run {' '.join(cmd)}: {exc}>"
    output = (result.stdout or "").strip()
    if result.returncode != 0:
        output = (output + "\n" if output else "") + (result.stderr or "").strip()
    return output or "<no output>"


def _parse_systemctl_show(output: str) -> dict:
    props = {}
    for line in output.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            props[key] = value
    return props


def build_report(unit: str, config: Config) -> str:
    show_output = _run(["systemctl", "show", unit, "-p", SYSTEMCTL_SHOW_PROPS], timeout=10)
    props = _parse_systemctl_show(show_output)

    lines = [
        f"=== StereoRec failure report: {unit} ===",
        f"Generated: {datetime.datetime.now().isoformat()}",
        "",
    ]

    if props.get("Result") == "start-limit-hit":
        # The one Result value that means Restart=always has stopped mattering:
        # systemd hit StartLimitBurst (too many failures in too short a window)
        # and gave up on this unit entirely -- it will NOT come back on its own,
        # unlike every other failure mode this report covers. Called out here,
        # loudly, since it's otherwise just one line buried in the raw show
        # output below and easy to miss when skimming.
        lines += [
            "*** Result=start-limit-hit -- systemd gave up restarting this unit after too many",
            "*** failures in a short window (StartLimitIntervalSec/StartLimitBurst).",
            "*** Restart=always does NOT apply here. The unit stays stopped until someone runs",
            "***   systemctl reset-failed " + unit + " && systemctl start " + unit,
            "*** (or reboots). This is the 'LEDs dark and staying dark' field symptom.",
            "",
        ]

    lines += [
        "--- systemctl show (Result is the authoritative 'why') ---",
        show_output,
        "",
        f"--- journalctl -u {unit} (last {JOURNAL_LINES} lines) ---",
        _run(["journalctl", "-u", unit, "-n", str(JOURNAL_LINES), "--no-pager"], timeout=15),
        "",
        f"--- dmesg -T (last {KERNEL_LOG_LINES} lines) ---",
        read_recent_kernel_log(max_lines=KERNEL_LOG_LINES) or "<unavailable>",
        "",
        "--- System snapshot ---",
    ]

    temp = read_cpu_temp_c()
    throttled = read_throttled_flags()
    free_mb = (
        free_space_mb(config.fallback_log_dir) if os.path.isdir(config.fallback_log_dir) else None
    )
    lines.append(f"cpu_temp={f'{temp:.1f}C' if temp is not None else 'unknown'}")
    lines.append(f"throttled={throttled or 'unknown'}")
    lines.append(
        f"free_mb({config.fallback_log_dir})="
        f"{f'{free_mb:.0f}' if free_mb is not None else 'unknown'}"
    )
    lines.append("")
    lines.append("--- free -h ---")
    lines.append(_run(["free", "-h"], timeout=5))

    return "\n".join(lines) + "\n"


def write_report(config: Config, unit: str, report: str) -> str:
    ensure_dir(config.fallback_log_dir)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_unit = unit.replace("/", "_")
    filename = f"stereorec-failure_{safe_unit}_{stamp}.log"
    path = os.path.join(config.fallback_log_dir, filename)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(report)
    return path


def flush_to_usb(config: Config, src_path: str) -> None:
    mount = resolve_mount(config)
    if not mount:
        return
    dest_dir = os.path.join(mount, "STEREOREC", REPORT_SUBDIR)
    try:
        ensure_dir(dest_dir)
        shutil.copyfile(src_path, os.path.join(dest_dir, os.path.basename(src_path)))
    except OSError as exc:
        logger.warning("Could not copy failure report to USB: %s", exc)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    if len(sys.argv) < 2:
        logger.error("Usage: log_failure_report.py <failed-unit-name>")
        return 1
    unit = sys.argv[1]

    config = Config.load()
    report = build_report(unit, config)
    # Also goes to the journal via StandardOutput=journal on the oneshot unit,
    # so it's visible even if the fallback_log_dir/USB write below fails.
    logger.info("Failure report for %s:\n%s", unit, report)

    path = write_report(config, unit, report)
    flush_to_usb(config, path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
