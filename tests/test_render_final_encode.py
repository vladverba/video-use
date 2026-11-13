import importlib.util
import json
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch


MODULE_PATH = Path(__file__).parents[1] / "helpers" / "render.py"
SPEC = importlib.util.spec_from_file_location("video_use_render", MODULE_PATH)
assert SPEC and SPEC.loader
render = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(render)


IPHONE_HLG = {
    "color_primaries": "bt2020",
    "color_transfer": "arib-std-b67",
    "color_space": "bt2020nc",
    "color_range": "tv",
}
SDR_709 = {
    "color_primaries": "bt709",
    "color_transfer": "bt709",
    "color_space": "bt709",
    "color_range": "tv",
}


def _probe_result(stream: dict) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        [], 0, stdout=json.dumps({"streams": [stream]}), stderr=""
    )


def _arg(args: list[str], flag: str) -> str:
    return args[args.index(flag) + 1]


class FinalVideoArgsTests(unittest.TestCase):
    def _args(self, stream: dict) -> list[str]:
        with patch.object(render.subprocess, "run", return_value=_probe_result(stream)):
            return render.final_video_args(Path("source.mov"))

    def test_hlg_source_keeps_hevc_10bit_hdr(self):
        args = self._args({**IPHONE_HLG, "bit_rate": "8557112"})
        self.assertEqual(_arg(args, "-c:v"), "hevc_videotoolbox")
        self.assertEqual(_arg(args, "-allow_sw"), "1")
        self.assertEqual(_arg(args, "-pix_fmt"), "p010le")
        self.assertEqual(_arg(args, "-profile:v"), "main10")
        self.assertEqual(_arg(args, "-tag:v"), "hvc1")
        self.assertEqual(_arg(args, "-color_primaries"), "bt2020")
        self.assertEqual(_arg(args, "-color_trc"), "arib-std-b67")
        self.assertEqual(_arg(args, "-colorspace"), "bt2020nc")
        self.assertEqual(_arg(args, "-color_range"), "tv")

    def test_sdr_source_is_not_tagged_as_hdr(self):
        args = self._args(SDR_709)
        self.assertEqual(_arg(args, "-color_trc"), "bt709")
        self.assertNotIn("arib-std-b67", " ".join(args))
        self.assertNotIn("bt2020", " ".join(args))

    def test_untagged_source_sets_no_color_flags(self):
        args = self._args({"color_transfer": "unknown"})
        for flag in ("-color_primaries", "-color_trc", "-colorspace", "-color_range"):
            self.assertNotIn(flag, args)

    def test_bitrate_is_above_source(self):
        args = self._args({**SDR_709, "bit_rate": "47866728"})  # 4K60 iPhone
        self.assertEqual(_arg(args, "-b:v"), str(int(47866728 * 1.25)))

    def test_low_or_unknown_source_bitrate_uses_floor(self):
        for stream in ({**SDR_709, "bit_rate": "2000000"}, SDR_709):
            with self.subTest(stream=stream):
                self.assertEqual(_arg(self._args(stream), "-b:v"), "12000000")

    def test_probe_failure_still_encodes(self):
        error = subprocess.CalledProcessError(1, ["ffprobe"])
        with patch.object(render.subprocess, "run", side_effect=error):
            args = render.final_video_args(Path("missing.mov"))
        self.assertNotIn("-color_trc", args)
        self.assertEqual(_arg(args, "-c:v"), "hevc_videotoolbox")
        self.assertEqual(_arg(args, "-b:v"), "12000000")


class PreferNativeTests(unittest.TestCase):
    def test_uses_homebrew_arm64_build_when_present(self):
        with patch.object(render.Path, "exists", return_value=True):
            self.assertEqual(render._prefer_native("ffmpeg"), "/opt/homebrew/bin/ffmpeg")

    def test_falls_back_to_path_lookup(self):
        with patch.object(render.Path, "exists", return_value=False):
            self.assertEqual(render._prefer_native("ffmpeg"), "ffmpeg")


if __name__ == "__main__":
    unittest.main()
