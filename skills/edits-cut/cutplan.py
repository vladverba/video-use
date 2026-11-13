"""Cut-plan arithmetic for the edits-cut skill.

The skill drives Instagram Edits through its MCP; this module does the parts
that are easy to get wrong by hand: validating the cut list, correcting cut
edges against detected pauses, padding + frame snapping, split points, and
checking the built timeline against the plan. Stdlib only.
"""

import argparse
import json
import math
import re
import sys
from pathlib import Path

HOOK_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")


class CutlistError(ValueError):
    """The cut list (or an MCP response checked against it) is invalid."""


def _check_range(r: dict, label: str, duration: float) -> None:
    for key in ("start", "end"):
        if not isinstance(r.get(key), (int, float)):
            raise CutlistError(f"{label}: missing numeric '{key}'")
    if r["end"] <= r["start"]:
        raise CutlistError(f"{label}: end must be after start ({r['start']}-{r['end']})")
    if r["start"] < 0 or r["end"] > duration:
        raise CutlistError(f"{label}: range {r['start']}-{r['end']} is outside 0-{duration}")


def _check_ordered(ranges: list, label: str) -> None:
    for prev, cur in zip(ranges, ranges[1:]):
        if cur["start"] < prev["start"]:
            raise CutlistError(f"{label}: ranges are not in source order")
        if cur["start"] < prev["end"]:
            raise CutlistError(f"{label}: ranges overlap ({prev['start']}-{prev['end']} and {cur['start']}-{cur['end']})")


def correct_edges(ranges: list, pauses: list, tolerance: float = 0.03,
                  start_tolerance: float = 0.15) -> tuple[list, list]:
    """Move cut edges that the transcript placed inside a detected pause.

    ASR stretches the last word before a pause and stamps the first word after
    one too early. An end inside a pause is pulled back to where the pause
    starts; a start inside a pause is pushed forward to where it ends. Ends
    within `tolerance` of the pause boundary are left alone; starts get the
    larger `start_tolerance`, because a pause threshold ends 30-80ms after a
    soft word onset really begins (measured on a real 4K60 recording) and correcting that
    jitter would eat the lead padding.
    Returns (new_ranges, corrections) and never mutates the input.
    """
    out, corrections = [], []
    for i, r in enumerate(ranges):
        r = dict(r)
        for p in pauses:
            if p["start"] < r["end"] < p["end"] and r["end"] - p["start"] > tolerance:
                corrections.append({"range": i, "edge": "end", "old": r["end"], "new": p["start"]})
                r["end"] = p["start"]
            if p["start"] < r["start"] < p["end"] and p["end"] - r["start"] > start_tolerance:
                corrections.append({"range": i, "edge": "start", "old": r["start"], "new": p["end"]})
                r["start"] = p["end"]
        if r["end"] <= r["start"]:
            raise CutlistError(f"range {i} ({ranges[i]['start']}-{ranges[i]['end']}) lies inside a pause; check the transcript")
        out.append(r)
    return out, corrections


_EPS = 1e-6  # absorbs float noise like 4.7333333 * 60 = 283.99999999


def pad_and_snap(ranges: list, fps: float, duration: float,
                 lead: float = 0.05, trail: float = 0.08) -> list:
    """Pad each range (lead before, trail after) and snap it to the frame grid.

    Starts snap down and ends snap up, so padding is never lost. Neighbours
    whose padding would overlap meet at the middle of their original gap.
    Ranges must already be in source order. Never mutates the input.
    """
    out = [dict(r) for r in ranges]
    padded = [(r["start"] - lead, r["end"] + trail) for r in ranges]
    for r, (s, e) in zip(out, padded):
        r["start"] = max(0.0, math.floor(s * fps + _EPS) / fps)
        r["end"] = min(duration, math.ceil(e * fps - _EPS) / fps)
    for i in range(len(out) - 1):
        if out[i]["end"] > out[i + 1]["start"]:
            mid = (ranges[i]["end"] + ranges[i + 1]["start"]) / 2
            out[i]["end"] = out[i + 1]["start"] = round(mid * fps) / fps
    return out


