# One Pace → Japanese subtitles — project handoff

Context for continuing this in Claude Code. Read this, then look at `opace_audio_align.py`
and `debug_probe.py`. **Now on the Windows PC (i3-12100F, RTX 3060 12GB, 32GB RAM),
running inside WSL2 — which only gets ~15GB of the 32GB, and the host sits >90% RAM
while a run is live, so treat ~6GB python RSS as the practical ceiling.**

## Goal
Watch **One Pace** (a fan recut of the One Piece anime that cuts filler) with
**accurate Japanese subtitles**, for language learning. One Pace ships English subs
only. We want official-quality Japanese text, correctly timed to the One Pace cut,
generated automatically (no reading ahead / no manual per-line work).

## Key facts learned
- One Pace publishes per-episode English `.ass` subs (github.com/one-pace/one-pace-public-subtitles),
  perfectly timed to their cut. **But** their English is re-localized/paraphrased from
  the official subs, and they do NOT translate from or align to Japanese.
- One Pace provides a table mapping each One Pace episode → source One Piece episode(s),
  e.g. Romance Dawn 02 = Ep 1; RD03 = Ep 2 + 19; RD04 = Ep 3; RD01 = "Episode of East
  Blue" recap (spans dozens of eps — skip it, no single source).
- The One Pace **audio is the original Toei audio**, just cut/reordered — NOT paraphrased.
- Official/again-human Japanese subs exist on Jimaku (jimaku.cc) and kitsunekko, including
  Netflix `.vtt` rips (e.g. `ワンピース.S01E01.WEBRip.Netflix.ja[cc].vtt`).

## Approaches tried, in order
1. **English-text bridge** (rejected as the primary): match One Pace EN lines to official
   EN lines → carry the paired official JA. Capped at ~88% on RD02 because One Pace's
   paraphrasing breaks the match on rewritten lines. Order-aware "rescue" lifted it from
   81%→88%. Still leaves heavy-rewrite lines in English. Good as a *fallback*, not primary.
2. **ASR bridge** (fallback option): transcribe One Pace audio with kotoba-whisper, use
   the transcription only as a *fingerprint* to look up the correct official JA line
   (display official text, throw away ASR). Robust to paraphrase. Runs well on the RTX 3060.
3. **Audio alignment** (CURRENT primary): acoustically fingerprint the source episode(s)
   with **audalign**, slide a window across the One Pace audio to find where each chunk
   came from, cluster hits into constant-offset segments, then map the official JA sub
   (timed to source) onto the One Pace timeline. Zero ASR, official text AND timing.
   This is `opace_audio_align.py`.

## State as of 2026-06-07 late night: PIVOTED TO ASR BRIDGE (`opace_asr_bridge.py`)

The audio-alignment output below validated on paper but was BROKEN when watched
(prime suspect: the Netflix VTT is not synced to `onepieceofficial01.mkv` — never
verified — plus ±3s probe-window granularity at every segment boundary). Pivoted to
approach #2, now implemented and producing plausible output on RD02:

### How `opace_asr_bridge.py` works (v4)
1. Transcribe One Pace audio with faster-whisper **large-v3** on the 3060
   (word timestamps; ~3min/episode; transcript cached in `work/asr_*.json` so
   matcher tweaks are instant). NOT kotoba-whisper: its distil architecture
   crashes CTranslate2 (`std::bad_alloc`) with word_timestamps=True.
2. Normalize BOTH the ASR stream and the official JA lines to hiragana
   (pykakasi) — kanji/kana orthography differences otherwise kill matching.
3. difflib SequenceMatcher (autojunk=False!) between the two char streams; each
   official line ≥50% covered by ≥2-char blocks is placed at the time its chars
   were spoken (per-char times interpolated inside word timestamps).
4. Rescue pass: unplaced lines between two placed neighbors are fuzzy-searched
   (rapidfuzz partial_ratio_alignment ≥65) in just the ASR span between them.
