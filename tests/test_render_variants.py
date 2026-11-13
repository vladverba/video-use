import contextlib
import importlib.util
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


MODULE_PATH = Path(__file__).parents[1] / "helpers" / "render.py"
SPEC = importlib.util.spec_from_file_location("video_use_render", MODULE_PATH)
assert SPEC and SPEC.loader
render = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(render)


def _edl(**extra) -> dict:
    return {
        "sources": {"a": "a.mov"},
        "ranges": [{"source": "a", "start": 10, "end": 12},
                   {"source": "a", "start": 14, "end": 16}],
        "hooks": [
            {"name": "hook_1", "ranges": [{"source": "a", "start": 0, "end": 1}]},
            {"name": "hook_2", "ranges": [{"source": "a", "start": 2, "end": 3},
                                          {"source": "a", "start": 4, "end": 5}]},
            {"name": "hook_3", "ranges": [{"source": "a", "start": 6, "end": 7}]},
        ],
        **extra,
    }


class RenderPiecesTests(unittest.TestCase):
    def _run(self, edl: dict, measured=None, **kwargs):
        """Run render_pieces with extract/concat/loudness mocked.

        `measured` maps a prenorm file name to its (I, TP) loudness.
        Returns (outputs, extract calls, concat calls, gain calls)."""
        measured = measured or {}

        def fake_extract(edl_, edit_dir, preview, draft=False, fps=None,
                         ranges=None, prefix="seg"):
            ranges = edl_["ranges"] if ranges is None else ranges
            ext = ".mp4" if (preview or draft) else ".mov"
            return [Path(f"{prefix}_{i:02d}{ext}") for i in range(len(ranges))]

        def fake_measure(path):
            i, tp = measured.get(path.name, (-20.0, -10.0))
            return {"input_i": str(i), "input_tp": str(tp), "input_lra": "3",
                    "input_thresh": "-30", "target_offset": "0"}

        with tempfile.TemporaryDirectory() as d, \
             patch.object(render, "extract_all_segments", side_effect=fake_extract) as ex, \
             patch.object(render, "concat_segments") as cc, \
             patch.object(render, "measure_loudness", side_effect=fake_measure), \
             patch.object(render, "apply_gain") as gain, \
             patch.object(render, "probe_source_fps", return_value="30/1"), \
             patch.object(render, "ICLOUD_DRIVE", Path(d) / "no-icloud"), \
             contextlib.redirect_stdout(io.StringIO()):
            edit_dir = Path(d)
            outputs = render.render_pieces(edl, edit_dir, **kwargs)
            outputs = [o.relative_to(edit_dir) for o in outputs]
            concats = [(c.args[0], c.args[1].relative_to(edit_dir),
                        c.kwargs.get("encode_audio", False)) for c in cc.call_args_list]
            gains = [(c.args[0].relative_to(edit_dir), c.args[1].relative_to(edit_dir),
                      c.args[2]) for c in gain.call_args_list]
        return outputs, ex.call_args_list, concats, gains

    def test_each_hook_and_the_body_become_separate_files(self):
        outputs, extracts, concats, _ = self._run(_edl())
        prefixes = [c.kwargs["prefix"] for c in extracts]
        self.assertEqual(prefixes, ["body", "hook_1", "hook_2", "hook_3"])
        # Joined audio stays PCM (.mov); the gain step does the single AAC encode.
        self.assertEqual(concats, [
            ([Path("hook_1_00.mov")], Path("hook_1.prenorm.mov"), False),
            ([Path("hook_2_00.mov"), Path("hook_2_01.mov")], Path("hook_2.prenorm.mov"), False),
            ([Path("hook_3_00.mov")], Path("hook_3.prenorm.mov"), False),
            ([Path("body_00.mov"), Path("body_01.mov")], Path("body.prenorm.mov"), False),
        ])
        self.assertEqual(outputs, [Path("deliver/hook_1.mp4"), Path("deliver/hook_2.mp4"),
                                   Path("deliver/hook_3.mp4"), Path("deliver/body.mp4")])

    def test_every_piece_gets_the_same_gain_set_from_the_body(self):
        # Body at -20 LUFS -> +6 dB to reach -14. Hooks measure differently
        # but get the same +6 dB, so the stitch on the phone has no level jump.
        measured = {"body.prenorm.mov": (-20.0, -12.0),
                    "hook_1.prenorm.mov": (-18.0, -11.0)}
        _, _, _, gains = self._run(_edl(), measured=measured)
        self.assertEqual([g[1] for g in gains], [Path("deliver/hook_1.mp4"), Path("deliver/hook_2.mp4"),
                                                Path("deliver/hook_3.mp4"), Path("deliver/body.mp4")])
        self.assertEqual({round(g[2], 3) for g in gains}, {6.0})

    def test_gain_is_not_capped_by_peaks(self):
        # Quiet, peaky speech: +13 dB would push peaks past the ceiling, but
        # the limiter in apply_gain handles them, so the gain still reaches
        # -14 LUFS (capping it left a real recording ~5 dB short).
        measured = {"body.prenorm.mov": (-27.0, -10.0),
                    "hook_2.prenorm.mov": (-25.0, -5.0)}
        _, _, _, gains = self._run(_edl(), measured=measured)
        self.assertEqual({round(g[2], 3) for g in gains}, {13.0})

    def test_apply_gain_limits_peaks_with_identical_settings(self):
        with patch.object(render.subprocess, "run") as run, \
             patch.object(render, "_enforce_true_peak_ceiling"), \
             contextlib.redirect_stdout(io.StringIO()):
            render.apply_gain(Path("a.mov"), Path("a.mp4"), 13.0)
        af = run.call_args.args[0][run.call_args.args[0].index("-af") + 1]
        self.assertTrue(af.startswith("volume=13.000dB,alimiter="), af)

    def test_without_loudnorm_concat_encodes_audio_once_to_final_mp4(self):
        outputs, _, concats, gains = self._run(_edl(), loudnorm=False)
        self.assertEqual(gains, [])
        self.assertEqual([(c[1], c[2]) for c in concats],
                         [(Path(f"deliver/hook_{i}.mp4"), True) for i in (1, 2, 3)]
                         + [(Path("deliver/body.mp4"), True)])

    def test_every_piece_shares_one_rate(self):
        _, extracts, _, _ = self._run(_edl())
        self.assertEqual({c.kwargs["fps"] for c in extracts}, {"30/1"})

    def test_grade_is_dropped(self):
        _, extracts, _, _ = self._run(_edl(grade="warm_cinematic"))
        self.assertTrue(all(c.args[0]["grade"] is None for c in extracts))

    def test_preview_outputs_do_not_overwrite_finals(self):
        outputs, _, _, _ = self._run(_edl(), preview=True)
        # Previews are for checking, not posting: they stay out of deliver/.
        self.assertEqual(outputs[0], Path("hook_1_preview.mp4"))
        self.assertEqual(outputs[-1], Path("body_preview.mp4"))

    def test_deliver_holds_only_final_videos_and_transcripts(self):
        captured = {}

        def fake_transcript(edl_, hook, edit_dir, out_dir=None):
            captured.setdefault("dirs", []).append(out_dir)

        with patch.object(render, "write_variant_transcript", side_effect=fake_transcript):
            self._run(_edl())
        self.assertEqual({d.name for d in captured["dirs"]}, {"deliver"})

    def test_invalid_hooks_are_rejected(self):
        bad = [
            _edl(hooks=[]),
            _edl(ranges=[]),
            _edl(hooks=[{"name": "../evil", "ranges": [{"source": "a", "start": 0, "end": 1}]}]),
            _edl(hooks=[{"name": "h", "ranges": []}]),
            _edl(hooks=[{"name": "h", "ranges": [{"source": "a", "start": 0, "end": 1}]},
                        {"name": "h", "ranges": [{"source": "a", "start": 1, "end": 2}]}]),
            # "body" would overwrite the shared body file.
            _edl(hooks=[{"name": "body", "ranges": [{"source": "a", "start": 0, "end": 1}]}]),
        ]
        for edl in bad:
            with self.subTest(hooks=edl["hooks"], ranges=edl["ranges"]):
                with self.assertRaises(SystemExit):
                    self._run(edl)


