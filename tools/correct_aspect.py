#!/usr/bin/env python3
"""correct_aspect.py -- offline correction for the ArduChip's anamorphic stereo capture.

On this HAT, both stereo eyes are packed anamorphically into a standard
single-sensor-shaped frame (e.g. 2028x1520, itself 4:3) rather than a visibly
wide combined frame. StereoRec records that raw frame as-is; this script
un-squeezes it afterward by stretching its width, matching the correction
empirically validated during bring-up (2028x1520 -> 5404x1520).

This is a deliberate offline/post-processing step -- doing it live during
recording would add a per-frame resize cost and move the encoder off the
sensor-mode-matched resolutions the Pi 4 hardware-encoder throughput numbers
in README_pi4.md are based on.

Usage:
    python3 correct_aspect.py video.ts
    python3 correct_aspect.py video.ts -o corrected.mp4
    python3 correct_aspect.py video.ts --squeeze 2.665
    python3 correct_aspect.py video.ts --target-width 5404
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from typing import Tuple

DEFAULT_SQUEEZE = 2.665  # empirically validated for 2028x1520 -> 5404x1520


def eprint(*args) -> None:
    print(*args, file=sys.stderr)


def ffmpeg_bin() -> str:
    path = shutil.which("ffmpeg")
    if not path:
        eprint("ERROR: ffmpeg not found on PATH. Install it (e.g. apt install ffmpeg).")
        sys.exit(2)
    return path


def ffprobe_bin() -> str:
    path = shutil.which("ffprobe")
    if not path:
        eprint("ERROR: ffprobe not found on PATH. Install it (e.g. apt install ffmpeg).")
        sys.exit(2)
    return path


def probe_size(path: str) -> Tuple[int, int]:
    result = subprocess.run(
        [
            ffprobe_bin(), "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height", "-of", "json", path,
        ],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        eprint(f"ERROR: ffprobe failed: {result.stderr.strip()}")
        sys.exit(1)
    streams = json.loads(result.stdout).get("streams") or []
    if not streams:
        eprint("ERROR: no video stream found.")
        sys.exit(1)
    return int(streams[0]["width"]), int(streams[0]["height"])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("input", help="Path to the recorded video (e.g. video.ts)")
    parser.add_argument("-o", "--output", help="Output path (default: <input>_corrected.mp4)")
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--squeeze", type=float, default=None,
        help=f"Width multiplier to apply (default {DEFAULT_SQUEEZE})",
    )
    group.add_argument(
        "--target-width", type=int, default=None,
        help="Exact output width (height stays unchanged)",
    )
    parser.add_argument(
        "--crf", type=int, default=18,
        help="x264 CRF for the corrected output (default 18; lower = higher quality)",
    )
    args = parser.parse_args()

    if not os.path.isfile(args.input):
        eprint(f"ERROR: not a file: {args.input}")
        return 2

    width, height = probe_size(args.input)
    if args.target_width is not None:
        target_width = args.target_width
    else:
        squeeze = args.squeeze if args.squeeze is not None else DEFAULT_SQUEEZE
        target_width = round(width * squeeze)

    output = args.output or (os.path.splitext(args.input)[0] + "_corrected.mp4")

    cmd = [
        ffmpeg_bin(), "-y", "-i", args.input,
        "-vf", f"scale={target_width}:{height}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", str(args.crf),
        "-fflags", "+genpts", output,
    ]
    print(f"Source: {width}x{height}  ->  corrected: {target_width}x{height}")
    eprint("Running:", " ".join(cmd))
    result = subprocess.run(cmd)
    if result.returncode == 0:
        print(f"OK: wrote {output}")
    else:
        eprint(f"ffmpeg failed (exit {result.returncode}).")
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
