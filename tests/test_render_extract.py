import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


MODULE_PATH = Path(__file__).parents[1] / "helpers" / "render.py"
SPEC = importlib.util.spec_from_file_location("video_use_render", MODULE_PATH)
assert SPEC and SPEC.loader
render = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(render)


FINAL_ARGS = ["-c:v", "libx265", "-pix_fmt", "yuv420p10le", "-tag:v", "hvc1"]


class ExtractSegmentCommandTests(unittest.TestCase):
    """Inspect the ffmpeg command extract_segment builds, per quality mode."""

    def _cmd(self, tmp: Path, **kwargs) -> list[str]:
        with (
            patch.object(render, "is_hdr_source", return_value=True),
            patch.object(render, "is_portrait_source", return_value=True),
            patch.object(render, "final_video_args", return_value=FINAL_ARGS),
            patch.object(render.subprocess, "run") as run,
        ):
            render.extract_segment(
                Path("src.mov"), 1.0, 2.0, "", tmp / "seg.mp4", rate="30/1", **kwargs
            )
        return run.call_args.args[0]

    def test_final_keeps_source_resolution_and_hdr(self):
        with tempfile.TemporaryDirectory() as d:
            cmd = self._cmd(Path(d))
        joined = " ".join(cmd)
        self.assertNotIn("tonemap", joined)
        self.assertNotIn("scale=", joined)
        self.assertNotIn("-vf", cmd)
        self.assertNotIn("libx264", cmd)
        self.assertIn(" ".join(FINAL_ARGS), joined)
        self.assertIn("-r 30/1 -fps_mode cfr", joined)
        self.assertEqual(cmd[cmd.index("-c:a") + 1], "pcm_s16le")

    def test_final_applies_explicit_grade_only(self):
        with tempfile.TemporaryDirectory() as d, \
             patch.object(render, "is_hdr_source", return_value=True), \
             patch.object(render, "final_video_args", return_value=FINAL_ARGS), \
             patch.object(render.subprocess, "run") as run:
            render.extract_segment(
                Path("src.mov"), 0.0, 1.0, "eq=contrast=1.1", Path(d) / "s.mp4", rate="30/1"
            )
        cmd = run.call_args.args[0]
        self.assertEqual(cmd[cmd.index("-vf") + 1], "eq=contrast=1.1")

    def test_preview_unchanged_tonemapped_1080p_h264(self):
        with tempfile.TemporaryDirectory() as d:
            cmd = self._cmd(Path(d), preview=True)
        vf = cmd[cmd.index("-vf") + 1]
        self.assertTrue(vf.startswith(render.TONEMAP_CHAIN))
        self.assertTrue(vf.endswith("scale=-2:1920"))
        self.assertIn("libx264", cmd)
        self.assertEqual(cmd[cmd.index("-crf") + 1], "22")
        self.assertEqual(cmd[cmd.index("-pix_fmt") + 1], "yuv420p")
        self.assertNotIn("-fps_mode", cmd)
        self.assertEqual(cmd[cmd.index("-c:a") + 1], "aac")

    def test_speed_applied_in_same_encode_with_output_timeline_fades(self):
        with tempfile.TemporaryDirectory() as d:
            cmd = self._cmd(Path(d), speed=1.1)
        self.assertEqual(cmd[cmd.index("-vf") + 1],
                         "setpts=(PTS-STARTPTS)/1.1,fps=30/1:round=near")
        af = cmd[cmd.index("-af") + 1]
        self.assertTrue(af.startswith("atempo=1.100000,"))
        out_dur = 55 / 30  # 2.0s / 1.1 = 1.818s → snapped to 55 frames @ 30fps
        self.assertIn(f"afade=t=out:st={out_dur - 0.03:.3f}:d=0.03", af)
        self.assertEqual(cmd[cmd.index("-t") + 1], f"{out_dur:.6f}")
        self.assertEqual(cmd[cmd.index("-frames:v") + 1], "55")

    def test_speed_one_adds_no_time_stretch(self):
        with tempfile.TemporaryDirectory() as d:
            cmd = self._cmd(Path(d))
        joined = " ".join(cmd)
        self.assertNotIn("setpts", joined)
        self.assertNotIn("atempo", joined)
        self.assertEqual(cmd[cmd.index("-t") + 1], "2.000000")
        self.assertEqual(cmd[cmd.index("-frames:v") + 1], "60")

    def test_off_grid_duration_snaps_to_whole_frames(self):
        with tempfile.TemporaryDirectory() as d, \
             patch.object(render, "final_video_args", return_value=FINAL_ARGS), \
             patch.object(render.subprocess, "run") as run:
            # 4.43s @ 30fps = 132.9 frames → 133 frames = 4.433333s
            render.extract_segment(Path("s.mov"), 0.0, 4.43, "", Path(d) / "s.mp4", rate="30/1")
        cmd = run.call_args.args[0]
        self.assertEqual(cmd[cmd.index("-frames:v") + 1], "133")
        self.assertEqual(cmd[cmd.index("-t") + 1], f"{133 / 30:.6f}")

    def test_edl_speed_threads_to_every_segment(self):
        edl = {"sources": {"a": "a.mov"}, "speed": 1.1,
               "ranges": [{"source": "a", "start": 0, "end": 1},
                          {"source": "a", "start": 2, "end": 3}]}
        with tempfile.TemporaryDirectory() as d, \
             patch.object(render, "verify_segment_end", side_effect=lambda s, a, b: b), \
             patch.object(render, "extract_segment") as extract, \
             patch("sys.stdout"):
            render.extract_all_segments(edl, Path(d), preview=False, fps="30")
        self.assertEqual([c.kwargs["speed"] for c in extract.call_args_list], [1.1, 1.1])
        self.assertTrue(all(c.args[4].suffix == ".mov" for c in extract.call_args_list))

    def test_non_positive_edl_speed_is_rejected(self):
        edl = {"sources": {"a": "a.mov"}, "speed": 0,
               "ranges": [{"source": "a", "start": 0, "end": 1}]}
        with tempfile.TemporaryDirectory() as d, \
             patch.object(render, "extract_segment") as extract:
            with self.assertRaises(SystemExit):
                render.extract_all_segments(edl, Path(d), preview=False, fps="30")
        extract.assert_not_called()

    def test_draft_unchanged_720p(self):
        with tempfile.TemporaryDirectory() as d:
            cmd = self._cmd(Path(d), draft=True)
        self.assertTrue(cmd[cmd.index("-vf") + 1].endswith("scale=-2:1280"))
        self.assertEqual(cmd[cmd.index("-crf") + 1], "28")


if __name__ == "__main__":
    unittest.main()
