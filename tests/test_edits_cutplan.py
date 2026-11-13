import contextlib
import copy
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).parents[1] / "skills" / "edits-cut" / "cutplan.py"
SPEC = importlib.util.spec_from_file_location("edits_cutplan", MODULE_PATH)
assert SPEC and SPEC.loader
cutplan = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cutplan)


def _cutlist(**overrides) -> dict:
    data = {
        "source": "/Users/x/Movies/reels/demo/IMG.MOV",
        "duration": 60.0,
        "fps": 60,
        "hooks": [
            {"name": "hook_1", "caption_style": "Word Highlight Box",
             "highlight_color": "#D300C5",
             "ranges": [{"start": 1.0, "end": 3.0, "quote": "a", "reason": "r"}]},
            {"name": "hook_2", "caption_style": "Short Pop",
             "highlight_color": "#FFD400",
             "ranges": [{"start": 4.0, "end": 6.0, "quote": "b", "reason": "r"}]},
        ],
        "body": [
            {"start": 10.0, "end": 12.0, "quote": "c", "reason": "r"},
            {"start": 14.0, "end": 16.0, "quote": "d", "reason": "r"},
        ],
    }
    data.update(overrides)
    return data


class LoadCutlistTest(unittest.TestCase):
    def _load(self, data: dict) -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cutlist.json"
            path.write_text(json.dumps(data))
            return cutplan.load_cutlist(path)

    def _assert_rejects(self, data: dict, fragment: str) -> None:
        with self.assertRaises(cutplan.CutlistError) as ctx:
            self._load(data)
        self.assertIn(fragment, str(ctx.exception))

    def test_valid_cutlist_loads(self):
        loaded = self._load(_cutlist())
        self.assertEqual([h["name"] for h in loaded["hooks"]], ["hook_1", "hook_2"])
        self.assertEqual(len(loaded["body"]), 2)

    def test_rejects_end_not_after_start(self):
        data = _cutlist()
        data["body"][0]["end"] = data["body"][0]["start"]
        self._assert_rejects(data, "end must be after start")

    def test_rejects_range_outside_duration(self):
        data = _cutlist()
        data["body"][1]["end"] = 61.0
        self._assert_rejects(data, "outside")
        data = _cutlist()
        data["hooks"][0]["ranges"][0]["start"] = -0.5
        self._assert_rejects(data, "outside")

    def test_rejects_body_out_of_source_order(self):
        data = _cutlist()
        data["body"].reverse()
        self._assert_rejects(data, "source order")

    def test_rejects_overlapping_body_ranges(self):
        data = _cutlist()
        data["body"][1]["start"] = 11.5
        self._assert_rejects(data, "overlap")

    def test_rejects_hook_range_not_before_body(self):
        data = _cutlist()
        data["hooks"][1]["ranges"][0] = {"start": 11.0, "end": 13.0}
        self._assert_rejects(data, "before the first body range")

    def test_rejects_invalid_or_duplicate_hook_names(self):
        data = _cutlist()
        data["hooks"][0]["name"] = "hook 1"
        self._assert_rejects(data, "invalid hook name")
        data = _cutlist()
        data["hooks"][1]["name"] = "hook_1"
        self._assert_rejects(data, "duplicate hook name")

    def test_rejects_duplicate_caption_style_or_color(self):
        data = _cutlist()
        data["hooks"][1]["caption_style"] = "Word Highlight Box"
        self._assert_rejects(data, "duplicate caption_style")
        data = _cutlist()
        data["hooks"][1]["highlight_color"] = "#d300c5"
        self._assert_rejects(data, "duplicate highlight_color")

    def test_rejects_non_positive_or_non_numeric_fps_and_duration(self):
        for key, value in (("fps", 0), ("fps", -60), ("duration", 0), ("duration", "60")):
            self._assert_rejects(_cutlist(**{key: value}), key)

    def test_rejects_missing_fields(self):
        data = _cutlist()
        del data["fps"]
        self._assert_rejects(data, "fps")
        data = _cutlist(hooks=[])
        self._assert_rejects(data, "at least one hook")