5. Official text only is emitted; ASR text is never shown. No VAD (it eats
   dialogue over BGM; music hallucinations don't match anything → harmless).

### v13 refinements (second eyeball pass: all flagged misplacements were
### short lines placed without acoustic evidence — fixed)
- **Readings via MeCab/fugashi, tagged with context** (replaced pykakasi, which
  misread 叩き→こうき; and whisper word-fragments must be CONCATENATED before
  tagging or MeCab misreads 結構→けつかまえ). Lifted char match 60→72%.
- **Grunts** (うう…/キャーッ/おい — GRUNT regex + length gates): never placed
  without coverage evidence. TRADEOFF (user-accepted): a grunt ASR can't hear is
  absent rather than guessed wrong.
- **Interp hardened**: gap ratio 0.8–1.25 always + gap ASR text must corroborate
  the line (partial_ratio ≥50) + silent gap = no interp (nobody spoke).
- **Mid-cue cuts**: when one \N-part of a cue has hits and the other doesn't,
  duration disambiguates: heard-span ≥70% of cue duration → concurrent speakers,
  keep whole cue text; else → the other part was cut, emit heard part only.
- **Cover hits clustered in time** (largest contiguous cluster, 3s gap max) so a
  stray far-away 2-char hit can't stretch/misplace a line.
- Debug sidecar `<out>.ja.ass.debug.tsv`: per official line, placement method
  (cover/rescue/part/interp/-) + times — for fast trace sessions.

### v14 (third eyeball pass — all 5 reports fixed, several to the exact second)
- **Netflix CC rollup duplicates**: the VTT repeats identical cue text at
  near-identical times; dedupe at load (same norm within 6s) or every copy stacks
  on the same audio. Affects EVERY episode's VTT.
- **Consistency snap**: official inter-line spacing survives wherever One Pace
  didn't cut. When both placed neighbors predict the same start (±2.5s agreement
  -> locally linear, no cut) and a line deviates >1.2s, snap it to the weighted
  prediction. Fixes wrong-instance matches (何だ？ matching an earlier 何だ) and
  ASR word-boundary blur — moved 9 lines, all verified against user's trace.

### v15–v18 (sub-second polish — each fix generalized from one user report)
- Snap tolerance 0.4s for ≤3-char lines (whisper glues a reply's beat to the
  previous line) and for regions where both neighbors agree ≤0.3s (provably
  linear -> deviation = smear, not cut). v18 moves ~31 lines; if a future trace
  finds a NEWLY-off line, tune this rule first.
