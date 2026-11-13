"""Render a video from an EDL.

Implements the HEURISTICS render pipeline in the correct order:

  1. Per-segment extract with color grade + 30ms audio fades baked in
  2. Lossless -c copy concat into base.mp4
  3. If overlays or subtitles: single filter graph that overlays animations
     (with PTS shift so frame 0 lands at the overlay window start)
     and applies `subtitles` filter LAST → final.mp4

Optionally builds a master SRT from the per-source transcripts + EDL
output-timeline offsets, applies the proven force_style (2-word
UPPERCASE chunks, Helvetica 18 Bold, MarginV=35).

Usage:
    python helpers/render.py <edl.json> -o final.mp4
    python helpers/render.py <edl.json> -o preview.mp4 --preview
    python helpers/render.py <edl.json> -o final.mp4 --build-subtitles
    python helpers/render.py <edl.json> -o final.mp4 --no-subtitles
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from fractions import Fraction
from pathlib import Path

try:
    from grade import get_preset, auto_grade_for_clip  # same directory
except Exception:
    def get_preset(name: str) -> str:
        return ""

    def auto_grade_for_clip(video, start=0.0, duration=None, verbose=False):  # type: ignore
        return "eq=contrast=1.03:saturation=0.98", {}

# speed.py is stdlib-only and required, so make the sibling import work even
# when render.py is loaded by path (tests) rather than run from helpers/.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from speed import atempo_chain  # noqa: E402


# -------- Subtitle style (bold-overlay, proven at 1920×1080 and 1080×1920) --
#
# MarginV is NOT taste — it is a platform safe-zone rule.
# TikTok / IG Reels / Shorts UI (caption, username, music, right-rail actions)
# covers roughly the bottom ~25–30% of a 1080×1920 frame. Captions placed near
# the bottom edge get clipped or obscured by the UI. libass auto-scales the
# render canvas relative to PlayResY=288, so MarginV=90 lands the caption
# baseline roughly 30% up from the bottom on any aspect — clear of the UI on
# every major vertical-video platform. Do not drop this below ~75 without a
# specific reason.
SUB_FORCE_STYLE = (
    "FontName=Helvetica,FontSize=18,Bold=1,"
    "PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,BackColour=&H00000000,"
    "BorderStyle=1,Outline=2,Shadow=0,"
    "Alignment=2,MarginV=90"
)

# -------- Helpers ------------------------------------------------------------


def _prefer_native(name: str) -> str:
    """Prefer the Apple Silicon Homebrew build when installed.

    An x86_64 ffmpeg under Rosetta has no VideoToolbox hardware access and
    runs libx265 ~3x slower than the native arm64 build. Falls back to
    whatever `name` resolves to on PATH.
    """
    native = Path("/opt/homebrew/bin") / name
    return str(native) if native.exists() else name


FFMPEG = _prefer_native("ffmpeg")
FFPROBE = _prefer_native("ffprobe")


def run(cmd: list[str], quiet: bool = False) -> None:
    if not quiet:
        print(f"  $ {' '.join(str(c) for c in cmd[:6])}{' …' if len(cmd) > 6 else ''}")
    subprocess.run(cmd, check=True)


def resolve_grade_filter(grade_field: str | None) -> str:
    """The EDL's 'grade' field can be a preset name, a raw ffmpeg filter, or 'auto'.

    Returns the filter string to embed into the per-segment -vf chain.
    For 'auto', returns the sentinel "__AUTO__" which is resolved per-segment.
    """
    if not grade_field:
        return ""
    if grade_field == "auto":
        return "__AUTO__"
    # Preset names are short identifiers, filter strings contain '=' or ','.
    if re.fullmatch(r"[a-zA-Z0-9_\-]+", grade_field):
        try:
            return get_preset(grade_field)
        except KeyError:
            print(f"warning: unknown preset '{grade_field}', using as raw filter")
            return grade_field
    return grade_field


def resolve_path(maybe_path: str, base: Path) -> Path:
    """Resolve a path that may be absolute or relative to `base`."""
    p = Path(maybe_path)
    if p.is_absolute():
        return p
    return (base / p).resolve()


# -------- HDR → SDR tone mapping (HLG / PQ sources) --------------------------
#
# iPhone defaults to HLG HDR in Rec.2020 (and many mirrorless cameras ship PQ).
# If the source is HDR and we only downconvert bit depth (yuv420p10le → yuv420p)
# without tone-mapping, the output is 8-bit but still carries HLG/PQ transfer
# metadata. Players that honor the metadata (screen recorders, most social
# upload re-encodes) interpret 8-bit values in an HDR container and the result
# looks oversaturated / blown out. QuickTime on macOS can hide this locally —
# screen recording and uploaded renders cannot.
#
# Fix: detect HDR via color_transfer and prepend a zscale+tonemap chain to the
# vf graph so the output is clean Rec.709 SDR.

HDR_TRANSFERS = {"smpte2084", "arib-std-b67"}  # PQ (HDR10) and HLG

TONEMAP_CHAIN = (
    "zscale=t=linear:npl=100,"
    "format=gbrpf32le,"
    "zscale=p=bt709,"
    "tonemap=tonemap=hable:desat=0,"
    "zscale=t=bt709:m=bt709:r=tv,"
    "format=yuv420p"
)


def is_hdr_source(video: Path) -> bool:
    """Return True if the source uses a PQ or HLG transfer function."""
    try:
        out = subprocess.run(
            [FFPROBE, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=color_transfer",
             "-of", "default=noprint_wrappers=1:nokey=1", str(video)],
            capture_output=True, text=True, check=True,
        )
        return out.stdout.strip() in HDR_TRANSFERS
    except subprocess.CalledProcessError:
        return False


# -------- Final-quality encode (HDR-preserving) -----------------------------
#
# Final renders keep the source's look instead of downconverting: HEVC Main 10,
# the source's own color tags (HLG/BT.2020 for iPhone HDR footage), `hvc1` so
# iOS/QuickTime plays it. Encoded on the Mac's hardware HEVC encoder at 1.25x
# the source bitrate: measured on 4K60 iPhone footage at VMAF 95.5 vs libx265
# CRF 13's 95.3, ~14x faster (6s vs 82s per 5s of source).

FINAL_BITRATE_FACTOR = 1.25
FINAL_MIN_BITRATE = 12_000_000  # bits/s; floor for low-bitrate sources


def probe_color_tags(video: Path) -> dict[str, str]:
    """Source color tags that are actually set (unknown/missing omitted)."""
    try:
        out = subprocess.run(
            [FFPROBE, "-v", "error", "-select_streams", "v:0",
             "-show_entries",
             "stream=color_primaries,color_transfer,color_space,color_range",
             "-of", "json", str(video)],
            capture_output=True, text=True, check=True,
        )
        streams = json.loads(out.stdout).get("streams") or []
    except (subprocess.CalledProcessError, json.JSONDecodeError, OSError):
        return {}
    if not streams:
        return {}
    return {
        k: v for k, v in streams[0].items()
        if k in ("color_primaries", "color_transfer", "color_space", "color_range")
        and v and v != "unknown"
    }


def probe_video_bitrate(video: Path) -> int | None:
    """Source video stream bitrate in bits/s, falling back to the container's."""
    try:
        out = subprocess.run(
            [FFPROBE, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=bit_rate:format=bit_rate",
             "-of", "json", str(video)],
            capture_output=True, text=True, check=True,
        )
        data = json.loads(out.stdout)
    except (subprocess.CalledProcessError, json.JSONDecodeError, OSError):
        return None
    for value in ((data.get("streams") or [{}])[0].get("bit_rate"),
                  (data.get("format") or {}).get("bit_rate")):
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def final_video_args(source: Path) -> list[str]:
    """ffmpeg video-encode args for a final render of a segment of `source`.

    Color tags are copied from the source, never assumed, so an SDR source
    is not mislabeled as HDR (or vice versa). Tags the source doesn't set
    are left unset. `-allow_sw 1` lets the encode still succeed (slowly) on
    a machine without the hardware encoder.
    """
    tags = probe_color_tags(source)
    flags: list[str] = []
    for tag, flag in (("color_primaries", "-color_primaries"),
                      ("color_transfer", "-color_trc"),
                      ("color_space", "-colorspace"),
                      ("color_range", "-color_range")):
        if tag in tags:
            flags += [flag, tags[tag]]
    bitrate = max(int((probe_video_bitrate(source) or 0) * FINAL_BITRATE_FACTOR),
                  FINAL_MIN_BITRATE)
    return [
        "-c:v", "hevc_videotoolbox", "-allow_sw", "1",
        "-profile:v", "main10", "-pix_fmt", "p010le",
        "-b:v", str(bitrate),
        *flags,
        "-tag:v", "hvc1",
    ]


