# One Pace → Japanese subtitles — project handoff

Context for continuing this in Claude Code. Read this, then look at `opace_audio_align.py`
and `debug_probe.py`. **Now continuing on the Windows PC (i3-12100F, RTX 3060 12GB, 32GB
RAM)** — see "State as of 2026-06-07" below for why and for exactly where we are.

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

## State as of 2026-06-07 (RD02 run #1 done on the Mac — moved to PC after crashes)

### What happened
First full RD02 run completed on the M1 MacBook (16GB). Results:

- Pipeline mechanics all work: ffmpeg extract → fingerprint (~1 min) → probe 420
  windows (~2.5 min, much faster than feared) → cluster → map → save.
- **420/420 windows were "confident" matches (conf ≥ 10) — but 0 segments formed,
  0 JA lines placed.** The output `out/[One Pace][2] Romance Dawn 02 ....ja.ass`
  from this run is EMPTY/garbage — ignore it.
- 420/420 confident is itself suspicious (even transition/music windows matched);
  MIN_CONF=10 may be too low, but that's secondary.

### Diagnosis (unconfirmed — confirm first on the PC)
0 segments despite 420 hits means **no two consecutive windows ever had deltas within
DELTA_TOL=0.4s** — i.e. `delta = off0 - t` is NOT constant within a contiguous chunk.
That points at audalign's offset convention, not at the fan edit. Hypotheses for what
`offset_seconds[0]` actually is, for a clip cut from One Pace time `t` whose true
position in the source episode is `P`:

| if offsets at t=60,63,66,69 look like… | convention | fix for step-4 delta |
|---|---|---|
| increasing with t (≈ t + C) | `off0 = P` | `delta = off0 - t` is already right → bug is elsewhere (check `_first`: maybe `offset_seconds[0]` is not the top-confidence entry — inspect the confidence list ordering in the debug dump) |
| decreasing with t (≈ -(t + C)) | `off0 = -P` | `delta = -off0 - t` |
| roughly constant across t | `off0` is already the segment shift | `delta = off0` (drop the `- t`) |

`debug_probe.py` (in repo, paths are relative, Windows-safe) settles this: it extracts
audio into a persistent `./work/` dir, fingerprints the source, probes windows at
t = 60/63/66/69 (consecutive) + 300/600/900/1100 (scattered), and dumps the raw
`offset_seconds[:5]` + `confidence[:5]` + available keys per match. Read the table
above against its output, patch the `delta` line in `opace_audio_align.py` step 3
(and the corresponding `op_s = s - sign*delta` mapping in steps 4–6 if the convention
change affects it), re-run, validate.

### Why we left the Mac ⚠️
The Mac kernel-panicked once (watchdog timeout, "18 swapfiles and LOW swap space")
and later froze with the "out of application memory" dialog showing **28GB attributed
to iTerm2** (= our terminal's child processes, i.e. the audalign work). 16GB RAM +
~19GB free disk couldn't take it. **audalign's probe loop appears to balloon memory**
— plausibly `ad.recognize()` accumulating per-clip fingerprints in the recognizer,
or per-recognize alignment buffers not being freed across 420 calls.

**On the PC (32GB): watch memory during the probe loop.** If python's working set
climbs unbounded across windows, mitigate inside the loop, e.g.:
- after each `ad.recognize(clip, ...)`, remove the clip's fingerprints from `rec` if
  audalign stored them (inspect `rec.fingerprinted_files` / `rec.file_names`), or
- recreate the recognizer every N windows from saved fingerprints
  (`save_fingerprinted_files` / `load_fingerprinted_files`) — this is also the
  building block for the planned parallelization, so it's not wasted work.

### Confirmed facts (don't re-derive)
- audalign return shape: `recognize()` → `{"match_time": float, "match_info":
  {srcfile: {"offset_seconds": [...], "confidence": [...], ...}}}` or `None`.
- `one piece official 01.mkv` has a single `jpn` AAC track — the dual-audio trap
  does not apply to this file.
- Equal `--source`/`--jasub` counts pair by command-line order, so the stem mismatch
  between `one piece official 01` and `ワンピース.S01E01...` is harmless.
- Probe speed was ~3 windows/sec on an M1 at ACC=4 — the loop is NOT the bottleneck
  it was feared to be; parallelization is nice-to-have, not required.

## Open unknowns / things to verify
- **Offset convention** (above) — the current blocker, `debug_probe.py` answers it.
- **Memory growth** in the probe loop (above) — observe on the PC, mitigate if real.
- **Offset sign for JA mapping**: separate from the clustering-delta convention; the
  script AUTO-DETECTS it (tries both, keeps whichever lands more JA lines in-range).
  Check the printed `sign=`. (Run #1 printed `sign=+1` but with 0 segments it was a
  meaningless 0-vs-0 tie.)
- **Transition robustness**: One Pace adds crossfades/music swaps at cuts; those windows
  won't fingerprint-match (usually non-dialogue → fine). If the map is fragmented even
  AFTER the delta fix, the edit is too aggressive → fall back to approach #2 (ASR).
- **MIN_CONF=10 looks too permissive** (420/420 hit). After the delta fix, if garbage
  windows pollute segments, raise it and re-check.

## Run
```bash
python opace_audio_align.py \
  --onepace "[One Pace][2] Romance Dawn 02 [480p][En CC][7CEC60A5].mp4" \
  --source "one piece official 01.mkv" \
  --jasub "ワンピース.S01E01.WEBRip.Netflix.ja[cc].vtt" --outdir out
# multi-source: pass --source A B  --jasub A.ja B.ja  (in matching order)
# optional dual sub: --en-ass "romancedawn_02_en.ass"
# (PowerShell: same command works with backtick ` instead of \ for line continuation)
```
The printed **SEGMENT MAP** is the validation gate: clean steadily-increasing source
times with a few sane jumps = success. Tunables at top of file: `WIN/HOP`, `MIN_CONF`,
`DELTA_TOL`, `MIN_WINDOWS`, `ACC`.

## Next steps (in order)
1. `pip install audalign pysubs2`, ffmpeg on PATH, then `python debug_probe.py`
   → read offsets against the convention table above.
2. Patch the `delta` computation in `opace_audio_align.py`; also make its work dir
   persistent (`./work`, skip extraction if wavs exist) like `debug_probe.py` does —
   the Mac crashes cost us the temp wavs twice.
3. Re-run RD02 (command above), validate the SEGMENT MAP, check memory while it runs.
4. Quality check the output `.ass` (coverage %, spot timing against video).
5. Scale to RD03 (multi-source: Ep 2 + 19) and RD04 (Ep 3), then batch arcs.
6. If audio alignment underperforms on some episodes, wire in the ASR bridge (#2) as
   per-line fallback (kotoba-whisper on the RTX 3060).

## Env
ffmpeg on PATH. `pip install audalign pysubs2`. (For ASR fallback: kotoba-whisper /
faster-whisper + CUDA.) Repo contents needed: the two videos, the `.vtt`, both `.py`
files, this file. `out/` and `work/` are regenerable.