class CorrectEdgesTest(unittest.TestCase):
    PAUSES = [{"start": 3.0, "end": 5.0}, {"start": 9.0, "end": 12.0}]

    def test_end_inside_pause_pulled_to_pause_start(self):
        ranges, corrections = cutplan.correct_edges([{"start": 1.0, "end": 4.2}], self.PAUSES)
        self.assertEqual(ranges[0]["end"], 3.0)
        self.assertEqual(corrections, [{"range": 0, "edge": "end", "old": 4.2, "new": 3.0}])

    def test_start_inside_pause_pushed_to_pause_end(self):
        ranges, corrections = cutplan.correct_edges([{"start": 9.5, "end": 14.0}], self.PAUSES)
        self.assertEqual(ranges[0]["start"], 12.0)
        self.assertEqual(corrections, [{"range": 0, "edge": "start", "old": 9.5, "new": 12.0}])

    def test_edges_in_speech_unchanged(self):
        original = [{"start": 5.5, "end": 8.5, "quote": "q"}]
        ranges, corrections = cutplan.correct_edges(copy.deepcopy(original), self.PAUSES)
        self.assertEqual(ranges, original)
        self.assertEqual(corrections, [])

    def test_edge_within_30ms_of_pause_boundary_unchanged(self):
        ranges, corrections = cutplan.correct_edges(
            [{"start": 4.98, "end": 9.02}], self.PAUSES)
        self.assertEqual(ranges[0], {"start": 4.98, "end": 9.02})
        self.assertEqual(corrections, [])

    def test_start_jitter_under_150ms_left_alone(self):
        # Real recording case: "Sensor" stamped 263.28, -35dB pause ends 263.36.
        ranges, corrections = cutplan.correct_edges(
            [{"start": 263.28, "end": 267.0}], [{"start": 261.6, "end": 263.36}])
        self.assertEqual(ranges[0]["start"], 263.28)
        self.assertEqual(corrections, [])

    def test_start_more_than_150ms_inside_pause_corrected(self):
        ranges, _ = cutplan.correct_edges(
            [{"start": 4.8, "end": 8.0}], self.PAUSES)
        self.assertEqual(ranges[0]["start"], 5.0)

    def test_range_entirely_inside_a_pause_is_an_error(self):
        with self.assertRaises(cutplan.CutlistError):
            cutplan.correct_edges([{"start": 9.5, "end": 11.0}], self.PAUSES)

    def test_does_not_mutate_input(self):
        original = [{"start": 1.0, "end": 4.2}]
        cutplan.correct_edges(original, self.PAUSES)
        self.assertEqual(original[0]["end"], 4.2)