def is_portrait_source(video: Path) -> bool:
    """Return True if the displayed video is portrait, including rotation."""
    try:
        out = subprocess.run(
            [FFPROBE, "-v", "error", "-select_streams", "v:0",
             "-show_entries",
             "stream=width,height:stream_side_data=rotation",
             "-of", "json", str(video)],
            capture_output=True, text=True, check=True,
        )
        streams = json.loads(out.stdout).get("streams") or []
        if not streams:
            return False
        stream = streams[0]
        w, h = int(stream["width"]), int(stream["height"])

        # ffmpeg autorotates display-matrix side data before applying filters.
        # Swap coded dimensions for quarter-turns so the scale axis is selected
        # from the dimensions the filter actually sees. A plain metadata tag is
        # intentionally ignored because it does not guarantee autorotation.
        rotation = 0
        for side_data in stream.get("side_data_list") or []:
            if side_data.get("rotation") is not None:
                rotation = side_data["rotation"]
                break
        if int(round(float(rotation))) % 360 in (90, 270):
            w, h = h, w
        return h > w
    except (
        subprocess.CalledProcessError,
        json.JSONDecodeError,
        OSError,
        OverflowError,
        KeyError,
        TypeError,
        ValueError,
    ):
        return False


def parse_fps(value: str) -> str:
    """Validate and canonicalize an ffmpeg frame rate."""
    text = value.strip()
    if len(text) > 32 or not re.fullmatch(
        r"(?:[0-9]+(?:\.[0-9]+)?|[0-9]+/[0-9]+)", text
    ):
        raise argparse.ArgumentTypeError(
            "FPS must be a positive number or rational, e.g. 30 or 30000/1001"
        )
    try:
        rate = Fraction(text)
    except (ValueError, ZeroDivisionError) as exc:
        raise argparse.ArgumentTypeError(
            "FPS must be a positive number or rational, e.g. 30 or 30000/1001"
        ) from exc
    if rate <= 0:
        raise argparse.ArgumentTypeError("FPS must be greater than zero")
    # FFmpeg stores video rates as AVRational (signed 32-bit components).
    # Bounding the reduced fraction keeps every accepted canonical value safe
    # for ffmpeg and makes parse_fps(parse_fps(value)) idempotent.
    max_component = 2_147_483_647
    if rate.numerator > max_component or rate.denominator > max_component:
        raise argparse.ArgumentTypeError("FPS precision or magnitude is too large")
    return f"{rate.numerator}/{rate.denominator}"


# iPhone footage is nominally 30/1 but its timestamps jitter, so the average
# lands at e.g. 961000/32041 (29.993, ~0.024% off). Snap to the nominal rate
# within this tolerance. Kept below 0.1% so a genuine 30000/1001 (29.97) stream
# tagged 30/1 nominal still keeps its real average.
NOMINAL_SNAP_TOLERANCE = 0.0005


def _snapped_nominal(stream: dict) -> str | None:
    """Nominal rate if the average is within tolerance of it, else None."""
    try:
        avg = Fraction(parse_fps(stream.get("avg_frame_rate") or ""))
        nominal = Fraction(parse_fps(stream.get("r_frame_rate") or ""))
    except argparse.ArgumentTypeError:
        return None
    if abs(avg - nominal) / nominal <= NOMINAL_SNAP_TOLERANCE:
        return f"{nominal.numerator}/{nominal.denominator}"
    return None


