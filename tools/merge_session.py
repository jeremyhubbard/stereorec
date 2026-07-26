#!/usr/bin/env python3
"""merge_session.py — merge a recorded session's TS segments into a single MP4.

Reads ``manifest.json`` (falling back to a directory scan), validates that every listed
segment exists and is non-trivial, detects missing/corrupt segments, builds an FFmpeg
concat list, and stream-copies the segments into one MP4 — no re-encoding:

    ffmpeg -f concat -safe 0 -i segments.txt -c copy output.mp4

Because every segment is an independent MPEG-TS file beginning on a keyframe, concat +
stream copy is lossless and fast. Use ``--reencode`` only if a player chokes on the
copied stream.

Usage:
    python3 merge_session.py /media/pi/STEREOREC/20260530_213100
    python3 merge_session.py <session_dir> -o /tmp/out.mp4
    python3 merge_session.py <session_dir> --skip-missing
    python3 merge_session.py <session_dir> --verify   # probe each segment, don't merge
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from typing import List, Optional, Tuple

SEGMENT_RE = re.compile(r"^segment_(\d{6})\.ts$")


def eprint(*args) -> None:
    print(*args, file=sys.stderr)


def ffmpeg_bin() -> str:
    path = shutil.which("ffmpeg")
    if not path:
        eprint("ERROR: ffmpeg not found on PATH. Install it (e.g. apt install ffmpeg).")
        sys.exit(2)
    return path


def ffprobe_bin() -> Optional[str]:
    return shutil.which("ffprobe")


def load_manifest_segments(session_dir: str) -> Optional[List[str]]:
    manifest_path = os.path.join(session_dir, "manifest.json")
    try:
        with open(manifest_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        segs = data.get("segments")
        if isinstance(segs, list):
            return [str(s) for s in segs]
    except (OSError, ValueError):
        return None
    return None


def scan_segments(session_dir: str) -> List[str]:
    try:
        names = [n for n in os.listdir(session_dir) if SEGMENT_RE.match(n)]
    except OSError:
        return []
    names.sort()
    return names


def probe_ok(path: str) -> bool:
    """Return True if ffprobe can read at least one video stream from the file."""
    probe = ffprobe_bin()
    if not probe:
        # No ffprobe available; fall back to a size sanity check.
        try:
            return os.path.getsize(path) > 1316
        except OSError:
            return False
    try:
        result = subprocess.run(
            [probe, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_type", "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=30,
        )
        return result.returncode == 0 and "video" in result.stdout
    except (subprocess.SubprocessError, OSError):
        return False


def validate(session_dir: str, segments: List[str], do_probe: bool
             ) -> Tuple[List[str], List[str], List[str]]:
    """Return (present_ok, missing, corrupt)."""
    present_ok: List[str] = []
    missing: List[str] = []
    corrupt: List[str] = []
    for name in segments:
        path = os.path.join(session_dir, name)
        if not os.path.isfile(path):
            missing.append(name)
            continue
        if do_probe and not probe_ok(path):
            corrupt.append(name)
            continue
        present_ok.append(name)
    return present_ok, missing, corrupt


def detect_index_gaps(segments: List[str]) -> List[int]:
    """Return the list of segment indices that are absent from an otherwise contiguous run."""
    indices = sorted(int(m.group(1)) for m in
                     (SEGMENT_RE.match(s) for s in segments) if m)
    if not indices:
        return []
    gaps: List[int] = []
    for expected in range(indices[0], indices[-1] + 1):
        if expected not in indices:
            gaps.append(expected)
    return gaps


def build_concat_file(session_dir: str, segments: List[str]) -> str:
    fd, concat_path = tempfile.mkstemp(prefix="segments_", suffix=".txt")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        for name in segments:
            abspath = os.path.abspath(os.path.join(session_dir, name))
            # Escape single quotes per ffmpeg concat demuxer rules.
            safe = abspath.replace("'", "'\\''")
            fh.write(f"file '{safe}'\n")
    return concat_path


def merge(session_dir: str, segments: List[str], output: str, reencode: bool) -> int:
    concat_path = build_concat_file(session_dir, segments)
    try:
        cmd = [ffmpeg_bin(), "-y", "-f", "concat", "-safe", "0", "-i", concat_path]
        if reencode:
            cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20"]
        else:
            cmd += ["-c", "copy"]
        # mpegts -> mp4 may need this for clean timestamps with stream copy.
        cmd += ["-fflags", "+genpts", output]
        eprint("Running:", " ".join(cmd))
        result = subprocess.run(cmd)
        return result.returncode
    finally:
        try:
            os.unlink(concat_path)
        except OSError:
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description="Merge a StereoRec session into one MP4.")
    parser.add_argument("session_dir", help="Path to the session directory")
    parser.add_argument("-o", "--output", help="Output MP4 path "
                        "(default: <session_dir>/<session_id>.mp4)")
    parser.add_argument("--skip-missing", action="store_true",
                        help="Merge available segments even if some are missing/corrupt")
    parser.add_argument("--no-probe", action="store_true",
                        help="Skip ffprobe validation of each segment (faster)")
    parser.add_argument("--reencode", action="store_true",
                        help="Re-encode instead of stream-copy (only if copy fails)")
    parser.add_argument("--verify", action="store_true",
                        help="Validate segments and report; do not merge")
    args = parser.parse_args()

    session_dir = os.path.abspath(args.session_dir)
    if not os.path.isdir(session_dir):
        eprint(f"ERROR: not a directory: {session_dir}")
        return 2

    segments = load_manifest_segments(session_dir)
    if segments is None:
        eprint("WARNING: manifest.json missing/unreadable — scanning directory instead.")
        segments = scan_segments(session_dir)
    if not segments:
        eprint("ERROR: no segments found.")
        return 1

    present_ok, missing, corrupt = validate(session_dir, segments, do_probe=not args.no_probe)
    gaps = detect_index_gaps(segments)

    print(f"Session:    {session_dir}")
    print(f"Listed:     {len(segments)} segment(s)")
    print(f"Valid:      {len(present_ok)}")
    if missing:
        eprint(f"MISSING ({len(missing)}): {', '.join(missing)}")
    if corrupt:
        eprint(f"CORRUPT ({len(corrupt)}): {', '.join(corrupt)}")
    if gaps:
        eprint(f"INDEX GAPS: {', '.join(f'{g:06d}' for g in gaps)}")

    if args.verify:
        return 0 if (not missing and not corrupt) else 1

    if (missing or corrupt) and not args.skip_missing:
        eprint("Refusing to merge with missing/corrupt segments. "
               "Re-run with --skip-missing to proceed with available segments.")
        return 1

    if not present_ok:
        eprint("ERROR: no valid segments to merge.")
        return 1

    output = args.output or os.path.join(session_dir,
                                         f"{os.path.basename(session_dir)}.mp4")
    rc = merge(session_dir, present_ok, output, reencode=args.reencode)
    if rc == 0:
        print(f"OK: wrote {output} ({len(present_ok)} segments)")
    else:
        eprint(f"ffmpeg failed (exit {rc}).")
        if not args.reencode:
            eprint("Try --reencode if the source has timestamp discontinuities.")
    return rc


if __name__ == "__main__":
    sys.exit(main())
