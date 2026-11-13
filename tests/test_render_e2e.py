"""End-to-end: render hook and body pieces from a synthetic iPhone-style HLG clip.

Slow (real ffmpeg + libx265, ~30-60s). Skipped when ffmpeg is unavailable.

Fixture: 1080x1920 HEVC Main 10 HLG @ 30fps, 12s. Every whole second a white
flash (3 frames) on video and a 30ms 1kHz beep on audio start together, so
A/V sync can be measured in the outputs across every concat join.
"""

import contextlib
import importlib.util
import io
import json
import shutil
import subprocess
import tempfile
import unittest
import unittest.mock
from pathlib import Path

import numpy as np


MODULE_PATH = Path(__file__).parents[1] / "helpers" / "render.py"
SPEC = importlib.util.spec_from_file_location("video_use_render", MODULE_PATH)
assert SPEC and SPEC.loader
render = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(render)

FPS = 30
SPEED = 1.1
FRAME = 1 / FPS


def _have_ffmpeg() -> bool:
    return all(shutil.which(b) or Path(b).exists() for b in (render.FFMPEG, render.FFPROBE))


def _make_fixture(path: Path) -> None:
    subprocess.run([
        render.FFMPEG, "-v", "error", "-y",
        "-f", "lavfi", "-i", f"color=black:s=1080x1920:r={FPS}:d=12",
        "-f", "lavfi", "-i",
        "aevalsrc='if(lt(mod(t,1),0.03),0.5*sin(2*PI*1000*t),0)':s=48000:d=12",
        "-vf", f"drawbox=enable='lt(mod(n,{FPS}),3)':color=white:t=fill",
        "-c:v", "libx265", "-preset", "ultrafast", "-pix_fmt", "yuv420p10le",
        "-profile:v", "main10",
        "-x265-params", "log-level=error:colorprim=bt2020:transfer=arib-std-b67:"
                        "colormatrix=bt2020nc:range=limited",
        "-color_primaries", "bt2020", "-color_trc", "arib-std-b67",
        "-colorspace", "bt2020nc", "-color_range", "tv", "-tag:v", "hvc1",
        "-c:a", "aac", "-b:a", "192k",
        str(path),
    ], check=True)