def probe_source_fps(video: Path) -> str | None:
    """Return an ffmpeg-ready source rate, preferring the average frame rate.

    ``avg_frame_rate`` represents the observed average and is the better default
    for variable-frame-rate inputs. ``r_frame_rate`` remains a fallback for
    streams where the average is unavailable. Values are normalized to an exact
    rational so rates such as ``30000/1001`` survive without rounding.
    """
    try:
        out = subprocess.run(
            [FFPROBE, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=avg_frame_rate,r_frame_rate",
             "-of", "json", str(video)],
            capture_output=True, text=True, check=True,
        )
        streams = json.loads(out.stdout).get("streams") or []
        if not streams:
            return None
        nominal = _snapped_nominal(streams[0])
        if nominal is not None:
            return nominal
        for field in ("avg_frame_rate", "r_frame_rate"):
            value = streams[0].get(field)
            if value and value != "0/0":
                try:
                    return parse_fps(value)
                except argparse.ArgumentTypeError:
                    continue
    except (subprocess.CalledProcessError, json.JSONDecodeError, OSError):
        return None
    return None


# -------- Per-segment extraction (Rule 2 + Rule 3) --------------------------


def _probe_rms_db(source: Path, t0: float, t1: float) -> float:
    """Mean RMS level (dB) of source's audio over [t0, t1]. -100.0 if silent/unreadable."""
    if t1 <= t0:
        return -100.0
    cmd = [
        FFMPEG, "-nostdin", "-ss", f"{t0:.4f}", "-to", f"{t1:.4f}",
        "-i", str(source), "-af", "astats=metadata=0", "-f", "null", "-",
    ]
    out = subprocess.run(cmd, capture_output=True, text=True).stderr
    vals = re.findall(r"RMS level dB:\s*(-?[\d.]+|-inf)", out)
    if not vals:
        return -100.0
    last = vals[-1]
    return -100.0 if last == "-inf" else float(last)


def _trailing_silence_start(
    source: Path, end: float, lookback: float, noise_db: int, min_dur: float
) -> float | None:
    """Start of the silence run still open at `end`, within
    [end - lookback, end] of source's audio at the given threshold. Returns
    an absolute timestamp, or None if the audio is still sounding at `end`.

    Must be the LAST silence_start with no matching silence_end before
    `end` — not the earliest silence_start in the window. A word with
    internal stop consonants (e.g. the closures inside "duplicates") produces
    its own short silence_start/silence_end pairs that close well before
    `end`; taking the earliest of those lands the correction candidate
    before the word even finishes speaking. Only a silence run that is still
    open when the probe window ends at `end` is a genuine candidate for
    trailing dead air — confirmed against a real case where "duplicates."
    had a 60ms inter-word gap register as the earliest silence_start, while
    the true trailing silence didn't begin until ~0.74s later.
    """
    t0 = max(0.0, end - lookback)
    if end <= t0:
        return None
    cmd = [
        FFMPEG, "-nostdin", "-ss", f"{t0:.4f}", "-to", f"{end:.4f}",
        "-i", str(source),
        "-af", f"silencedetect=noise={noise_db}dB:d={min_dur}",
        "-f", "null", "-",
    ]
    out = subprocess.run(cmd, capture_output=True, text=True).stderr
    events = re.findall(r"silence_(start|end):\s*([\d.]+)", out)
    open_start: float | None = None
    for kind, val in events:
        if kind == "start":
            open_start = t0 + float(val)
        else:
            open_start = None
    return open_start


def verify_segment_end(
    source: Path, start: float, end: float, pad: float = 0.04, verbose: bool = True
) -> float:
    """Guard against ASR last-word timestamp drift before trusting a cut end.

    Every segment end in this pipeline comes from a transcript phrase break on
    a silence gap — exactly the case where Scribe's end timestamp for the last
    word can drift 0.5-2s+ late, since there's no next word to bound it
    against (far beyond the ~50-100ms drift Rule 7 already accounts for).
    Cutting where the waveform first drops under a loose threshold still
    catches the word's decay tail, not true silence, so the baked-in 30ms
    fade-out (Rule 3) audibly chops real sound instead of fading silence.

    Finds a candidate cut point at the strict (-45dB) noise-floor boundary,
    then requires the ENTIRE span from that candidate through the original
    `end` to measure as silent (via `_probe_rms_db`), not just that a silence
    event exists somewhere nearby — an earlier mid-sentence pause followed by
    more real speech before `end` fails that check (the span includes that
    speech, so its average RMS reads loud) and is correctly left alone.

    That span-average check is NOT sufficient by itself, though: a stop
    consonant's silent closure phase (e.g. the "k" in "works.") can register
    as near-silence even though the word's release burst and trailing
    fricative haven't played out yet, and a short loud blip surrounded by
    quiet can still average below the threshold over the whole span (caught
    a real case — "works." got clipped mid-consonant this way, a 0.237s
    over-correction that landed inside the word, not after it). The minimum
    correction-magnitude gate below is the actual safety margin against that:
    genuine ASR drift bugs measured 0.86s and 2.2s of excess trailing dead
    air, while ordinary consonant closures and normal end-of-take padding
    measured up to ~0.46s across otherwise-fine segments in the same edit.
    0.5s sits comfortably between the two, so only unambiguous cases correct.
    Never pushes the end past the original ASR timestamp.
    """
    duration = end - start
    if duration <= 0.05:
        return end

    lookback = min(duration, 3.0)
    candidate_start = _trailing_silence_start(source, end, lookback, noise_db=-45, min_dur=0.05)
    if candidate_start is None:
        return end

    candidate = min(end, candidate_start + pad)
    if (end - candidate) <= 0.5:
        return end  # not enough excess to bother correcting

    span_rms = _probe_rms_db(source, candidate, end)
    if span_rms > -45.0:
        return end  # real sound somewhere in [candidate, end]; leave it alone

    if verbose:
        print(f"        [verify] end {end:.3f} -> {candidate:.3f}s "
              f"(ASR timestamp drift / mid-decay cut)")
    return candidate