def split_plan(ranges: list, duration: float) -> dict:
    """Split points on a clip spanning 0-duration, and which pieces to keep.

    Ranges are padded/snapped, in source order and non-overlapping.
    """
    times = []
    for r in ranges:
        for t in (r["start"], r["end"]):
            if _EPS < t < duration - _EPS and not any(abs(t - x) < _EPS for x in times):
                times.append(t)
    times.sort()
    bounds = [0.0, *times, duration]
    pieces = []
    for s, e in zip(bounds, bounds[1:]):
        keep = any(r["start"] - _EPS <= s and e <= r["end"] + _EPS for r in ranges)
        pieces.append({"start": s, "end": e, "keep": keep})
    return {"times": times, "pieces": pieces}


def match_pieces(plan: dict, split_response: dict, fps: float) -> dict:
    """Map a splitAt response onto the plan's pieces: which clip ids to delete/keep."""
    if split_response.get("skipped"):
        raise CutlistError(f"splitAt skipped some times: {split_response['skipped']}")
    expected, got = plan["pieces"], split_response.get("pieces") or []
    if len(got) != len(expected):
        raise CutlistError(f"splitAt returned {len(got)} pieces, plan expects {len(expected)}")
    half = 0.5 / fps
    ids = {"delete": [], "keep": []}
    for i, (want, have) in enumerate(zip(expected, got)):
        if abs(have["start"] - want["start"]) > half or abs(have["end"] - want["end"]) > half:
            raise CutlistError(
                f"piece {i} is {have['start']:.4f}-{have['end']:.4f}, "
                f"plan expects {want['start']:.4f}-{want['end']:.4f}")
        ids["keep" if want["keep"] else "delete"].append(have["id"])
    return ids


def verify_clips(plan: dict, clips_response: dict, fps: float) -> list:
    """Compare the main track (a `clips` response) with the plan's kept pieces.

    Returns a list of human-readable problems; empty means the timeline matches.
    """
    frame = 1.0 / fps
    want = [p for p in plan["pieces"] if p["keep"]]
    have = sorted(clips_response.get("clips") or [], key=lambda c: c["startTime"])
    if len(have) != len(want):
        return [f"timeline has {len(have)} clips, plan expects {len(want)}"]
    problems = []
    prev_end = 0.0
    for i, (w, c) in enumerate(zip(want, have)):
        if abs(c["sourceInPoint"] - w["start"]) > frame or abs(c["sourceOutPoint"] - w["end"]) > frame:
            problems.append(
                f"clip {i}: source {c['sourceInPoint']:.4f}-{c['sourceOutPoint']:.4f}, "
                f"plan expects {w['start']:.4f}-{w['end']:.4f}")
        expected_len = w["end"] - w["start"]
        if abs(c["duration"] - expected_len) > frame:
            problems.append(f"clip {i}: duration {c['duration']:.4f}, expected {expected_len:.4f}")
        if abs(c["startTime"] - prev_end) > frame:
            problems.append(f"clip {i}: gap/overlap, starts at {c['startTime']:.4f}, previous ends at {prev_end:.4f}")
        prev_end = c["endTime"]
    return problems


