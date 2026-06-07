# Development log & design notes

Why `opace_asr_bridge.py` is built the way it is. User docs live in
[README.md](README.md); per-change history is in git. This file keeps the design
rationale and the lessons that would be expensive to relearn.

## Goal

Watch One Pace (fan recut of One Piece that removes filler) with accurate Japanese
subtitles, for language learning. One Pace ships English subs only; official JA subs
exist (Netflix rips via Jimaku/kitsunekko) but are timed to the original episodes.
Requirement: official-quality text, correctly timed to the recut, fully automatic.

## Approaches tried

1. **English-text bridge** (rejected): match One Pace EN lines to official EN lines,
   carry the paired JA. Capped ~88% — One Pace paraphrases too much.
2. **Acoustic fingerprinting** (audalign; built, then ditched): fingerprint source
   episodes, slide a window over the One Pace audio, map official lines through the
   resulting offset segments. Validated on paper, failed the eyeball test — it never
   verifies that the *subtitle file* is synced to the *source video file*, costs GB
   of RAM, needs the source episodes on disk, and has ±window-size timing granularity.
   (Also: audalign's `recognize()` permanently adds the query clip to the recognizer
   DB and reuses stale fingerprints on filename collision — the memory balloon and
   the 0-segments bug were one bug.)
3. **ASR bridge** (CURRENT): transcribe the One Pace audio itself (faster-whisper
   large-v3, word timestamps), align official lines to the transcript at the
   character level, emit official text at spoken times. No source videos needed, no
   sync assumptions, per-line timing. GPU does the work; RAM is no constraint.

## Pipeline (and why each stage exists)

1. **Extract** 16 kHz mono audio; `jpn`-tagged track explicitly selected
   (multi-audio releases are common; an English-dub file silently produces garbage —
   the EN-audio RD02 file cost a whole evening of debugging the wrong layer).
2. **Transcribe** with word timestamps; cached per episode (`work/asr_*.json`) so
   matcher iteration is free. Kotoba-whisper is NOT usable: its distil architecture
   crashes CTranslate2's word-timestamp aligner (`std::bad_alloc`).
   Stock hallucinations (`ご視聴ありがとうございました` over action scenes) are
   excised so untranscribable regions read as silence, not as contradicting speech.
3. **Normalize both sides to hiragana readings** via MeCab/fugashi, tagged with
   full context. Char-exact matching otherwise dies on kanji/kana orthography
   (ASR `分かった` vs CC `わかった`). pykakasi is context-naive (`叩き`→`こうき`);
   MeCab on whisper's word fragments mis-segments (`結構`→`けつかまえ`) — tag the
   concatenated stream, then map readings back to word times.
   Netflix VTT rollup duplicates (same cue repeated at near-identical times) are
   deduped at load or every copy stacks on the same audio.
4. **Align** the two char streams (difflib, `autojunk=False`), then a **second pass
   over the leftovers**: One Pace *reorders scenes* (RD03 plays part of Ep 2's
   ending before its middle), and a displaced chunk is invisible to one global
   monotonic alignment but perfectly ordered internally.
5. **Place** each line whose chars are ≥50% covered, with guards learned from
   eyeball traces:
   - hits trimmed to their largest time-contiguous cluster (stray 2-char matches);
   - span duration sanity vs the cue's official duration (straddling junk);
   - span text must resemble the line (fragment noise: `１発で当たった` "matching"
     `俺は強いって` on `った/って`);
   - whisper smears word boundaries across SFX (`ルフィ` stretched 3 s over a
     rubber-band sound) — artifact-duration edge chars are trimmed;
   - mid-cue edits: per-`\N`-part coverage decides between "show the heard half"
     (edit enters mid-cue) and "keep whole cue" (concurrent two-speaker lines,
     disambiguated by heard-span vs cue duration).
6. **Recover** unplaced lines, strictly gated:
   - *rescue*: fuzzy search confined between placed neighbors (≥5 chars, ≥75);
   - *interpolation*: proportional VTT layout into a gap, only when the gap ratio
     is ~1 AND the gap's ASR text corroborates AND the gap isn't silent;
   - *extrapolation*: VTT-contiguous runs laid out backward from an anchor —
     BACKWARD only (forward walks step over scene cuts), ≤35 s horizon, into
     event-free low-ASR-density territory, and only with a placed companion within
     ±3 lines (1-of-7 placed in a scene means the scene is cut, not unheard).
7. **Consistency snap**: official inter-line spacing survives wherever One Pace
   didn't cut, so when both neighbors predict the same start and a line deviates,
   the prediction wins (whisper glues replies to the previous line and clips onsets
   after silence). Tiny lines far from BOTH predictions are wrong-instance junk
   (`あったぞ` matched the OP lyric `ありったけ`).
8. **Dedupe & islands**: catchphrases recur across episodes and in next-episode
   previews — overlapping near-identical events keep the one with *scene
   continuity* (placed line-order neighbors nearby), not match strength. Isolated
   placements with no corroborating context are strays (ED lyrics matching
   `ほんとか！`). Both run *before* extrapolation too, so false anchors die
   childless.
9. **Emit**: ends extended toward official display durations, onsets of compressed
   single-word spans rebuilt backward from their reliable ends, overlaps clamped.
   `excludes.txt` is the human override of last resort: a single cut line whose
   neighbors are all present is undecidable from evidence (proved by `--separate`
   second opinion agreeing with the human).

## Failure modes worth remembering

- **Wrong audio language** is the silent killer; the tell is a tiny hit rate, and
  the fix is the file, not the code.
- **The One Pace episode table lies**: RD03 was listed as Ep 2+19 but audibly
  contained "Ep 3" scenes — which turned out to live in *Netflix's* S01E02
  (streaming episode boundaries ≠ TV). Trust the dialogue-density gap report, not
  the table; the report's ASR sample identifies missing content.
- **Whisper failure modes** seen in practice: timestamp smear across SFX, onset
  clipping after silence, glued replies, wholesale hallucination over
  music+gunfire, and total deafness to screams. Each has a targeted mitigation
  (refine/snap/onset-rebuild/excise/extrapolate) — none of them global hacks.
- **Vocal separation (BS-RoFormer, `--separate`) is not a free win**: A/B against
  trace-verified ground truth upgraded some evidence but regressed 3 verified
  placements and costs ~9 min/episode. It stays as a second-opinion tool.
- Precision floor is ~±0.7 s where whisper glues words and neighbor predictions
  disagree sub-second. Accepted.

## Status

| Episode | Sources (subs) | Result |
|---|---|---|
| RD02 | S01E01 | 257/393 placed — user-verified ground truth |
| RD03 | S01E02 + S01E19 | 421/778 — user-verified (1 manual exclude) |
| RD04 | S01E03 | 256/391 — user-verified |

Trace rounds per episode: 7 → 4 → 1. Next: Orange Town arc (grab Netflix VTTs
through ~S01E08 first; expect boundary drift).

## Environment

WSL2 Ubuntu, RTX 3060 12 GB (CUDA via Windows driver), Python 3.12 venv.
`audio-separator`'s `diffq` dep needs `apt install python3.12-dev` to build.
The old audalign-era RAM constraints are irrelevant to the ASR pipeline.