def extract_segment(
    source: Path,
    seg_start: float,
    duration: float,
    grade_filter: str,
    out_path: Path,
    preview: bool = False,
    draft: bool = False,
    rate: str | None = None,
    speed: float = 1.0,
) -> None:
    """Extract a cut range as its own MP4 with grade + 30ms audio fades baked in.

    `speed` != 1.0 time-stretches the segment in the same encode (video
    `setpts`, pitch-preserving `atempo` audio), so a sped-up render costs no
    extra generation of video encoding. `duration` is the *source* duration;
    the segment comes out `duration / speed` long.

    Final segments carry uncompressed PCM audio (so `out_path` should be
    .mov). AAC per segment would add ~21ms of encoder priming/padding per
    file, which players and loudnorm play back-to-back, so audio drifted
    ~40ms later at every concat join. Audio is AAC-encoded once, after the join.

    `-ss` before `-i` for fast accurate seeking. Scale to 1080p from 4K.
    Portrait sources (height > width) are scaled by height to preserve orientation.

    Quality ladder:
      - final (default): source resolution, source color (HDR kept),
                         HEVC Main 10 via `final_video_args`
      - preview:         1080p libx264 medium CRF 22, SDR (evaluable for QC)
      - draft:           720p libx264 ultrafast CRF 28 (cut-point check only)
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    final = not (preview or draft)

    # Frame rate: use the rate the caller resolved once for the whole render
    # (every segment must share it — concat -c copy in Rule 2 requires a uniform
    # frame rate). When called standalone with no rate, preserve this source's
    # own rate; fall back to 24 only if it can't be probed.
    out_rate = rate if rate is not None else (probe_source_fps(source) or "24")

    vf_parts: list[str] = []
    if not final:
        portrait = is_portrait_source(source)
        if draft:
            scale = "scale=-2:1280" if portrait else "scale=1280:-2"
        else:
            scale = "scale=-2:1920" if portrait else "scale=1920:-2"
        if is_hdr_source(source):
            vf_parts.append(TONEMAP_CHAIN)
        vf_parts.append(scale)
    if grade_filter:
        vf_parts.append(grade_filter)
    if speed != 1.0:
        # `fps=...:round=near` picks the nearest source frame for each output
        # frame. Without it, `-fps_mode cfr` rounds late and video trails the
        # time-stretched audio by up to ~50ms at 1.1x.
        vf_parts.append(f"setpts=(PTS-STARTPTS)/{speed}")
        vf_parts.append(f"fps={out_rate}:round=near")

    # Output length snapped to a whole number of frames. ffmpeg keeps the full
    # last video frame past `-t`, so an off-grid length leaves video up to one
    # frame longer than audio — and those gaps accumulate across concat joins.
    # `-frames:v` pins the exact video frame count; `-t` trims audio to match.
    frame = 1 / Fraction(out_rate)
    out_frames = max(1, round(Fraction(duration / speed) / frame))
    out_duration = float(out_frames * frame)

    # 30ms audio fades at both edges (Rule 3) — prevent pops. Fades run after
    # the tempo change, so they're placed on the output timeline.
    fade_out_start = max(0.0, out_duration - 0.03)
    af = f"afade=t=in:st=0:d=0.03,afade=t=out:st={fade_out_start:.3f}:d=0.03"
    if speed != 1.0:
        af = f"{atempo_chain(speed)},{af}"

    if final:
        video_args = final_video_args(source)
    else:
        preset, crf = ("ultrafast", "28") if draft else ("medium", "22")
        video_args = ["-c:v", "libx264", "-preset", preset, "-crf", crf,
                      "-pix_fmt", "yuv420p"]

    cmd = [
        FFMPEG, "-y",
        "-ss", f"{seg_start:.3f}",
        "-i", str(source),
        "-t", f"{out_duration:.6f}",
        *(["-vf", ",".join(vf_parts)] if vf_parts else []),
        "-af", af,
        *video_args,
        "-frames:v", str(out_frames),
        "-r", out_rate, *(["-fps_mode", "cfr"] if final else []),
        *(["-c:a", "pcm_s16le"] if final else ["-c:a", "aac", "-b:a", "192k"]),
        "-ar", "48000",
        "-movflags", "+faststart",
        str(out_path),
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def extract_all_segments(
    edl: dict,
    edit_dir: Path,
    preview: bool,
    draft: bool = False,
    fps: str | None = None,
    ranges: list[dict] | None = None,
    prefix: str = "seg",
) -> list[Path]:
    """Extract every EDL range into edit_dir/clips_graded/seg_NN.mp4.
    Returns the ordered list of segment paths.

    `ranges` overrides the EDL's top-level ranges (used for hooks),
    and `prefix` keeps each group's segment files distinct.

    If the EDL `grade` is "auto", analyze each segment range with
    `auto_grade_for_clip` and apply a per-segment subtle correction.
    Otherwise, apply the same preset/raw filter to every segment.
    """
    resolved = resolve_grade_filter(edl.get("grade"))
    speed = float(edl.get("speed", 1.0))
    if speed <= 0:
        sys.exit(f"EDL speed must be greater than zero, got {speed}")
    is_auto = resolved == "__AUTO__"
    clips_dir = edit_dir / (
        "clips_draft" if draft else ("clips_preview" if preview else "clips_graded")
    )
    clips_dir.mkdir(parents=True, exist_ok=True)

    ranges = edl["ranges"] if ranges is None else ranges
    sources = edl["sources"]

    # Resolve ONE output frame rate for the entire render and apply it to every
    # segment. The lossless concat (Rule 2, `-c copy`) requires all segments to
    # share a frame rate; probing per-segment would diverge for multi-source
    # EDLs that mix rates (e.g. a 30fps and a 60fps source) and break the concat.
    # Explicit --fps wins; otherwise preserve the first source's rate.
    if fps is not None:
        out_rate = parse_fps(str(fps))
    elif ranges:
        first_src = resolve_path(sources[ranges[0]["source"]], edit_dir)
        out_rate = probe_source_fps(first_src) or "24"
    else:
        out_rate = "24"

    seg_paths: list[Path] = []
    print(f"extracting {len(ranges)} segment(s) → {clips_dir.name}/  @ {out_rate} fps"
          f"{' (forced)' if fps is not None else ' (from source)'}")
    if is_auto:
        print("  (auto-grade per segment: analyzing each range)")
    for i, r in enumerate(ranges):
        src_name = r["source"]
        src_path = resolve_path(sources[src_name], edit_dir)
        start = float(r["start"])
        end = verify_segment_end(src_path, start, float(r["end"]))
        duration = end - start
        # Final segments carry PCM audio, which needs .mov (see extract_segment).
        ext = ".mp4" if (preview or draft) else ".mov"
        out_path = clips_dir / f"{prefix}_{i:02d}_{src_name}{ext}"

        if is_auto:
            seg_filter, _stats = auto_grade_for_clip(src_path, start=start, duration=duration, verbose=False)
        else:
            seg_filter = resolved

        note = r.get("beat") or r.get("note") or ""
        print(f"  [{i:02d}] {src_name}  {start:7.2f}-{end:7.2f}  ({duration:5.2f}s)  {note}")
        if is_auto:
            print(f"        grade: {seg_filter or '(none)'}")
        extract_segment(src_path, start, duration, seg_filter, out_path, preview=preview, draft=draft, rate=out_rate, speed=speed)
        seg_paths.append(out_path)

    return seg_paths



# -------- Hook + body pieces ------------------------------------------------

HOOK_NAME_RE = re.compile(r"[A-Za-z0-9_-]+")
BODY_NAME = "body"
DELIVER_DIR = "deliver"  # only final videos + caption transcripts
# iCloud Drive's local folder; files copied here sync to the phone untouched.
ICLOUD_DRIVE = Path.home() / "Library" / "Mobile Documents" / "com~apple~CloudDocs"


def render_pieces(
    edl: dict,
    edit_dir: Path,
    preview: bool = False,
    draft: bool = False,
    fps: str | None = None,
    loudnorm: bool = True,
) -> list[Path]:
    """One file per hook plus one shared body file, stitched later on the phone.

    Each entry in `hooks` ({"name": ..., "ranges": [...]}) becomes
    edit_dir/deliver/<name>.mp4 and the EDL's top-level `ranges` become
    edit_dir/deliver/body.mp4, next to the caption transcripts, so deliver/
    holds only what the user AirDrops. Preview/draft renders get a suffix and
    stay in edit_dir. Every piece gets the same audio gain, set from the body, so
    there is no level jump where a hook is stitched onto the body.
    """
    hooks = edl.get("hooks") or []
    if not hooks:
        sys.exit("EDL has no hooks")
    if not edl.get("ranges"):
        sys.exit("EDL has hooks but no body ranges")
    names = [h.get("name", "") for h in hooks]
    for h, name in zip(hooks, names):
        if not HOOK_NAME_RE.fullmatch(name):
            sys.exit(f"hook name must match [A-Za-z0-9_-]+, got {name!r}")
        if name == BODY_NAME:
            sys.exit(f"hook name {BODY_NAME!r} is reserved for the body file")
        if not h.get("ranges"):
            sys.exit(f"hook {name!r} has no ranges")
    if len(set(names)) != len(names):
        sys.exit(f"hook names must be unique: {names}")

    ignored = [k for k in ("grade", "overlays", "subtitles") if edl.get(k)]
    if ignored:
        print(f"warning: {', '.join(ignored)} not supported with hooks — ignoring")
    edl = {**edl, "grade": None}

    # One rate for every piece, so they stitch cleanly (Rule 2).
    if fps is not None:
        rate = parse_fps(str(fps))
    else:
        first = resolve_path(edl["sources"][edl["ranges"][0]["source"]], edit_dir)
        rate = probe_source_fps(first) or "24"

    suffix = "_draft" if draft else ("_preview" if preview else "")
    deliver_dir = edit_dir / DELIVER_DIR
    pieces = [(BODY_NAME, extract_all_segments(
        edl, edit_dir, preview, draft=draft, fps=rate, prefix=BODY_NAME))]
    for hook, name in zip(hooks, names):
        segs = extract_all_segments(
            edl, edit_dir, preview, draft=draft, fps=rate,
            ranges=hook["ranges"], prefix=name,
        )
        pieces.append((name, segs))
        write_variant_transcript(edl, hook, edit_dir, deliver_dir)
    pieces.append(pieces.pop(0))  # hooks first, body last

    # Finals go to deliver/ (just what the user AirDrops); previews stay in edit/.
    out_dir = edit_dir if suffix else deliver_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    outputs = [out_dir / f"{name}{suffix}.mp4" for name, _ in pieces]
    if not loudnorm:
        for (name, segs), out_path in zip(pieces, outputs):
            concat_segments(segs, out_path, edit_dir, encode_audio=True)
        return outputs

    # Joined audio stays uncompressed; the gain step does the one AAC encode.
    joined = [edit_dir / f"{name}{suffix}.prenorm{segs[0].suffix}" for name, segs in pieces]
    for (_, segs), path in zip(pieces, joined):
        concat_segments(segs, path, edit_dir)
    gain = shared_gain_db([measure_loudness(p) for p in joined])
    print(f"  loudness: +{gain:.2f} dB on every piece" if gain >= 0
          else f"  loudness: {gain:.2f} dB on every piece")
    for path, out_path in zip(joined, outputs):
        apply_gain(path, out_path, gain)
        path.unlink(missing_ok=True)
    if not suffix:
        publish_to_icloud(deliver_dir, ICLOUD_DRIVE)
    return outputs


def publish_to_icloud(deliver_dir: Path, icloud_root: Path) -> Path | None:
    """Copy everything in deliver_dir to <iCloud Drive>/Reels/<slug>/, where
    slug is the recording's folder name (edit/ sits inside it). iCloud syncs
    the files to the phone uncompressed. A re-render replaces them in place.

    Returns the destination, or None (with a warning) if iCloud Drive is
    missing or macOS blocks access; the local deliver/ folder is unaffected.
    """
    slug = deliver_dir.parent.parent.name
    dest = icloud_root / "Reels" / slug
    try:
        if not icloud_root.is_dir():
            raise FileNotFoundError(icloud_root)
        dest.mkdir(parents=True, exist_ok=True)
        for f in sorted(deliver_dir.iterdir()):
            if f.is_file():
                shutil.copy2(f, dest / f.name)
    except OSError as e:
        print(f"warning: could not copy to iCloud Drive ({e}) — files are only in {deliver_dir}")
        return None
    print(f"icloud: copied {deliver_dir.name}/ → iCloud Drive/Reels/{slug}/")
    return dest


def shared_gain_db(measurements: list[dict | None]) -> float:
    """One gain for all pieces: bring the body (last) to LOUDNORM_I.

    The pieces come from one recording with one mic, so a single gain keeps
    them at the same level; normalizing each on its own would not, because a
    short hook's integrated loudness reads differently from the body's.
    Peaks are left to the limiter in apply_gain, not used to cap the gain:
    quiet, peaky speech would otherwise land several dB under the target.
    """
    body = measurements[-1]
    if body is None:
        print("  warning: body loudness measurement failed — leaving levels as recorded")
        return 0.0
    return LOUDNORM_I - float(body["input_i"])


def apply_gain(input_path: Path, output_path: Path, gain_db: float) -> None:
    """Apply a fixed audio gain, then a peak limiter, and do the single AAC
    encode; video is copied. The limiter settings are the same for every piece,
    so pieces stitched together get identical treatment."""
    # Sample-peak limit a little under LOUDNORM_TP: true peaks (and AAC
    # overshoot) sit slightly above sample peaks; _enforce_true_peak_ceiling
    # catches the rest.
    limit = 10 ** ((LOUDNORM_TP - 0.5) / 20)
    cmd = [
        FFMPEG, "-y", "-hide_banner", "-nostats",
        "-i", str(input_path),
        "-c:v", "copy",
        "-af", f"volume={gain_db:.3f}dB,alimiter=limit={limit:.4f}"
               ":attack=5:release=50:level=disabled",
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
        "-movflags", "+faststart",
        str(output_path),
    ]
    print(f"  gain → {output_path.name}")
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    _enforce_true_peak_ceiling(output_path)


# -------- Lossless concat ----------------------------------------------------


def concat_segments(
    segment_paths: list[Path], out_path: Path, edit_dir: Path, encode_audio: bool = False
) -> None:
    """Lossless concat via the concat demuxer. Video is never re-encoded.

    `encode_audio` compresses the joined (PCM) audio to AAC in this same pass,
    for outputs that get no later audio step.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    concat_list = edit_dir / "_concat.txt"
    concat_list.write_text("".join(f"file '{p.resolve()}'\n" for p in segment_paths))

    cmd = [
        FFMPEG, "-y",
        "-f", "concat", "-safe", "0",
        "-i", str(concat_list),
        "-c", "copy",
        *(["-c:a", "aac", "-b:a", "192k", "-ar", "48000"] if encode_audio else []),
        "-movflags", "+faststart",
        str(out_path),
    ]
    print(f"concat → {out_path.name}")
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    concat_list.unlink(missing_ok=True)


