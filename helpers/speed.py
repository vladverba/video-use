"""Change a finished render's playback speed without distorting voice.

Uses ffmpeg `setpts` (video) + `atempo` (audio) — a pitch-preserving time
stretch, not a naive resample (which would raise/lower pitch like a
chipmunk/slowmo effect). `atempo` only accepts 0.5-2.0 per stage, so factors
outside that range are chained across multiple `atempo` filters.

Always forces a constant frame rate (CFR) on the output. `setpts` alone (no
explicit output rate) produces a variable-frame-rate file — confirmed via
ffprobe showing `r_frame_rate` and `avg_frame_rate` mismatched after a plain
`setpts` speed change. VFR files are a known cause of black-frame/import
glitches in mobile tools (Instagram Edits, TikTok, CapCut all confirmed
affected in the wild) even though the file plays back fine in ffplay/QuickTime.
Default target rate is the source's own frame rate (probed, not derived from
the speed factor) since that's the safest, most standard choice.

Usage:
    python helpers/speed.py final.mp4 -o final_1.1x.mp4 --speed 1.1
    python helpers/speed.py final.mp4 -o final_1.1x.mp4 --speed 1.1 --fps 30
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


def probe_source_fps(video: Path) -> str | None:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=avg_frame_rate,r_frame_rate",
             "-of", "json", str(video)],
            capture_output=True, text=True, check=True,
        )
        streams = json.loads(out.stdout).get("streams") or []
        if not streams:
            return None
        for field in ("avg_frame_rate", "r_frame_rate"):
            value = streams[0].get(field)
            if value and value != "0/0":
                return value
    except (subprocess.CalledProcessError, json.JSONDecodeError, OSError):
        return None
    return None


def atempo_chain(speed: float) -> str:
    """Chain atempo stages so each factor stays within ffmpeg's 0.5-2.0 range."""
    stages: list[float] = []
    remaining = speed
    while remaining > 2.0:
        stages.append(2.0)
        remaining /= 2.0
    while remaining < 0.5:
        stages.append(0.5)
        remaining /= 0.5
    stages.append(remaining)
    return ",".join(f"atempo={s:.6f}" for s in stages)


def apply_speed(
    input_path: Path,
    output_path: Path,
    speed: float,
    fps: str | None = None,
) -> None:
    out_fps = fps or probe_source_fps(input_path) or "30"

    cmd = [
        "ffmpeg", "-y",
        "-i", str(input_path),
        "-filter_complex",
        f"[0:v]setpts=PTS/{speed}[v];[0:a]{atempo_chain(speed)}[a]",
        "-map", "[v]", "-map", "[a]",
        "-r", out_fps, "-fps_mode", "cfr",
        "-c:v", "libx264", "-preset", "fast", "-crf", "18", "-pix_fmt", "yuv420p",
        "-bsf:v", "filter_units=remove_types=6",
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
        "-movflags", "+faststart",
        str(output_path),
    ]
    subprocess.run(cmd, check=True)


def main() -> None:
    ap = argparse.ArgumentParser(description="Change playback speed, pitch-preserved, CFR-safe")
    ap.add_argument("input", type=Path, help="Path to input video")
    ap.add_argument("-o", "--output", type=Path, required=True, help="Output video path")
    ap.add_argument("--speed", type=float, required=True, help="Speed multiplier, e.g. 1.1")
    ap.add_argument(
        "--fps", type=str, default=None,
        help="Force this output frame rate. Default: probe the input's own rate.",
    )
    args = ap.parse_args()

    if not args.input.exists():
        sys.exit(f"input not found: {args.input}")
    if args.speed <= 0:
        sys.exit("speed must be greater than zero")

    apply_speed(args.input.resolve(), args.output.resolve(), args.speed, args.fps)
    print(f"done: {args.output}")


if __name__ == "__main__":
    main()
