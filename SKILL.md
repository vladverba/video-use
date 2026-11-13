---
name: video-use
description: Cut a talking-head recording into short-form pieces the user stitches on their phone — one clip per hook plus one shared body clip. The recording has 3–5 hooks first (each possibly retaken), then the body with multiple takes per line. Transcribe, pick the best take of every line, render each hook and the body as separate files. Cuts only (no grade, no backgrounds, no subtitles by default), 1.1x speed, loudness-normalized, source quality preserved (native resolution, frame rate and HDR).
---

# Video Use

## Principle

1. **Cuts only, by default.** The job is picking takes and cutting. No color grade, background work, subtitles or animations unless the user explicitly asks (see *Opt-in extras*). The user adds backgrounds themselves afterwards.
2. **Separate pieces: one file per hook plus one body file.** The recording opens with 3–5 hooks, then the body. Render `hook_1.mp4` … `hook_N.mp4` and one `body.mp4`; the user stitches a hook onto the body on their phone and posts from there. Don't join them yourself.
3. **Preserve source quality.** Output matches the source's resolution, frame rate and color (HDR stays HDR). One video encode per piece, never stacked.
4. **LLM reasons from the transcript, visuals on demand.** The packed transcript (`takes_packed.md`) is the primary view. Drill into `timeline_view` only at decision points.
5. **No strategy pause.** Pick the takes, render every piece, self-check, then deliver. The user reviews finished videos, not plans. Ask only if the material is genuinely ambiguous (e.g. you can't tell hooks from body).
6. **Verify your own output before showing it.** If you wouldn't ship it, don't present it.

## Hard Rules (production correctness — non-negotiable)

1. **Per-segment extract → lossless concat.** Each range is extracted once (video encoded once), then joined with the concat demuxer. Never a single-pass filtergraph, never re-encode the joined video.
2. **Final segments carry uncompressed (PCM) audio in `.mov`; audio is AAC-encoded once, after the join.** AAC per segment adds ~21ms of encoder priming/padding per file, which players and loudnorm play back-to-back, drifting audio ~40ms later at every join. `render.py` does this; don't hand-roll segments with AAC audio.
3. **Segment lengths are whole frames.** `render.py` snaps each segment to `-frames:v N` with a matching `-t`, otherwise video overruns audio by up to a frame per segment.
4. **30ms audio fades at every segment boundary.** Prevents pops. Baked in by `render.py` (placed on the output timeline when sped up).
5. **Never cut inside a word.** Snap every cut edge to a word boundary from the transcript.
6. **Pad every cut edge.** 50ms before the first word, 80ms after the last (working window 30–200ms).
7. **Don't trust the transcript at long pauses — check the waveform.** Scribe drifts badly next to long silences:
   - **Last word before a pause** can end 0.5–2s+ late. `render.py`'s `verify_segment_end` corrects segment ends against the real waveform automatically.
   - **First word after a pause** can *start* many seconds early (seen: "This" stamped at 318.18s, really spoken at 325.33s). Not auto-corrected. Whenever a range's first word has an implausible duration (> ~0.6s), find the real onset with `silencedetect` on the source window before it and start the range ~50ms before that onset.
   - `[clears throat]` / audio events can span a whole gap; never use them as cut anchors.
8. **Word-level verbatim ASR only.** Never SRT/phrase mode. Never normalized fillers.
9. **Cache transcripts per source.** Never re-transcribe unless the source file changed.
10. **Spoken hook markers are always cut.** If the user says "hook 1", "hook 2"… those words never appear in an output.
11. **All session outputs in `<videos_dir>/edit/`.** Never write inside the `video-use/` repo. Never modify or move the source file.

## Directory layout

```
~/Movies/reels/<slug>/          ← <videos_dir>: one folder per recording
├── <source file, untouched>
└── edit/
    ├── project.md               ← memory; appended every session
    ├── takes_packed.md          ← phrase-level transcript, primary reading view
    ├── edl.json                 ← hooks + body cut decisions
    ├── transcripts/<name>.json  ← cached raw Scribe JSON
    ├── clips_graded/            ← per-segment extracts (.mov, PCM audio)
    ├── verify/                  ← self-eval timeline PNGs
    └── deliver/                 ← ONLY what the user AirDrops (select all → AirDrop)
        ├── hook_1.mp4 … hook_N.mp4  ← each hook on its own
        ├── body.mp4                 ← the shared body
        └── hook_N_transcript.md     ← exact spoken words of hook N + body (for the caption)
```

## Setup

First-time install lives in `install.md`. On cold start just verify:

- `ELEVENLABS_API_KEY` resolves (environment or `.env` at the video-use repo root). If missing, ask the user to paste one and write it to `.env`.
- `ffmpeg` + `ffprobe`. `render.py` prefers the native Apple Silicon build at `/opt/homebrew/bin/` when present (the x86_64 build under Rosetta has no hardware encoder and is ~3x slower).
- **Recordings live in `~/Movies/reels/<slug>/`.** Move a handed-over file there first (`mv`, never copy), and only touch that folder and the path the user gave. If a file is missing, ask; never search other folders.
- Python 3.10+ with deps installed (`pip install -e .` in the repo). On this machine use `python3.12`; the default `python3` is 3.8.
- **Free disk space.** A 40s 4K60 body is ~300 MB, and a render needs temporary copies on top. Check `df -h` before rendering; want ≥ 4 × the expected body size. If short, stop and tell the user before starting.

Helpers live alongside this SKILL.md (the skill is symlinked at `~/.claude/skills/video-use/`).

## Helpers

- **`transcribe.py <video> [--edit-dir <dir>]`** — single-file Scribe call. Cached.
- **`pack_transcripts.py --edit-dir <dir>`** — `transcripts/*.json` → `takes_packed.md` (phrases break on silence ≥ 0.5s).
- **`timeline_view.py <video> <start> <end> [-o out.png]`** — filmstrip + waveform PNG for decision points and self-eval.
- **`render.py <edl.json>`** — with `hooks` in the EDL, writes one `edit/deliver/<hook name>.mp4` per hook (hook only) plus its transcript, and `edit/deliver/body.mp4`. Nothing else goes in `deliver/`. A final render then copies `deliver/` to `iCloud Drive/Reels/<slug>/` automatically (re-renders replace the files), so the user picks them up in the Files app on the phone uncompressed. All pieces get one shared audio gain, set from the body (to -14 LUFS), then the same peak limiter (-1 dBTP), so there's no level jump at the stitch. `--preview` (1080p H.264, fast) / `--draft` (720p) write `_preview`/`_draft` suffixed files into `edit/` (never `deliver/`) for quick checks. `--no-loudnorm` skips loudness normalization.
- `speed.py`, `grade.py` — standalone tools for the opt-in path. Speed normally goes in the EDL (`"speed"`), not through `speed.py` (which would add a second encode).

## The process

1. **Inventory.** `ffprobe` the source (resolution, fps, color transfer, bitrate, duration). Check free disk space. Transcribe (`transcribe.py`), then `pack_transcripts.py`. If `project.md` exists, read it and summarize the last session in one sentence.
2. **Find the hooks and the body.**
   - **Spoken markers present** ("hook 1", "hook 2"…): they set the hook boundaries. The body starts at the first line after the last hook's takes.
   - **No markers:** use judgement. Hooks are the opening lines, each usually retaken several times, that each stand alone as an opener. A hook may be more than one line. The body starts at the first line that continues the content rather than opening it.
   - Retakes with different wording ("SaaS or app" vs "app or SaaS") are takes of the *same* hook, not separate hooks, unless the user said otherwise.
3. **Pick the best take of every hook line and body line.** Default to the last complete take (speakers improve as they go), unless it has a stumble, filler, throat-clear, or cut-off ending that an earlier complete take doesn't. Never use a take that doesn't finish its sentence if a complete one exists. Check every range's first word for start drift (Hard Rule 7). Each hook should flow into the body's first line.
4. **Write `edl.json`** (format below) with `"speed": 1.1`, a `reason` per range.
5. **Render.** `python3.12 helpers/render.py <edit>/edl.json`.
6. **Self-eval (before showing the user):**
   - `ffprobe` every output: codec HEVC Main 10, `hvc1`, resolution and color tags = source, `r_frame_rate` = `avg_frame_rate` = source nominal rate, video and audio `start_time` 0.
   - Frame count = Σ `round((end - start) / speed × fps)` over each piece's ranges.
   - Decode check: `ffmpeg -v error -i <out> -f null -` prints nothing.
   - `timeline_view` (±1.5s) at every join inside `body.mp4` and inside any multi-range hook, plus the first and last 1.5s of each piece (clean in/out, no clipped first word). Look for visual jumps/flashes and waveform spikes at the cut.
   - `deliver/` contains exactly N hook mp4s, `body.mp4` and N transcripts — nothing else.
   - Read each `hook_N_transcript.md` — it should be exactly the intended words.
   - Fix → re-render → re-check. Cap at 3 passes; flag anything still wrong rather than looping.
7. **Deliver.** Check the render printed `icloud: copied deliver/ → iCloud Drive/Reels/<slug>/`; if it printed a warning instead, tell the user the files didn't reach iCloud and why. List each hook file with its line and length, then the body's length, the take choices that were non-obvious, and any drift corrections. Tell the user the files are in Files → iCloud Drive → Reels → `<slug>` on the phone (Share → Save Video puts them in Photos). AirDrop is unreliable on this setup, and iMessage/WhatsApp compress video. `open <edit>/deliver/` in Finder as a fallback. Never write anything else into `deliver/`.
8. **Iterate + persist.** Natural-language feedback → edit the EDL → re-render. Never re-transcribe. Append to `project.md`.

## Cut craft

- **Audio-first.** Cut candidates come from word boundaries and silence gaps. Silences ≥ 400ms are the cleanest cut points.
- **Pacing.** With 50ms/80ms padding and 1.1x speed, lines sit ~0.12s apart — tight, short-form pacing. Loosen the end padding (up to 200ms) if the user wants more air.
- **Never reason audio and video independently.** Every cut must work on both tracks; a static tripod shot makes hard cuts between takes acceptable.

## The packed transcript

`takes_packed.md` lists each phrase with its `[start-end]` range; phrases break on any silence ≥ 0.5s. A phrase whose range is far longer than its words (e.g. `[318.18-360.10]` for one sentence) signals timestamp drift on its first or last word — check word-level times in the JSON.

```
## IMG_0001  (duration: 8m 02.0s, 40 phrases)
  [029.88-034.06] S0 Most people get this step wrong, and it costs them hours.
  [073.08-077.06] S0 Here's the one change that fixed it for me.
```

## Output spec

Final renders match the source: native resolution, nominal frame rate (an iPhone average like 59.986 snaps to 60/1), source color tags (HDR/HLG kept as HDR, SDR stays SDR). Video: HEVC Main 10 on the hardware encoder (`hevc_videotoolbox`) at 1.25× the source bitrate (floor 12 Mbps), `hvc1` tag for iOS. Audio: AAC 192k 48 kHz, loudness-normalized to -14 LUFS / -1 dBTP. Don't downscale or change the frame rate unless the user asks.

## EDL format

```json
{
  "version": 1,
  "sources": {"IMG_0001": "/abs/path/IMG_0001.MOV"},
  "speed": 1.1,
  "hooks": [
    {"name": "hook_1", "ranges": [
      {"source": "IMG_0001", "start": 29.83, "end": 34.14, "beat": "HOOK 1",
       "quote": "...", "reason": "3rd of 3 takes; take 1 has a throat-clear."}]},
    {"name": "hook_2", "ranges": [ ... ]}
  ],
  "ranges": [
    {"source": "IMG_0001", "start": 212.33, "end": 217.30, "beat": "EXAMPLE",
     "quote": "...", "reason": "Last take; includes 'interface'."}
  ]
}
```

- `hooks`: one entry per hook file. `name` becomes the filename (`[A-Za-z0-9_-]+`, unique, not `body`). A hook can have several ranges (multi-line hook).
- `ranges`: the shared body, in output order → `body.mp4`.
- `speed`: applied to every range in the same encode (pitch-preserving). Default 1.1.
- Without `hooks`, `render.py` renders a single video from `ranges` (requires `-o`). `grade`, `overlays`, `subtitles` are ignored (with a warning) when `hooks` is present.

## Opt-in extras (only when the user asks)

These are off by default and only work in single-output mode (EDL without `hooks`). The code is kept, not maintained for the hook + body flow.

- **Color grade** — `grade` field (preset name, raw ffmpeg filter, or `auto`), applied per segment during extraction. `grade.py --list-presets` for starting points. Reason about the image via `timeline_view`, never go aggressive without checking skin tones.
- **Subtitles** — `render.py --build-subtitles` builds `master.srt` and burns it in LAST (after overlays), with `MarginV=90` to clear platform UI. Known gap: SRT offsets ignore `speed`, so use subtitles only at speed 1.0.
- **Animations / overlays** — `overlays` entries `{file, start_in_output, duration, [width, height, x, y]}`, PTS-shifted so frame 0 lands at the window start. Build each animation in its own `edit/animations/slot_<id>/` (PIL, HyperFrames, Remotion, or Manim via `skills/manim-video/`), parallel sub-agents, cubic easing, hold the last frame ≥ 1s. Note: overlays force a second video encode of the whole base.

## Memory — `project.md`

Append one section per session at `<edit>/project.md`:

```markdown
## Session N — YYYY-MM-DD

**Hooks:** what each hook is and which take was used (one file each)
**Body:** take choices + why (one line each for non-obvious picks)
**Corrections:** timestamp-drift fixes, anything re-cut
**Outstanding:** deferred items
```

## Anti-patterns

- **Grading, subtitling or adding backgrounds unasked.** Cuts only.
- **Downscaling, tonemapping HDR→SDR, or changing the frame rate** by default. Match the source.
- **Stacked encodes** — e.g. render then `speed.py`, or re-encoding the joined video. Speed goes in the EDL.
- **AAC audio in segments before a concat.** Audio drift at every join (Hard Rule 2).
- **Trusting a word's timestamp next to a long pause.** Check the waveform (Hard Rule 7).
- **Treating reworded retakes as separate hooks.**
- **Using a take that cuts off mid-sentence** when a complete one exists.
- **Re-transcribing cached sources.**
- **Rendering without checking disk space** on 4K sources.
- **Measuring A/V sync with `silencedetect` after lossy re-encodes** — codec noise in the gaps shifts its threshold crossings; use sample-level onsets.