class PadAndSnapTest(unittest.TestCase):
    def test_applies_lead_and_trail_on_frame_grid(self):
        out = cutplan.pad_and_snap([{"start": 1.0, "end": 2.0}], fps=60, duration=10.0)
        self.assertAlmostEqual(out[0]["start"], 0.95)
        self.assertAlmostEqual(out[0]["end"], 2.08333333, places=6)  # 2.08 -> ceil to 125/60

    def test_start_snaps_down_end_snaps_up(self):
        out = cutplan.pad_and_snap([{"start": 1.006, "end": 2.001}], fps=60, duration=10.0,
                                   lead=0.0, trail=0.0)
        self.assertAlmostEqual(out[0]["start"], 60 / 60)
        self.assertAlmostEqual(out[0]["end"], 121 / 60)

    def test_values_already_on_grid_stay_put(self):
        out = cutplan.pad_and_snap([{"start": 284 / 60, "end": 537 / 60}], fps=60,
                                   duration=60.0, lead=0.0, trail=0.0)
        self.assertAlmostEqual(out[0]["start"], 284 / 60)
        self.assertAlmostEqual(out[0]["end"], 537 / 60)

    def test_clamped_to_clip(self):
        out = cutplan.pad_and_snap([{"start": 0.02, "end": 9.95}], fps=60, duration=10.0)
        self.assertEqual(out[0]["start"], 0.0)
        self.assertEqual(out[0]["end"], 10.0)

    def test_overlapping_neighbours_meet_in_the_gap(self):
        # gap 2.0-2.1; padded they'd overlap (2.08 > 2.05), so both meet at 2.05
        out = cutplan.pad_and_snap([{"start": 1.0, "end": 2.0}, {"start": 2.1, "end": 3.0}],
                                   fps=60, duration=10.0)
        self.assertAlmostEqual(out[0]["end"], 123 / 60)   # 2.05 rounded to grid
        self.assertEqual(out[0]["end"], out[1]["start"])

    def test_keeps_other_fields_and_does_not_mutate(self):
        original = [{"start": 1.0, "end": 2.0, "quote": "q"}]
        out = cutplan.pad_and_snap(original, fps=60, duration=10.0)
        self.assertEqual(out[0]["quote"], "q")
        self.assertEqual(original[0]["start"], 1.0)


class SplitPlanTest(unittest.TestCase):
    def test_times_and_pieces(self):
        plan = cutplan.split_plan([{"start": 2.0, "end": 4.0}, {"start": 6.0, "end": 8.0}],
                                  duration=10.0)
        self.assertEqual(plan["times"], [2.0, 4.0, 6.0, 8.0])
        self.assertEqual([(p["start"], p["end"], p["keep"]) for p in plan["pieces"]], [
            (0.0, 2.0, False), (2.0, 4.0, True), (4.0, 6.0, False),
            (6.0, 8.0, True), (8.0, 10.0, False)])

    def test_ranges_touching_clip_edges_add_no_gap_piece(self):
        plan = cutplan.split_plan([{"start": 0.0, "end": 3.0}, {"start": 5.0, "end": 10.0}],
                                  duration=10.0)
        self.assertEqual(plan["times"], [3.0, 5.0])
        self.assertEqual([p["keep"] for p in plan["pieces"]], [True, False, True])

    def test_adjacent_ranges_share_one_split(self):
        plan = cutplan.split_plan([{"start": 1.0, "end": 2.0}, {"start": 2.0, "end": 3.0}],
                                  duration=10.0)
        self.assertEqual(plan["times"], [1.0, 2.0, 3.0])
        self.assertEqual([p["keep"] for p in plan["pieces"]], [False, True, True, False])


class MatchPiecesTest(unittest.TestCase):
    # Shape of a real splitAt response from the 2026-11-08 spike.
    PLAN = cutplan.split_plan([{"start": 284 / 60, "end": 537 / 60},
                               {"start": 3217 / 60, "end": 3467 / 60}], duration=60.0)
    RESPONSE = {
        "ok": True, "splitCount": 4, "skipped": [],
        "pieces": [
            {"start": 0, "id": "A", "end": 4.7333333333333334},
            {"start": 4.7333333333333334, "id": "B", "end": 8.9499999999999993},
            {"start": 8.9499999999999993, "id": "C", "end": 53.616666666666667},
            {"start": 53.616666666666667, "id": "D", "end": 57.783333333333331},
            {"start": 57.783333333333331, "id": "E", "end": 60},
        ],
    }

    def test_returns_delete_and_keep_ids_in_order(self):
        ids = cutplan.match_pieces(self.PLAN, self.RESPONSE, fps=60)
        self.assertEqual(ids, {"delete": ["A", "C", "E"], "keep": ["B", "D"]})

    def test_piece_count_mismatch_is_an_error(self):
        response = copy.deepcopy(self.RESPONSE)
        response["pieces"].pop()
        with self.assertRaises(cutplan.CutlistError):
            cutplan.match_pieces(self.PLAN, response, fps=60)

    def test_boundary_off_by_more_than_half_a_frame_is_an_error(self):
        response = copy.deepcopy(self.RESPONSE)
        response["pieces"][1]["end"] += 1 / 60
        with self.assertRaises(cutplan.CutlistError) as ctx:
            cutplan.match_pieces(self.PLAN, response, fps=60)
        self.assertIn("piece 1", str(ctx.exception))

    def test_skipped_splits_are_an_error(self):
        response = copy.deepcopy(self.RESPONSE)
        response["skipped"] = [8.95]
        with self.assertRaises(cutplan.CutlistError):
            cutplan.match_pieces(self.PLAN, response, fps=60)