- Span refinement: trim edge chars with artifact durations (>3x lower-median or
  zero-width) — whisper smears words across SFX (ルフィ 3s over rubber-band
  sound, ピストル's スト 3.6s). Works from 4-char spans, lower-median base.
- Onset reconstruction: after silence (cut boundaries) whisper clips word
  onsets; when a span is <60% of the cue's official duration and there's room,
  rebuild the start backward from the reliable end (end − cue duration).
- All eyeball-traced lines (≈20 reports over 5 passes) verified to land within
  ~0.5s of the user's called times; see debug TSV for placement provenance.

### RD02 result (v6 — eyeball-tested, user-approved start timing)
320/400 official lines placed (80%); the rest is verified cut content, the OP theme
song (00:29–02:30 — sung lyrics aren't in the dialogue VTT), and untranscribable
screams. First eyeball pass (v4): start timing good on nearly every line. v5/v6
fixed what that pass found: overlapping events (now clamped to next line's start,
display-duration prior from the VTT), and ASR-deaf short lines (見せておやり,
食い物ねえか cluster) now placed by a VTT-proportional interpolation pass — runs of
unheard lines between placed neighbors are laid out proportionally when the OP gap ≈
VTT gap (ratio 0.5–2.0 for runs ≤2, 0.8–1.25 for runs ≤6; near-1 ratio proves no cut
inside the gap). Output: `out_asr/….ja.ass`. GPU does the work; system RAM is no
longer a constraint (the old audio-align RAM notes below are historical).

**The fingerprint/audio-align method is DITCHED** (user verdict after eyeballing
both with correct files): `opace_audio_align.py` is kept only as a forensic tool
(its SEGMENT MAP cross-checks cut boundaries); `out_align/` can be deleted.

### ASR bridge env
`pip install faster-whisper rapidfuzz pykakasi pysubs2 nvidia-cublas-cu12
nvidia-cudnn-cu12` (CUDA libs are ctypes-preloaded by the script — ld.so ignores
post-start LD_LIBRARY_PATH; nvidia.* are namespace packages, use `__path__` not
`__file__`). large-v3 CT2 model cached under `~/.cache/huggingface`.

### ASR bridge next steps
1. Eyeball RD02 (`mpv … --sub-file=out_asr/….ja.ass`).
2. If good: try `--model deepdml/faster-whisper-large-v3-turbo-ct2` for batch
   speed (turbo has real alignment heads, so word timestamps should work).
3. Multi-source episodes (RD03 = Ep2+19): pass both VTTs in watch order —
   concatenated official stream stays monotonic only if One Pace plays the
   episodes in blocks; if interleaved, split the difflib alignment per source.
4. Tunables if recall is short: MIN_COVER (0.50), MIN_BLOCK (2), RESCUE_SCORE (65).

### Repo layout (reorganized 2026-06-07 after RD02 sign-off)
```
opace_asr_bridge.py   # THE pipeline (only script left)
rd02/                 # video + VTT + generated .ja.ass (+ debug tsv)
rd03/                 # same per episode; .ja.ass lands next to the video
work/                 # caches: per-episode wav + ASR transcript json
```
The `.ja.ass` shares the video's stem -> mpv/VLC auto-load it. Deleted as unused
(in git history if ever needed): `opace_audio_align.py`, `debug_probe.py`, the
source episode mkv (the ASR bridge needs NO source videos — only One Pace files
+ official VTTs), audalign pickles/wavs. ~840MB freed.

Script hardening for RD03+: per-episode wav names (was a shared `onepace.wav` —
silent cross-episode cache poisoning), and explicit `jpn` audio-track selection
for multi-audio releases (RD03's 1080p mkv carries jpn/eng/spa — the dual-audio
trap from the "key facts" is now handled in code).

### RD03 (2026-06-07 night) — DONE, pending eyeball; two structural lessons
- 405/778 lines placed from E02+E19 VTTs; both remaining dialogue-flagged gaps
  are the OP/ED songs (read the gap report's ASR sample to tell songs from
  missing content).
- **Don't trust the One Pace episode table**: it said RD03 = Ep2+19; the audio
  showed a 3-min "Ep3-looking" block — which turned out to live in NETFLIX's
  S01E02 (their episode boundaries ≠ TV's). E03's VTT was never needed. The
  gap report's dialogue-density detector catches missing-source/mis-mapped
  content automatically.
- **One Pace REORDERS scenes within an episode** (RD03 plays part of E02's
  ending before its middle). A single monotonic alignment drops displaced
  chunks; the bridge now runs a SECOND difflib pass over (unmatched ASR) x
  (unmatched official lines), which recovers internally-ordered displaced
  chunks wherever they were moved.
- Multi-source works: pass VTTs in playback-block order; blocks confirmed
  non-interleaved (E02 0:58-19:28 incl. reordered chunk, E19 19:28-27:18).
- RD03 trace round (4 reports, all fixed, RD02 regression-clean):
  - one-sided snap: when neighbor predictions disagree (cut between) but ONE
    neighbor is vtt-close (≤3s), its prediction still beats whisper drift.
  - emit dedupe: the same utterance gets claimed by multiple official lines
    (catchphrases recur across episodes AND in next-episode previews) — events
    overlapping >60% with text similarity ≥65 keep only the best-evidenced.
  - island drop (post-dedupe): a placed event with no neighbor within ±3 lines
    or 30s is a stray (short line matching ED lyrics) — dropped.
  - precision floor is ~±0.7s where whisper glues words across pauses and both
    neighbor predictions disagree sub-second; accepted as-is.

### Vocal separation A/B (2026-06-07, late): NOT default
`--separate` (BS-RoFormer via audio-separator) was A/B'd on RD03 against the
trace-verified baseline: it upgrades several extrapolated lines to real
evidence and halves hallucinations, but REGRESSED 3 user-verified items and
costs ~9min/episode. Baseline stays primary. Use `--separate` as a second
opinion on disputed placements (it independently confirmed excluding line 405)
or on episodes with heavy hallucination zones. Needs `python3.12-dev` (apt)
for the diffq build. RD02-04 outputs as of this state = user ground truth.

### Status: RD02, RD03, RD04 DONE (user-verified). Next: Orange Town arc.
Per-episode trace rounds shrank 7 -> 4 -> 1; the pipeline is mature. Routine:
drop One Pace file + VTT(s) in inputs/rdXX/, run, read the gap report (songs
are fine; dialogue-dense gaps mean missing/mis-mapped source), spot-check the
debug TSV, excludes.txt for human-only calls.

## Historical: audio-alignment state (superseded by the ASR bridge above)

### The second bug: the RD02 file had ENGLISH dub audio ⚠️
The original `[En CC][7CEC60A5].mp4` download had the English DUB track. Every run
against it could only match music/SFX (the shared M&E track), never dialogue — that's
why even the "good" ACC=4 run had big gaps and why lower ACC collapsed entirely.
Replaced with `[En Sub][164BA736].mp4` (Japanese audio). Results transformed:

| | EN audio ACC=4 | EN audio ACC=2 | JA audio ACC=2 |
|---|---|---|---|
| confident windows | 352/420 (M&E only) | 30/420 | **290/420** |
| segments | 38 (2 garbage) | 7 | **22, zero garbage** |
| JA lines placed | 123 | 23 | **355** (of 435 VTT cues; rest = cut content) |
| peak RAM | 6.2 GB | 1.2 GB | ~1.2 GB |
| runtime (warm cache) | ~8 min | ~3 min | **~2.5 min** |

JA-audio map: source times strictly increase 00:15→24:17, long continuous segments
(up to 2m18s), only window-granularity overlaps at boundaries. Output:
`out_ja_acc2/[One Pace][2] Romance Dawn 02 [480p][En Sub][164BA736].ja.ass`.

**Lesson for every future episode: verify the One Pace download has Japanese audio
FIRST.** ffprobe language tags may be `und` (useless); the practical tell is the
run's hit rate — dialogue-dense JA audio gives ~70%+ confident windows at ACC=2,
an English dub gives <10%. `ACC = 2` is now the script default; the old garbage-
segment and MIN_CONF worries evaporated with the correct audio.

## Earlier on 2026-06-07 (first bug: audalign recognizer pollution — fixed)

### Root cause of the Mac run's "420/420 confident, 0 segments" — SOLVED
It was **not** the offset sign convention. `debug_probe.py` showed offsets increase
with t (row 1 of the old convention table): `off0 = P`, so `delta = off0 - t` was
already correct. The real bug, confirmed in audalign's source
(`recognizers/fingerprint/recognize.py`, the `file_name not in recognizer.file_names`
branch):

- **`ad.recognize()` permanently ADDS the query clip to the recognizer's fingerprint
  DB**, and if a query's *basename* is already in the DB it **silently reuses the
  STORED fingerprints instead of the fresh file**.
- The probe loop reused one filename (`clip.wav`), so every window after the first
  was matched as *window 0's audio*: constant `off0`, `delta = off0 - t` falling by
  exactly HOP=3.0 per step → no two consecutive deltas within DELTA_TOL → 0 segments.
- The same per-clip accumulation was the **Mac memory balloon / kernel panics**.
  One bug, both symptoms.

**Fix in `opace_audio_align.py`:** after each `recognize()`, evict the clip's entries
from `rec.fingerprinted_files` / `rec.file_names`. With that, RSS stayed flat through
all 420 probes (peak 6.2GB at ACC=4 = the fingerprint DB itself, not a leak).

### First good RD02 run (ACC=4, this PC)
- 352/420 windows confident (84% — the suspicious 420/420 is gone)
- **segments: 38 | JA lines placed: 123 | sign=+1** (meaningful detection now)
- SEGMENT MAP validates: source times climb 00:30→24:20 with sane jumps at cuts.
  Two blemishes: one 9s outlier (OP 13:57 ← src 20:52, out of order + overlapping
  its neighbor; false match that slipped past MIN_CONF=10) and one possible scene
  reorder (OP 16:45 ← src 20:18). 36/38 segments clean.
- Output: `out/[One Pace][2] Romance Dawn 02 […].ja.ass`
- Timing on the i3-12100F: fingerprint ~2.5min (first run only, now cached),
  probe loop ~8min, flat ~6.1GB RSS.

### Performance/RAM work (2026-06-07, after the fix)
RAM and wall time are the user's main concern (PC must stay usable; multi-source
episodes must not multiply RAM). Changes in `opace_audio_align.py`:
- **Sequential per-source probing**: sources are fingerprinted+probed one at a time,
  keeping per-window best-confidence match across passes — peak RAM stays at ONE
  episode's DB regardless of `--source` count (RD03's Ep2+Ep19 won't double it).
- **Fingerprint disk cache**: `work/<stem>.acc<N>.pickle`, auto save/load — re-runs
  and tuning skip extraction AND fingerprinting.
- **`--acc` flag** (now default 2): ACC=2 cuts the fingerprint DB ~5× and speeds
  both phases; with correct Japanese audio it outperforms every EN-audio run
  (settled by the A/B table in the state section above).
- Persistent `./work` dir (wavs + fingerprint pickles survive crashes/re-runs).
- Run under `nice -n 10` to keep the PC responsive while watching/working.

### Confirmed facts (don't re-derive)
- audalign return shape: `recognize()` → `{"match_time": float, "match_info":
  {srcfile: {"offset_seconds": [...], "confidence": [...], ...}}}` or `None`;
  lists are sorted by descending confidence, offsets aligned to confidences.
- Offset convention: `offset_seconds[0]` = position of the clip in the SOURCE
  (`sample_difference = a_offset - t_offset` in audalign). `delta = off0 - t`.
- `recognize()` mutates the recognizer (adds query clip to DB) — always evict after.
- `onepieceofficial01.mkv` (renamed from `one piece official 01.mkv` on the Mac)
  has a single `jpn` AAC track — the dual-audio trap does not apply.
- Equal `--source`/`--jasub` counts pair by command-line order, so stem mismatches
  between source files and JA sub names are harmless.
- WSL2 here gets ~15GB of the 32GB host RAM; host is >90% used during runs, so
  raising the WSL cap is NOT an option — reduce footprint instead (ACC, sequential
  probing).

## Open unknowns / things to verify
- **Eyeball test**: watch RD02 with `out_ja_acc2/….ja.ass` and spot-check timing
  (this is next step 1 — see below).
- **Small map gaps** (e.g. OP 01:51–02:15, 03:03–03:18): likely the title card /
  transitions (non-dialogue → fine). Confirm while watching.
- **Per-episode audio language**: every new One Pace download must be checked
  (see lesson above) — wrong audio silently degrades to M&E-only matching.
- **Wall time**: ~2.5min/episode warm, ~5min cold at ACC=2 — probably fine. If
  batching ever hurts, parallelize the probe loop with `multiprocessing` fork
  workers (Linux COW shares the fingerprint DB read-only across workers —
  near-zero extra RAM, ~4× on the i3).

## Run
```bash
# from the repo root, inside WSL (note: .venv, python3, renamed source file)
nice -n 10 .venv/bin/python opace_audio_align.py \
  --onepace "[One Pace][2] Romance Dawn 02 [480p][En Sub][164BA736].mp4" \
  --source "onepieceofficial01.mkv" \
  --jasub "ワンピース.S01E01.WEBRip.Netflix.ja[cc].vtt" --outdir out
# multi-source: pass --source A B  --jasub A.ja B.ja  (in matching order)
# optional dual sub: --en-ass "romancedawn_02_en.ass"
# optional: --acc N (default 2; 3-4 = more RAM/time, only if hit rate is low
#                    despite confirmed-Japanese audio)
```
The printed **SEGMENT MAP** is the validation gate: clean steadily-increasing source
times with a few sane jumps = success. Hit rate is the audio-language tell: ~70%
confident = JA audio; <10% = wrong (dub) audio. Tunables at top of file: `WIN/HOP`,
`MIN_CONF`, `DELTA_TOL`, `MIN_WINDOWS`; accuracy via `--acc`.

To watch: `mpv "[One Pace][2] … [En Sub][164BA736].mp4" --sub-file="out_ja_acc2/… .ja.ass"`
(or VLC: Subtitle → Add Subtitle File; from Windows the repo is at
`\\wsl$\Ubuntu\home\mfi\projects\opace\`).

## Next steps (in order)
1. Eyeball RD02 with `out_ja_acc2/….ja.ass`; note any drift/garbage sections.
2. Scale to RD03 (multi-source: Ep 2 + 19) and RD04 (Ep 3) — CHECK AUDIO LANGUAGE
   of each One Pace download first — then batch arcs.
3. If audio alignment underperforms on some episodes, wire in the ASR bridge (#2) as
   per-line fallback (kotoba-whisper on the RTX 3060).

## Env
WSL2 Ubuntu on the PC. ffmpeg on PATH (`/usr/bin/ffmpeg`). Python via project venv:
`.venv/` (python3.12, `pip install audalign pysubs2`). (For ASR fallback:
kotoba-whisper / faster-whisper + CUDA.) Repo contents needed: the two videos, the
`.vtt`, both `.py` files, this file. `out*/` is regenerable; `work/` holds reusable
wavs + fingerprint caches (regenerable but saves ~5min/episode).