class PublishToICloudTests(unittest.TestCase):
    def _deliver(self, root: Path) -> Path:
        d = root / "img1" / "edit" / "deliver"
        d.mkdir(parents=True)
        for n in ("hook_1.mp4", "body.mp4", "hook_1_transcript.md"):
            (d / n).write_text(n)
        return d

    def test_copies_deliver_files_into_reels_slug_folder(self):
        with tempfile.TemporaryDirectory() as t, contextlib.redirect_stdout(io.StringIO()):
            root, icloud = Path(t), Path(t) / "icloud"
            icloud.mkdir()
            deliver = self._deliver(root)
            dest = render.publish_to_icloud(deliver, icloud)
            self.assertEqual(dest, icloud / "Reels" / "img1")
            self.assertEqual(sorted(p.name for p in dest.iterdir()),
                             ["body.mp4", "hook_1.mp4", "hook_1_transcript.md"])
            # A copy: the local deliver/ folder keeps its files.
            self.assertEqual(len(list(deliver.iterdir())), 3)

    def test_rerender_replaces_files_in_place(self):
        with tempfile.TemporaryDirectory() as t, contextlib.redirect_stdout(io.StringIO()):
            root, icloud = Path(t), Path(t) / "icloud"
            icloud.mkdir()
            deliver = self._deliver(root)
            render.publish_to_icloud(deliver, icloud)
            (deliver / "body.mp4").write_text("v2")
            dest = render.publish_to_icloud(deliver, icloud)
            self.assertEqual((dest / "body.mp4").read_text(), "v2")

    def test_missing_or_blocked_icloud_warns_and_returns_none(self):
        with tempfile.TemporaryDirectory() as t:
            deliver = self._deliver(Path(t))
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertIsNone(render.publish_to_icloud(deliver, Path(t) / "nope"))
            self.assertIn("iCloud", out.getvalue())

    def test_final_render_publishes_but_preview_does_not(self):
        runs = RenderPiecesTests()
        with patch.object(render, "publish_to_icloud") as pub:
            runs._run(_edl())
            self.assertEqual(pub.call_count, 1)
            self.assertEqual(pub.call_args.args[0].name, "deliver")
        with patch.object(render, "publish_to_icloud") as pub:
            runs._run(_edl(), preview=True)
            pub.assert_not_called()