# -------- Master SRT (Rule 5) ------------------------------------------------


PUNCT_BREAK = set(".,!?;:")


def _srt_timestamp(seconds: float) -> str:
    total_ms = int(round(seconds * 1000))
    h, rem = divmod(total_ms, 3600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _words_in_range(transcript: dict, t_start: float, t_end: float) -> list[dict]:
    out: list[dict] = []
    for w in transcript.get("words", []):
        if w.get("type") != "word":
            continue
        ws = w.get("start")
        we = w.get("end")
        if ws is None or we is None:
            continue
        if we <= t_start or ws >= t_end:
            continue
        out.append(w)
    return out


def write_variant_transcript(
    edl: dict, hook: dict, edit_dir: Path, out_dir: Path | None = None
) -> Path | None:
    """Write the exact spoken words of one variant (hook, then body) to
    out_dir/<hook name>_transcript.md (default edit_dir), for the post caption.

    Returns the path, or None if a source transcript is missing.
    """
    transcripts: dict[str, dict] = {}

    def spoken(ranges: list[dict]) -> str | None:
        words: list[str] = []
        for r in ranges:
            src = r["source"]
            if src not in transcripts:
                path = edit_dir / "transcripts" / f"{src}.json"
                if not path.exists():
                    print(f"warning: no transcript at {path} — skipping transcript for {hook['name']}")
                    return None
                transcripts[src] = json.loads(path.read_text(encoding="utf-8"))
            words += [w["text"].strip() for w in
                      _words_in_range(transcripts[src], float(r["start"]), float(r["end"]))]
        return " ".join(words)

    hook_text = spoken(hook["ranges"])
    body_text = spoken(edl["ranges"]) if hook_text is not None else None
    if body_text is None:
        return None
    out_dir = out_dir or edit_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{hook['name']}_transcript.md"
    out_path.write_text(f"{hook_text}\n\n{body_text}\n", encoding="utf-8")
    return out_path


def build_master_srt(edl: dict, edit_dir: Path, out_path: Path) -> None:
    """Build an output-timeline SRT from per-source transcripts.

    - 2-word chunks (break on any punctuation in between)
    - UPPERCASE text
    - Output times computed as word.start - segment_start + segment_offset
    """
    transcripts_dir = edit_dir / "transcripts"
    sources = edl["sources"]

    entries: list[tuple[float, float, str]] = []
    seg_offset = 0.0

    for r in edl["ranges"]:
        src_name = r["source"]
        seg_start = float(r["start"])
        seg_end = float(r["end"])
        seg_duration = seg_end - seg_start

        tr_path = transcripts_dir / f"{src_name}.json"
        if not tr_path.exists():
            print(f"  no transcript for {src_name}, skipping captions for this segment")
            seg_offset += seg_duration
            continue

        transcript = json.loads(tr_path.read_text())
        words_in_seg = _words_in_range(transcript, seg_start, seg_end)

        # Group into 2-word chunks, break on punctuation
        chunks: list[list[dict]] = []
        current: list[dict] = []
        for w in words_in_seg:
            text = (w.get("text") or "").strip()
            if not text:
                continue
            current.append(w)
            # Break if the current text ends in punctuation or we hit 2 words
            ends_in_punct = bool(text) and text[-1] in PUNCT_BREAK
            if len(current) >= 2 or ends_in_punct:
                chunks.append(current)
                current = []
        if current:
            chunks.append(current)

        for chunk in chunks:
            local_start = max(seg_start, chunk[0].get("start", seg_start))
            local_end = min(seg_end, chunk[-1].get("end", seg_end))
            out_start = max(0.0, local_start - seg_start) + seg_offset
            out_end = max(0.0, local_end - seg_start) + seg_offset
            if out_end <= out_start:
                out_end = out_start + 0.4
            text = " ".join((w.get("text") or "").strip() for w in chunk)
            text = re.sub(r"\s+", " ", text).strip()
            # Strip trailing punctuation for cleaner uppercase look
            text = text.rstrip(",;:")
            text = text.upper()
            entries.append((out_start, out_end, text))

        seg_offset += seg_duration

    # Sort and write as SRT
    entries.sort(key=lambda e: e[0])
    lines: list[str] = []
    for i, (a, b, t) in enumerate(entries, start=1):
        lines.append(str(i))
        lines.append(f"{_srt_timestamp(a)} --> {_srt_timestamp(b)}")
        lines.append(t)
        lines.append("")
    out_path.write_text("\n".join(lines))
    print(f"master SRT → {out_path.name} ({len(entries)} cues)")


# -------- Loudness normalization (social-ready audio) -----------------------


# Social-media standard: -14 LUFS integrated, -1 dBTP peak, LRA 11 LU.
# Matches YouTube / Instagram / TikTok / X / LinkedIn normalization targets.
LOUDNORM_I = -14.0
LOUDNORM_TP = -1.0
LOUDNORM_LRA = 11.0


def measure_loudness(video_path: Path) -> dict[str, str] | None:
    """Run ffmpeg loudnorm first pass and parse the JSON measurement.

    Returns a dict with measured_i, measured_tp, measured_lra, measured_thresh,
    target_offset, or None if measurement failed.
    """
    filter_str = (
        f"loudnorm=I={LOUDNORM_I}:TP={LOUDNORM_TP}:LRA={LOUDNORM_LRA}:print_format=json"
    )
    cmd = [
        FFMPEG, "-y", "-hide_banner", "-nostats",
        "-i", str(video_path),
        "-af", filter_str,
        "-vn", "-f", "null", "-",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    # loudnorm prints the JSON to stderr at the end of the run
    stderr = proc.stderr

    # Find the JSON block — loudnorm output contains a `{ ... }` block
    start = stderr.rfind("{")
    end = stderr.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        data = json.loads(stderr[start : end + 1])
    except json.JSONDecodeError:
        return None
    needed = {"input_i", "input_tp", "input_lra", "input_thresh", "target_offset"}
    if not needed.issubset(data.keys()):
        return None
    return data


def _enforce_true_peak_ceiling(path: Path, max_attempts: int = 2) -> None:
    """Verify the real encoded file's true peak and trim it if needed.

    loudnorm's own gain calculation (both linear and dynamic mode) targets
    LOUDNORM_TP on the pre-encode PCM signal, but lossy AAC encoding can add
    inter-sample overshoot on top of that — observed on real content: a
    loudnorm pass targeting -1 dBTP produced a +0.3 dBTP *encoded* file
    (confirmed reproducible in both linear and dynamic modes, so this is an
    encoding-stage effect, not a loudnorm mode bug). Trust the measurement
    of what actually got shipped, not what loudnorm aimed for pre-encode.
    """
    for _ in range(max_attempts):
        check = measure_loudness(path)
        if check is None:
            return
        overshoot = float(check["input_tp"]) - LOUDNORM_TP
        if overshoot <= 0.05:
            return
        print(f"    true peak {check['input_tp']} dBTP overshoots target by "
              f"{overshoot:.2f} dB after encoding — trimming")
        trimmed = path.with_name(path.stem + ".tptrim" + path.suffix)
        cmd = [
            FFMPEG, "-y", "-hide_banner", "-nostats",
            "-i", str(path),
            "-c:v", "copy",
            "-af", f"volume=-{overshoot + 0.1:.3f}dB",
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
            "-movflags", "+faststart",
            str(trimmed),
        ]
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        trimmed.replace(path)


def apply_loudnorm_two_pass(
    input_path: Path,
    output_path: Path,
    preview: bool = False,
) -> bool:
    """Run two-pass loudnorm on input_path, write normalized copy to output_path.

    Returns True on success, False if measurement failed (caller should fall
    back to copying the input unchanged).

    In preview mode, skips the measurement pass and uses a one-pass approximation
    for speed. Final mode always does the proper two-pass.
    """
    if preview:
        # One-pass approximation — faster, slightly less accurate.
        filter_str = f"loudnorm=I={LOUDNORM_I}:TP={LOUDNORM_TP}:LRA={LOUDNORM_LRA}"
        cmd = [
            FFMPEG, "-y", "-hide_banner", "-nostats",
            "-i", str(input_path),
            "-c:v", "copy",
            "-af", filter_str,
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
            "-movflags", "+faststart",
            str(output_path),
        ]
        print(f"  loudnorm (1-pass preview) → {output_path.name}")
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        _enforce_true_peak_ceiling(output_path)
        return True

    # Full two-pass
    print(f"  loudnorm pass 1: measuring {input_path.name}")
    measurement = measure_loudness(input_path)
    if measurement is None:
        print("  loudnorm measurement failed — falling back to 1-pass")
        return apply_loudnorm_two_pass(input_path, output_path, preview=True)

    print(f"    measured: I={measurement['input_i']} LUFS  "
          f"TP={measurement['input_tp']}  LRA={measurement['input_lra']}")

    filter_str = (
        f"loudnorm=I={LOUDNORM_I}:TP={LOUDNORM_TP}:LRA={LOUDNORM_LRA}"
        f":measured_I={measurement['input_i']}"
        f":measured_TP={measurement['input_tp']}"
        f":measured_LRA={measurement['input_lra']}"
        f":measured_thresh={measurement['input_thresh']}"
        f":offset={measurement['target_offset']}"
        f":linear=true"
    )
    cmd = [
        FFMPEG, "-y", "-hide_banner", "-nostats",
        "-i", str(input_path),
        "-c:v", "copy",
        "-af", filter_str,
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
        "-movflags", "+faststart",
        str(output_path),
    ]
    print(f"  loudnorm pass 2: normalizing → {output_path.name}")
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    _enforce_true_peak_ceiling(output_path)
    return True


# -------- Final compositing (Rule 1 + Rule 4) -------------------------------


def build_final_composite(
    base_path: Path,
    overlays: list[dict],
    subtitles_path: Path | None,
    out_path: Path,
    edit_dir: Path,
) -> None:
    """Final pass: base → overlays (PTS-shifted) → subtitles LAST → out.

    If there are no overlays and no subtitles, just copy base to out.
    """
    has_overlays = bool(overlays)
    has_subs = subtitles_path is not None and subtitles_path.exists()

    if not has_overlays and not has_subs:
        # Nothing to do — just rename/copy base to final name
        run([FFMPEG, "-y", "-i", str(base_path), "-c", "copy", str(out_path)], quiet=True)
        return

    inputs: list[str] = ["-i", str(base_path)]
    for ov in overlays:
        ov_path = resolve_path(ov["file"], edit_dir)
        inputs += ["-i", str(ov_path)]

    filter_parts: list[str] = []
    # PTS-shift every overlay so its frame 0 lands at start_in_output.
    # Optional "width"/"height" scale the overlay to fit a region smaller than
    # the base canvas (e.g. a 1440x1080 4:3 insert placed on a 1080x1920 base).
    for idx, ov in enumerate(overlays, start=1):
        t = float(ov["start_in_output"])
        scale = ""
        if "width" in ov and "height" in ov:
            scale = f"scale={int(ov['width'])}:{int(ov['height'])},"
        filter_parts.append(f"[{idx}:v]{scale}setpts=PTS-STARTPTS+{t}/TB[a{idx}]")

    # Chain overlays on top of base. Optional "x"/"y" position the (possibly
    # scaled) overlay on the base canvas; both default to 0 (top-left).
    current = "[0:v]"
    for idx, ov in enumerate(overlays, start=1):
        t = float(ov["start_in_output"])
        dur = float(ov["duration"])
        end = t + dur
        x = ov.get("x", 0)
        y = ov.get("y", 0)
        next_label = f"[v{idx}]"
        filter_parts.append(
            f"{current}[a{idx}]overlay=x={x}:y={y}:enable='between(t,{t:.3f},{end:.3f})'{next_label}"
        )
        current = next_label

    # Subtitles LAST — Rule 1
    if has_subs:
        subs_abs = str(subtitles_path.resolve()).replace(":", r"\:").replace("'", r"\'")
        filter_parts.append(
            f"{current}subtitles='{subs_abs}':force_style='{SUB_FORCE_STYLE}'[outv]"
        )
        out_label = "[outv]"
    else:
        # Rename the last overlay output to [outv] for consistency
        if has_overlays:
            filter_parts.append(f"{current}null[outv]")
            out_label = "[outv]"
        else:
            out_label = "[0:v]"

    filter_complex = ";".join(filter_parts)

    cmd = [
        FFMPEG, "-y",
        *inputs,
        "-filter_complex", filter_complex,
        "-map", out_label,
        "-map", "0:a",
        "-c:v", "libx264", "-preset", "fast", "-crf", "18",
        "-pix_fmt", "yuv420p",
        "-c:a", "copy",
        "-movflags", "+faststart",
        str(out_path),
    ]
    print(f"compositing → {out_path.name}")
    print(f"  overlays: {len(overlays)}, subtitles: {'yes' if has_subs else 'no'}")
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


# -------- Main ---------------------------------------------------------------


def main() -> None:
    ap = argparse.ArgumentParser(description="Render a video from an EDL")
    ap.add_argument("edl", type=Path, help="Path to edl.json")
    ap.add_argument(
        "-o", "--output", type=Path,
        help="Output video path. Required unless the EDL has `hooks`, in which "
             "case each hook is written to <edl dir>/deliver/<hook name>.mp4 "
             "and the shared body to <edl dir>/deliver/body.mp4.",
    )
    ap.add_argument(
        "--preview",
        action="store_true",
        help="Preview mode: 1080p, medium, CRF 22 — evaluable for QC, faster than final.",
    )
    ap.add_argument(
        "--draft",
        action="store_true",
        help="Draft mode: 720p, ultrafast, CRF 28 — cut-point verification only.",
    )
    ap.add_argument(
        "--build-subtitles",
        action="store_true",
        help="Build master.srt from transcripts + EDL offsets before compositing",
    )
    ap.add_argument(
        "--no-subtitles",
        action="store_true",
        help="Skip subtitles even if the EDL references one",
    )
    ap.add_argument(
        "--no-loudnorm",
        action="store_true",
        help="Skip audio loudness normalization. Default is on (-14 LUFS, -1 dBTP, LRA 11).",
    )
    ap.add_argument(
        "--fps",
        type=parse_fps,
        default=None,
        help="Output frame rate. Default: preserve the source's frame rate "
             "(falls back to 24 if it can't be probed). Pass e.g. --fps 30 or "
             "--fps 30000/1001 to force.",
    )
    args = ap.parse_args()

    edl_path = args.edl.resolve()
    if not edl_path.exists():
        sys.exit(f"edl not found: {edl_path}")

    edl = json.loads(edl_path.read_text())
    edit_dir = edl_path.parent

    if edl.get("hooks"):
        if args.output:
            print("note: -o is ignored when the EDL has hooks")
        outputs = render_pieces(
            edl, edit_dir, preview=args.preview, draft=args.draft,
            fps=args.fps, loudnorm=not args.no_loudnorm,
        )
        print()
        for out in outputs:
            print(f"done: {out} ({out.stat().st_size / (1024 * 1024):.1f} MB)")
        return

    if not args.output:
        sys.exit("-o/--output is required when the EDL has no hooks")
    out_path = args.output.resolve()

    # 1. Extract per-segment (auto-grade per range if EDL grade is "auto")
    segment_paths = extract_all_segments(
        edl, edit_dir, preview=args.preview, draft=args.draft, fps=args.fps
    )

    # 2. Concat → base
    if args.draft:
        base_name = "base_draft.mp4"
    elif args.preview:
        base_name = "base_preview.mp4"
    else:
        base_name = "base.mp4"
    base_path = edit_dir / base_name
    concat_segments(segment_paths, base_path, edit_dir, encode_audio=True)

    # 3. Subtitles: build if requested, resolve final path
    subs_path: Path | None = None
    if not args.no_subtitles:
        if args.build_subtitles:
            subs_path = edit_dir / "master.srt"
            build_master_srt(edl, edit_dir, subs_path)
        elif edl.get("subtitles"):
            subs_path = resolve_path(edl["subtitles"], edit_dir)
            if not subs_path.exists():
                print(f"warning: subtitles path in EDL does not exist: {subs_path}")
                subs_path = None

    # 4. Composite (overlays + subtitles LAST) → intermediate (pre-loudnorm) path
    overlays = edl.get("overlays") or []
    if args.no_loudnorm:
        # Composite directly to final output
        build_final_composite(base_path, overlays, subs_path, out_path, edit_dir)
    else:
        # Composite to a temp file, then run loudnorm → final output
        tmp_composite = out_path.with_suffix(".prenorm.mp4")
        build_final_composite(base_path, overlays, subs_path, tmp_composite, edit_dir)
        print("loudness normalization → social-ready (-14 LUFS / -1 dBTP / LRA 11)")
        apply_loudnorm_two_pass(tmp_composite, out_path, preview=args.draft)
        tmp_composite.unlink(missing_ok=True)

    size_mb = out_path.stat().st_size / (1024 * 1024)
    print(f"\ndone: {out_path} ({size_mb:.1f} MB)")


if __name__ == "__main__":
    main()