def load_cutlist(path: str | Path) -> dict:
    data = _read_json(path)
    for key in ("source", "duration", "fps"):
        if key not in data:
            raise CutlistError(f"cut list is missing '{key}'")
    for key in ("duration", "fps"):
        if isinstance(data[key], bool) or not isinstance(data[key], (int, float)) or data[key] <= 0:
            raise CutlistError(f"cut list '{key}' must be a positive number, got {data[key]!r}")
    duration = data["duration"]
    hooks = data.get("hooks") or []
    body = data.get("body") or []
    if not hooks:
        raise CutlistError("cut list needs at least one hook")
    if not body:
        raise CutlistError("cut list needs at least one body range")

    for i, r in enumerate(body):
        _check_range(r, f"body[{i}]", duration)
    _check_ordered(body, "body")
    body_start = body[0]["start"]

    names, styles, colors = set(), set(), set()
    for h in hooks:
        name = h.get("name", "")
        if not HOOK_NAME_RE.match(name):
            raise CutlistError(f"invalid hook name {name!r} (allowed: letters, digits, _ and -)")
        if name in names:
            raise CutlistError(f"duplicate hook name {name!r}")
        names.add(name)
        style = h.get("caption_style")
        color = (h.get("highlight_color") or "").upper()
        if not style or not color:
            raise CutlistError(f"{name}: needs caption_style and highlight_color")
        if style in styles:
            raise CutlistError(f"{name}: duplicate caption_style {style!r}")
        if color in colors:
            raise CutlistError(f"{name}: duplicate highlight_color {color}")
        styles.add(style)
        colors.add(color)
        ranges = h.get("ranges") or []
        if not ranges:
            raise CutlistError(f"{name}: needs at least one range")
        for i, r in enumerate(ranges):
            _check_range(r, f"{name}.ranges[{i}]", duration)
        _check_ordered(ranges, name)
        if ranges[-1]["end"] > body_start:
            raise CutlistError(f"{name}: hook ranges must end before the first body range ({body_start})")
    return data


def build_plan(cutlist: dict, pauses: list) -> dict:
    """One split plan per hook: that hook's ranges followed by the shared body."""
    fps, duration = cutlist["fps"], cutlist["duration"]
    hooks = {}
    for h in cutlist["hooks"]:
        ranges, corrections = correct_edges(h["ranges"] + cutlist["body"], pauses)
        ranges = pad_and_snap(ranges, fps, duration)
        hooks[h["name"]] = {
            "caption_style": h["caption_style"],
            "highlight_color": h["highlight_color"],
            "corrections": corrections,
            "ranges": ranges,
            **split_plan(ranges, duration),
        }
    return {"source": cutlist["source"], "duration": duration, "fps": fps, "hooks": hooks}


def _read_json(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise CutlistError(f"cannot read {path}: {e}") from e


def _hook_plan(plan: dict, hook: str) -> dict:
    if hook not in plan.get("hooks", {}):
        raise CutlistError(f"no hook {hook!r} in plan (have: {', '.join(plan.get('hooks', {}))})")
    return plan["hooks"][hook]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan", help="cut list + pauses -> per-hook split plans (JSON)")
    p.add_argument("cutlist")
    p.add_argument("--pauses", required=True, help="findPauses output: a list of {start, end} or {pauses: [...]}")
    p = sub.add_parser("pieces", help="splitAt response -> clip ids to delete/keep")
    p.add_argument("plan")
    p.add_argument("hook")
    p.add_argument("split_response")
    p = sub.add_parser("verify", help="clips response -> mismatches against the plan")
    p.add_argument("plan")
    p.add_argument("hook")
    p.add_argument("clips")
    args = parser.parse_args(argv)

    try:
        if args.cmd == "plan":
            pauses = _read_json(args.pauses)
            if isinstance(pauses, dict):
                pauses = pauses.get("pauses", [])
            print(json.dumps(build_plan(load_cutlist(args.cutlist), pauses), indent=2))
            return 0
        plan = _read_json(args.plan)
        hook = _hook_plan(plan, args.hook)
        if args.cmd == "pieces":
            print(json.dumps(match_pieces(hook, _read_json(args.split_response), plan["fps"])))
            return 0
        problems = verify_clips(hook, _read_json(args.clips), plan["fps"])
        print("\n".join(problems) if problems else "OK")
        return 1 if problems else 0
    except CutlistError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
