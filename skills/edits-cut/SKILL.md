---
name: edits-cut
description: Cut a talking-head recording inside Instagram Edits (Mac) via the metapilot-native MCP — one Edits project per hook over a shared body, each with its own caption style and highlight color. Transcribe and cut in Edits; the user finishes (hook text, greenscreen) and posts from Edits on the Mac. Alternative to video-use, which renders mp4s with ffmpeg.
---

# Edits Cut

## Principle

1. **The deliverable is Edits projects, not files.** One project per hook: `<hook> + <shared body>`, cut at normal speed (1.0x), captioned. The user adds hook text and greenscreen, then shares and posts from Edits on the Mac. Never export unless asked.
2. **Edits does the media work.** Transcription (`transcribeClip`), pause detection (`findPauses`), cutting, fades and captions all happen in the app. `cutplan.py` (next to this file) only does arithmetic: edge correction, padding, frame snapping, split points, and verification.
3. **Rebuild, don't patch.** Every hook project is a fresh duplicate of the uncut base project with its full cut list applied. If one comes out wrong, delete it and rebuild it.
4. **Verify the timeline before moving on.** After every cutting phase, read `clips` back and run `cutplan.py verify`.
5. **The other path is `video-use`.** If the user wants mp4 files, exact output specs, or the app misbehaves, use the `video-use` skill instead.

## Setup

1. **MCP up:** call `appState`. It should return `screen: home` (or `editor`). If the server is unreachable, ask the user to open Edits and reconnect the MCP. Don't continue without it.
2. **Inbox:** Edits is sandboxed and can't read `~/Downloads` (the import fails with "Sandbox bookmark unavailable"). `~/Movies` works. Move (`mv`, never copy) the recording into `~/Movies/reels/<slug>/`, where `<slug>` is a short kebab-case name for this reel. All session files (transcript, pauses, cut list, plan, `project.md`) live in the same folder.
3. **The inbox is permanent.** Edits references media in place; it doesn't copy it. Moving or deleting the recording later breaks every project made from it. Tell the user this once.
4. **Disk:** check `df -h ~`. The recording isn't duplicated, but Edits caches waveforms and thumbnails. If less than ~5 GB is free, tell the user before starting.

## Driving the MCP