def _w(text, start, end):
    return {"text": text, "start": start, "end": end, "type": "word"}


class VariantTranscriptTests(unittest.TestCase):
    TRANSCRIPT = {"words": [
        _w("Hook", 0.1, 0.4), {"text": " ", "start": 0.4, "end": 0.5, "type": "spacing"},
        _w("one.", 0.5, 0.9),
        _w("Skipped", 1.5, 1.9),             # between ranges: must not appear
        _w("Body", 10.1, 10.5), _w("starts", 10.6, 11.0),
        _w("here.", 14.2, 14.8),
        _w("Outtake", 20.0, 20.5),           # after the body: must not appear
    ]}

    def _write(self, edit_dir: Path):
        import json
        (edit_dir / "transcripts").mkdir()
        (edit_dir / "transcripts" / "a.json").write_text(json.dumps(self.TRANSCRIPT))

    def test_hook_words_then_body_words_only(self):
        edl = _edl()
        hook = {"name": "hook_1", "ranges": [{"source": "a", "start": 0, "end": 1}]}
        with tempfile.TemporaryDirectory() as d:
            edit_dir = Path(d)
            self._write(edit_dir)
            path = render.write_variant_transcript(edl, hook, edit_dir, edit_dir / "deliver")
            text = path.read_text()
        self.assertEqual(path.relative_to(edit_dir), Path("deliver/hook_1_transcript.md"))
        self.assertEqual(text, "Hook one.\n\nBody starts here.\n")

    def test_missing_transcript_returns_none_without_writing(self):
        hook = {"name": "hook_1", "ranges": [{"source": "a", "start": 0, "end": 1}]}
        with tempfile.TemporaryDirectory() as d, contextlib.redirect_stdout(io.StringIO()):
            self.assertIsNone(render.write_variant_transcript(_edl(), hook, Path(d)))
            self.assertEqual(list(Path(d).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