def _probe(path: Path) -> dict:
    out = subprocess.run(
        [render.FFPROBE, "-v", "error", "-show_streams", "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    )
    streams = json.loads(out.stdout)["streams"]
    return {s["codec_type"]: s for s in streams}


def _flash_onsets(path: Path) -> list[float]:
    """Times where mean luma jumps from dark to bright."""
    out = subprocess.run(
        [render.FFPROBE, "-v", "error", "-f", "lavfi",
         "-i", f"movie={path},signalstats",
         "-show_entries", "frame=pts_time:frame_tags=lavfi.signalstats.YAVG",
         "-of", "csv=p=0"],
        capture_output=True, text=True, check=True,
    )
    onsets, prev_bright = [], False
    for line in out.stdout.splitlines():
        t, yavg = line.split(",")[:2]
        bright = float(yavg) > 300  # 10-bit scale; black ≈ 64, white ≈ 940
        if bright and not prev_bright:
            onsets.append(float(t))
        prev_bright = bright
    return onsets


def _beep_onsets(path: Path) -> list[float]:
    """Times where a beep starts, from decoded sample positions.

    Sample-based on purpose: silencedetect's threshold is thrown off by the
    faint codec noise AAC re-encodes leave in the gaps, which showed up as a
    false ~34ms "shift". Assumes audio starts at t=0 (asserted separately).
    """
    raw = subprocess.run(
        [render.FFMPEG, "-v", "error", "-i", str(path),
         "-ac", "1", "-ar", "48000", "-f", "f32le", "-"],
        capture_output=True, check=True,
    ).stdout
    loud = np.flatnonzero(np.abs(np.frombuffer(raw, dtype=np.float32)) > 0.05)
    if loud.size == 0:
        return []
    starts = [loud[0], *loud[1:][np.diff(loud) > 4800]]  # >0.1s gap = new beep
    return [int(i) / 48000 for i in starts]


def _peak(path: Path) -> float:
    raw = subprocess.run(
        [render.FFMPEG, "-v", "error", "-i", str(path),
         "-ac", "1", "-ar", "48000", "-f", "f32le", "-"],
        capture_output=True, check=True,
    ).stdout
    return float(np.max(np.abs(np.frombuffer(raw, dtype=np.float32))))


def _r(start: float, end: float) -> dict:
    return {"source": "clip", "start": start, "end": end}


# Every range starts 0.3s before a beep and ends 0.2s after one, so each
# segment carries flash/beep pairs and verify_segment_end has nothing to trim.
HOOKS = [
    {"name": "hook_1", "ranges": [_r(0.7, 2.2)]},
    {"name": "hook_2", "ranges": [_r(2.7, 3.2), _r(3.7, 4.2)]},
]
BODY = [_r(5.7, 8.2), _r(8.7, 10.2)]


def _expected_frames(ranges: list[dict]) -> int:
    return sum(round((r["end"] - r["start"]) / SPEED * FPS) for r in ranges)


@unittest.skipUnless(_have_ffmpeg(), "ffmpeg/ffprobe not available")
class HookAndBodyPiecesEndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.edit_dir = Path(cls._tmp.name)
        clip = cls.edit_dir / "clip.mov"
        _make_fixture(clip)
        (cls.edit_dir / "transcripts").mkdir()
        (cls.edit_dir / "transcripts" / "clip.json").write_text(json.dumps({"words": [
            {"text": "Hook", "start": 1.0, "end": 1.2, "type": "word"},
            {"text": "body.", "start": 6.0, "end": 6.3, "type": "word"},
        ]}))
        cls.edl = {"sources": {"clip": str(clip)}, "speed": SPEED,
                   "hooks": HOOKS, "ranges": BODY}
        # Never publish test renders to the real iCloud Drive.
        with contextlib.redirect_stdout(io.StringIO()), \
             unittest.mock.patch.object(render, "ICLOUD_DRIVE", cls.edit_dir / "icloud"):
            (cls.edit_dir / "icloud").mkdir()
            cls.outputs = render.render_pieces(cls.edl, cls.edit_dir)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_one_file_per_hook_plus_the_body(self):
        self.assertEqual([p.name for p in self.outputs],
                         ["hook_1.mp4", "hook_2.mp4", "body.mp4"])
        # deliver/ holds only what the user AirDrops: videos + caption transcripts.
        self.assertEqual(sorted(p.name for p in (self.edit_dir / "deliver").iterdir()),
                         ["body.mp4", "hook_1.mp4", "hook_1_transcript.md",
                          "hook_2.mp4", "hook_2_transcript.md"])
        self.assertFalse(list(self.edit_dir.glob("*.prenorm.*")))

    def test_outputs_keep_source_format_and_hdr(self):
        for out in self.outputs:
            with self.subTest(out=out.name):
                v = _probe(out)["video"]
                self.assertEqual(v["codec_name"], "hevc")
                self.assertEqual(v["profile"], "Main 10")
                self.assertEqual(v["codec_tag_string"], "hvc1")
                self.assertEqual(v["pix_fmt"], "yuv420p10le")
                self.assertEqual(v["color_transfer"], "arib-std-b67")
                self.assertEqual(v["color_primaries"], "bt2020")
                self.assertEqual(v["color_space"], "bt2020nc")
                self.assertEqual((v["width"], v["height"]), (1080, 1920))
                self.assertEqual(v["r_frame_rate"], "30/1")
                self.assertEqual(v["avg_frame_rate"], "30/1")
                a = _probe(out)["audio"]
                self.assertEqual(a["sample_rate"], "48000")
                # Both streams start at 0: no priming offset between them.
                self.assertEqual(float(v["start_time"]), 0.0)
                self.assertEqual(float(a["start_time"]), 0.0)

    def test_frame_count_matches_sped_up_ranges(self):
        for out, ranges in zip(self.outputs, [h["ranges"] for h in HOOKS] + [BODY]):
            with self.subTest(out=out.name):
                self.assertEqual(int(_probe(out)["video"]["nb_frames"]),
                                 _expected_frames(ranges))

    def test_audio_stays_in_sync_across_joins(self):
        for out, n_beeps in zip(self.outputs, (2, 2, 5)):
            with self.subTest(out=out.name):
                flashes, beeps = _flash_onsets(out), _beep_onsets(out)
                self.assertEqual(len(flashes), n_beeps, flashes)
                self.assertEqual(len(beeps), n_beeps, beeps)
                offsets = [b - f for f, b in zip(flashes, beeps)]
                # Each beep within one frame of its flash, and no drift
                # building up from the first join to the last.
                for off in offsets:
                    self.assertLessEqual(abs(off), FRAME + 0.005, offsets)
                self.assertLessEqual(max(offsets) - min(offsets), FRAME + 0.005, offsets)

    def test_pieces_play_at_the_same_level(self):
        # Every source beep has the same amplitude, so with one shared gain
        # the beeps peak equally in every piece: no level jump at the stitch.
        peaks_db = [20 * np.log10(_peak(out)) for out in self.outputs]
        self.assertLessEqual(max(peaks_db) - min(peaks_db), 0.5, peaks_db)

    def test_deliver_files_copied_to_icloud_reels_folder(self):
        dest = self.edit_dir / "icloud" / "Reels" / self.edit_dir.parent.name
        self.assertEqual(sorted(p.name for p in dest.iterdir()),
                         sorted(p.name for p in (self.edit_dir / "deliver").iterdir()))

    def test_transcript_written_per_variant(self):
        self.assertEqual((self.edit_dir / "deliver" / "hook_1_transcript.md").read_text(),
                         "Hook\n\nbody.\n")


if __name__ == "__main__":
    unittest.main()