- **The tool set follows the screen.** Home-screen tools (`projects`, `newProject`, `openProject`, `duplicateProject`, `renameProject`, `deleteProject`) and editor tools (`importMedia`, `clips`, `splitAt`, `transcribeClip`, …) are only available on their own screen, and the server reconnects on every switch. At the start of each phase, load every tool that phase needs in one ToolSearch call (`select:mcp__metapilot-native__a,mcp__metapilot-native__b,…`).
- **Gotchas:**
  - `addMedia` is for audio only (`audioKind`: music/soundEffect/voiceover). Use `importMedia(path)` for video.
  - `generateCaptions` takes `source: "video"`.
  - **No speed changes.** The user found 1.1x sounded off (2026-11-10), and `setSpeed` makes Edits render a black frame at some clip joins (3 of 12 joins in one hook at 1.1x; the same cut at 1.0x was clean). Never call `setSpeed`, `setSpeedCurve` or the speed-panel tools unless the user asks for a speed change. In that case, run `cutplan.py verify` before changing speed (afterwards it reports every clip's duration as a mismatch), then check every join with `captureFrame`.
  - Opening a project can change its "last modified" date even without edits. Don't open the user's other projects.
- **Failures:** if a tool call fails or times out, retry it once. If it fails a second time, stop and report the tool, its arguments and the error. Never loop.
- **Save files with Write:** when a step says to save a tool's output (transcript, pauses, `splitAt` and `clips` responses), write that JSON into the inbox folder with the Write tool so `cutplan.py` can read it.

## 1. Transcribe

Home screen → `newProject` → editor:

1. `importMedia(path: "~/Movies/reels/<slug>/<file>")` (absolute path).
2. `clips`: expect exactly one clip with `startTime 0` and `sourceInPoint 0`, so **timeline time = source time**. Every later time in this flow relies on that. If it isn't true, stop.
3. `transcribeClip(id: <clip id>, language: "en", mergeSplitWords: true)`. Long recordings return `status: running` with a `jobId`. Call `jobStatus(jobId)` again right away, without sleeping, until it finishes (a 12.8-minute recording took 2 calls). The finished result is large (~110 KB for 13 minutes) and lands in a tool-results file. Extract its `words` and `lines` into `transcript.json` with a short script rather than reading it into context.
4. `findPauses(id: <clip id>, path: "", start: 0, duration: <clip duration>, minDuration: 0.3, thresholdDb: -35)`. It returns `{pauses: [{start, end}], …}`, which `cutplan.py` reads as is. Save it to `pauses.json`. **Keep −35 dB.** At −45 dB, background blips break long silences into fragments, so a mistimed start only gets corrected to the first fragment. The −35 dB boundaries land 30–130 ms inside soft word edges. `cutplan.py` absorbs that: the 80 ms trail covers ends, and starts are only corrected when they're ≥150 ms inside a pause.

**Known transcript issues** (same family as Scribe's):
- **Last word before a pause runs long** (seen: "people." stamped 8.70–10.22).
- **A stray first word gets stamped seconds early** (seen: a lone "If" at 38.70, real take at 48.14).
- **Words get normalized** ("wanna" → "want to", "SaaS" → "SAS"). Fix these in the captions later.

`cutplan.py plan` corrects edges that land inside a pause. Still read every range's first and last word critically: a word lasting more than ~0.6s is suspect.

## 2. Pick takes

From the transcript `lines` and `words`:

- **Hooks vs body.** The recording opens with 3–5 hooks, each possibly retaken, then the body with several takes per line.
  - If the speaker says "hook 1", "hook 2"…, those markers set the boundaries, and the markers themselves are **always cut**.
  - Without markers, hooks are the opening lines that each stand alone as an opener. The body starts at the first line that continues the content.
  - Reworded retakes ("app or SaaS" / "SaaS or app") are takes of the same hook.
- **Best take of each line.** Default to the last complete take. Use an earlier one only if the last has a stumble, filler, throat-clear or cut-off ending that the earlier one doesn't. Never use a take that doesn't finish its sentence when a complete one exists.
- **Cut on word boundaries only.** `start` = first word's start and `end` = last word's end, raw from the transcript. `cutplan.py` adds the 50ms/80ms padding.
- Each hook should flow into the body's first line.

Write `cutlist.json`:

```json
{
  "source": "/Users/<user>/Movies/reels/<slug>/IMG_1234.MOV",
  "duration": 554.6, "fps": 60,
  "hooks": [
    {"name": "hook_1", "caption_style": "Word Highlight Box", "highlight_color": "#D300C5",
     "ranges": [{"start": 29.88, "end": 34.06, "quote": "Ads aren't the only way…", "reason": "3rd of 3 takes; take 1 has a throat-clear"}]}
  ],
  "body": [
    {"start": 212.33, "end": 217.30, "quote": "…", "reason": "last take"}
  ]
}
```

- `duration` = the clip's duration and `fps` = the project frame rate (`state.projectFrameRate`).
- `body` ranges are in source order. All hook ranges come before the first body range.
- Every hook gets its own `caption_style` and `highlight_color`. Default table:

| Hook | caption_style | highlight_color |
|---|---|---|
| hook_1 | Word Highlight Box | #D300C5 |
| hook_2 | Short Pop | #FFD400 |
| hook_3 | Word Highlight | #00C2FF |
| hook_4 | Fade In | #FF5A36 |
| hook_5 | Short Hard Cut | #3DDC84 |

Check each style name against `listCaptionStyles` (category `caption`). `setCaptionStyle(dryRun: true)` fails in a project that has no captions yet, so it can't validate names up front.

Then run:

```bash
python3.12 <skill dir>/cutplan.py plan <inbox>/cutlist.json --pauses <inbox>/pauses.json > <inbox>/plan.json
```

A non-zero exit means the cut list is invalid. Fix it and rerun. Report every entry in each hook's `corrections` (edge moved from the transcript time to the pause boundary) in the delivery summary.

## 3. Build one project per hook

**Base project.** The project from step 1 still holds the uncut recording. `saveProject` → `closeProject` → on the home screen, `renameProject(<id>, "<slug> — base")`. Never cut the base; it's the template for every hook project and for rebuilds.

**For each hook** (home screen tools, then editor tools):

1. `duplicateProject(<base id>)` → `renameProject(<new id>, "<slug> — hook_N")` → `openProject`. You can duplicate all hooks up front while on the home screen. That saves screen switches.
2. The duplicate keeps the base clip's id, so `splitAt` can use it directly (check with `clips` if unsure). `splitAt(id, times: plan.hooks.hook_N.times)` (default snap `projectFrame`). Save the response to `split_hook_N.json`, then:
   ```bash
   python3.12 <skill dir>/cutplan.py pieces <inbox>/plan.json hook_N <inbox>/split_hook_N.json
   ```
   This prints `{"delete": [...], "keep": [...]}`.
3. `selectById(first delete id, addToSelection: false)`, then `selectById(id, addToSelection: true)` for each remaining delete id → `delete`. The main track closes the gaps.
4. `clips` → save it to `clips_hook_N.json` → `cutplan.py verify <inbox>/plan.json hook_N <inbox>/clips_hook_N.json`. It must print `OK`.
5. For every keep id: `setFade(id, fadeIn: 0.03, fadeOut: 0.03)`.
6. `captureFrame(times: <every join's startTime>, maxWidth: 60)`: no frame may be black. Edits has rendered black frames at joins before (see Driving the MCP).
7. `generateCaptions(source: "video", language: "en", curseWords: false)` → `setCaptionStyle(style: <caption_style>)` (no id = all captions) → `setCaptionHighlightColor(<highlight_color>)`.
8. `captions` → compare every caption's text with the same words in `transcript.json` and the cut list `quote`s. Fix differences with `editCaption(id, text)`. The captions model runs separately from `transcribeClip` and **gives different text on each run**. Seen on a real run:
   - misheard words ("estimates **to make**" / "**to made**" for "they made"; "**Come and** build" for "Comment build");
   - a dropped first word at a clip join ("app is free…" for "<AppName> is free…");
   - split brand names ("Coin Snap", "Tik Tok", "chat GPT");
   - lowercase line starts after a join ("if", "comment").

   Some styles insert `\n` line breaks into the text (Word Highlight does). Keep them when editing. Keep a list of the fixes for the summary. A dropped first word can also mean the cut clipped it, so list those for the user to listen to.
9. `saveProject` → `closeProject`.

**When a verify fails** (exit 1) or a tool fails twice: don't patch the project. `deleteProject` that hook's project (only that one, which you just created) and redo the hook from step 1. After 2 failed builds of the same hook, stop and report the `verify` output.

## 4. Deliver

1. Say which projects were made, one line each: `<slug> — hook_N` with its hook line, its length (last clip's `endTime`) and its caption style and color.
2. List the non-obvious take picks, every edge correction from `plan.json` `corrections`, and every caption fix.
3. Tell the user that new Edits projects default to **HDR (HLG)** even for SDR footage (`projectDynamicRange` reports `hdr` either way), so they should check the dynamic range when exporting. Mention that `<slug> — base` is the uncut template. Keep it for rebuilds; delete it once the reel is posted.
4. `openProject` the hook_1 project so the user can review it in Edits. There's no file to `open`.
5. Append a session section to `<inbox>/project.md`:

```markdown
## Session N — YYYY-MM-DD
**Hooks:** each hook, its take and its caption style/color
**Body:** take choices + why (non-obvious only)
**Corrections:** edge corrections, caption fixes, rebuilt projects
**Outstanding:** anything deferred
```

**Feedback afterwards:** edit `cutlist.json` → rerun `cutplan.py plan` → delete the affected hook projects → rebuild them from the base. Never re-transcribe; `transcript.json` and `pauses.json` are cached in the inbox.

## Anti-patterns

- **Importing from `~/Downloads`** (or anywhere outside `~/Movies`). The sandbox blocks it.
- **Moving or deleting the inbox recording** after projects exist. That breaks them.
- **Patching a broken hook project** instead of rebuilding it from the base.
- **Cutting the base project.**
- **Trusting word timestamps next to pauses** without running `cutplan.py plan` with `findPauses` data.
- **Using `addMedia` for video**, or changing speed unasked.
- **Exporting unasked.** The deliverable is the projects.
- **Opening the user's unrelated projects.** It changes their modified date.