class VerifyClipsTest(unittest.TestCase):
    PLAN = MatchPiecesTest.PLAN

    def _clips(self) -> dict:
        a_len = (537 - 284) / 60
        b_len = (3467 - 3217) / 60
        return {"ok": True, "total": 2, "clips": [
            {"id": "B", "sourceInPoint": 284 / 60, "sourceOutPoint": 537 / 60,
             "startTime": 0, "endTime": a_len, "duration": a_len},
            {"id": "D", "sourceInPoint": 3217 / 60, "sourceOutPoint": 3467 / 60,
             "startTime": a_len, "endTime": a_len + b_len, "duration": b_len},
        ]}

    def test_matching_timeline_has_no_mismatches(self):
        self.assertEqual(cutplan.verify_clips(self.PLAN, self._clips(), 60), [])

    def test_reports_wrong_clip_count(self):
        clips = self._clips()
        clips["clips"].append(dict(clips["clips"][-1]))
        problems = cutplan.verify_clips(self.PLAN, clips, 60)
        self.assertTrue(any("3 clips" in p for p in problems), problems)

    def test_reports_source_points_off_by_more_than_a_frame(self):
        clips = self._clips()
        clips["clips"][1]["sourceInPoint"] -= 2 / 60
        problems = cutplan.verify_clips(self.PLAN, clips, 60)
        self.assertTrue(any("clip 1" in p and "source" in p for p in problems), problems)

    def test_reports_wrong_order(self):
        clips = self._clips()
        a, b = clips["clips"]
        a["sourceInPoint"], b["sourceInPoint"] = b["sourceInPoint"], a["sourceInPoint"]
        a["sourceOutPoint"], b["sourceOutPoint"] = b["sourceOutPoint"], a["sourceOutPoint"]
        self.assertNotEqual(cutplan.verify_clips(self.PLAN, clips, 60), [])

    def test_reports_wrong_duration(self):
        # e.g. a clip whose speed was changed in Edits
        clips = self._clips()
        clips["clips"][0]["duration"] /= 1.1
        problems = cutplan.verify_clips(self.PLAN, clips, 60)
        self.assertTrue(any("clip 0" in p and "duration" in p for p in problems), problems)

    def test_reports_gap_between_clips(self):
        clips = self._clips()
        clips["clips"][1]["startTime"] += 0.5
        clips["clips"][1]["endTime"] += 0.5
        problems = cutplan.verify_clips(self.PLAN, clips, 60)
        self.assertTrue(any("gap" in p for p in problems), problems)


class CliTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _write(self, name: str, data) -> str:
        path = self.dir / name
        path.write_text(json.dumps(data))
        return str(path)

    def _run(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cutplan.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def _plan(self) -> str:
        cutlist = self._write("cutlist.json", _cutlist())
        # Pause 2.5-3.9 stretches hook_1's last word (end 3.0 -> 2.5).
        pauses = self._write("pauses.json", [{"start": 2.5, "end": 3.9}])
        code, out, err = self._run("plan", cutlist, "--pauses", pauses)
        self.assertEqual(code, 0, err)
        return out

    def test_plan_prints_one_plan_per_hook(self):
        plan = json.loads(self._plan())
        self.assertEqual(list(plan["hooks"]), ["hook_1", "hook_2"])
        h1 = plan["hooks"]["hook_1"]
        self.assertEqual(h1["caption_style"], "Word Highlight Box")
        self.assertEqual(h1["corrections"], [{"range": 0, "edge": "end", "old": 3.0, "new": 2.5}])
        self.assertEqual(len(h1["ranges"]), 3)  # hook range + 2 body ranges
        self.assertAlmostEqual(h1["ranges"][0]["end"], 155 / 60)  # 2.5 + 0.08, ceil to frame
        self.assertEqual([p["keep"] for p in h1["pieces"]],
                         [False, True, False, True, False, True, False])
        self.assertEqual(plan["fps"], 60)
        self.assertNotIn("speed", plan)

    def test_plan_accepts_wrapped_pauses(self):
        cutlist = self._write("cutlist.json", _cutlist())
        pauses = self._write("pauses.json", {"ok": True, "pauses": [{"start": 2.5, "end": 3.9}]})
        code, out, _ = self._run("plan", cutlist, "--pauses", pauses)
        self.assertEqual(code, 0)
        self.assertEqual(len(json.loads(out)["hooks"]["hook_1"]["corrections"]), 1)

    def test_pieces_prints_delete_and_keep_ids(self):
        plan = json.loads(self._plan())
        plan_path = self._write("plan.json", plan)
        pieces = plan["hooks"]["hook_2"]["pieces"]
        response = {"ok": True, "skipped": [], "pieces": [
            {"id": f"p{i}", "start": p["start"], "end": p["end"]} for i, p in enumerate(pieces)]}
        code, out, err = self._run("pieces", plan_path, "hook_2",
                                   self._write("split.json", response))
        self.assertEqual(code, 0, err)
        ids = json.loads(out)
        self.assertEqual(ids["keep"], ["p1", "p3", "p5"])
        self.assertEqual(ids["delete"], ["p0", "p2", "p4", "p6"])

    def _clips_for(self, plan: dict, hook: str, speed: float = 1.0) -> dict:
        clips, t = [], 0.0
        for p in plan["hooks"][hook]["pieces"]:
            if p["keep"]:
                d = (p["end"] - p["start"]) / speed
                clips.append({"sourceInPoint": p["start"], "sourceOutPoint": p["end"],
                              "startTime": t, "endTime": t + d, "duration": d})
                t += d
        return {"ok": True, "clips": clips}

    def test_verify_passes_and_fails(self):
        plan = json.loads(self._plan())
        plan_path = self._write("plan.json", plan)
        good = self._write("good.json", self._clips_for(plan, "hook_1"))
        code, out, _ = self._run("verify", plan_path, "hook_1", good)
        self.assertEqual(code, 0)
        self.assertIn("OK", out)
        sped_up = self._write("sped.json", self._clips_for(plan, "hook_1", 1.1))
        code, out, _ = self._run("verify", plan_path, "hook_1", sped_up)
        self.assertEqual(code, 1)
        self.assertIn("duration", out)

    def test_unreadable_cutlist_exits_2_with_message(self):
        pauses = self._write("pauses.json", [])
        bad = self.dir / "bad.json"
        bad.write_text("{bad json")
        for path in (str(bad), str(self.dir / "missing.json")):
            code, _, err = self._run("plan", path, "--pauses", pauses)
            self.assertEqual(code, 2, err)
            self.assertIn("cannot read", err)

    def test_bad_input_exits_nonzero_with_message(self):
        bad = _cutlist()
        bad["body"].reverse()
        cutlist = self._write("cutlist.json", bad)
        pauses = self._write("pauses.json", [])
        code, _, err = self._run("plan", cutlist, "--pauses", pauses)
        self.assertEqual(code, 2)
        self.assertIn("source order", err)
        plan_path = self._write("plan.json", json.loads(self._plan()))
        code, _, err = self._run("verify", plan_path, "hook_9", pauses)
        self.assertEqual(code, 2)
        self.assertIn("hook_9", err)


if __name__ == "__main__":
    unittest.main()
