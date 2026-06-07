# opace-ja-subs

Watch **[One Pace](https://onepace.net/)** with **official Japanese subtitles**, perfectly timed to the fan recut.

One Pace ships English subs only. Official Japanese subtitles exist (Netflix rips on
[Jimaku](https://jimaku.cc/) / kitsunekko) — but they're timed to the *original* episodes,
not the recut. This tool listens to the One Pace episode's own audio, finds where every
official line is actually spoken, and writes a `.ja.ass` that players auto-load.
The official text is never altered: speech recognition is only used as a *timing key*,
so ASR mistakes can't reach your screen.

## Setup

Requirements: Python 3.10+, `ffmpeg` on PATH, and ideally an NVIDIA GPU
(CPU works, ~10× slower).

```bash
python -m venv .venv && source .venv/bin/activate
pip install faster-whisper rapidfuzz pysubs2 fugashi unidic-lite
pip install nvidia-cublas-cu12 nvidia-cudnn-cu12   # GPU only
```

## Use

Put each episode in its own folder: **one video + its official Japanese sub(s)**.
An episode cut from several source episodes gets several sub files — they're used
in filename order, which matches playback order for normal naming (`S01E02` before `S01E19`).

Folders can nest however you like — anything containing exactly one video plus
sub file(s) is an episode; everything else is just organization:

```
inputs/
├── rd03/
│   ├── [One Pace][3-5] Romance Dawn 03 [1080p].mkv
│   ├── ワンピース.S01E02.WEBRip.Netflix.ja[cc].vtt      ← multi-source episode:
│   └── ワンピース.S01E19.WEBRip.Netflix.ja[cc].vtt      ← one sub per source ep
├── rd04/
│   └── ...
└── orange town/
    ├── 1/
    │   ├── [One Pace][8-11] Orange Town 01 [1080p].mkv
    │   ├── ワンピース.S01E04.WEBRip.Netflix.ja[cc].vtt
    │   ├── ワンピース.S01E05.WEBRip.Netflix.ja[cc].vtt
    │   └── ワンピース.S01E06.WEBRip.Netflix.ja[cc].vtt
    ├── 2/
    └── 3/
```

Then:

```bash
python opace_asr_bridge.py inputs/                 # everything, recursively
python opace_asr_bridge.py "inputs/orange town"    # one arc
python opace_asr_bridge.py inputs/rd03/            # one episode
```

First run downloads the Whisper model (~3 GB). Each episode takes ~3–5 minutes on a
mid-range GPU (transcription is cached — re-runs take seconds). The result lands next
to the video as `<video>.ja.ass`; mpv/VLC load it automatically. Episodes that already
have a `.ja.ass` are skipped — `--force` redoes them.

`--delete-video` removes each video (plus its cached audio) after successful
processing to reclaim disk space. Mind that the subs are only useful *with* the
video — use this for arcs you've already watched, or when the videos are copies of
a library kept elsewhere. Re-processing with different subs stays possible either
way (the transcript is cached separately and is all the matcher needs).

## Read the report

Each episode prints a report; the arc run ends with a summary. The important part is
the **gap list** — stretches with no subtitles, classified by what the audio contains:

```
gaps > 20s:
  01:26.75 -> 02:52.61  (quiet: song/transition, OK)            ← opening theme, fine
  17:46.25 -> 18:16.41  *** DIALOGUE, UNMATCHED -- missing a sub source? ***
      ASR heard: ぞろのかたなどこだりゅうが...                      ← read this sample!
```

A *dialogue* gap means people are talking but no official line matched — almost always
a **missing or wrong subtitle file**. One Pace episodes often pull content from source
episodes the episode title doesn't mention, and streaming-service episode boundaries
don't always match the TV episodes. The printed sample tells you (or a search engine)
which episode the missing scene is from: add that sub file to the folder and re-run
with `--force`.

## Fix a wrong line

- `<video>.ja.ass.debug.tsv` lists every official line with its placement method and
  time — `cover` = heard in the audio, `rescue`/`part` = fuzzy-matched, `interp`/`interp1`
  = inferred from context (no acoustic evidence; the lines to be most skeptical of).
- A line that genuinely shouldn't exist (One Pace cut it, but context made it look
  present): put its exact text in `<episode folder>/excludes.txt`, one line per entry,
  and re-run with `--force`.
- The opposite — a line you can hear but ASR couldn't (heavy music): put its exact
  text in `<episode folder>/pins.txt`; it gets placed by official-sub spacing from
  the nearest matched line. Your ears outrank every automated gate.
- `--separate` re-transcribes from a dialogue-isolated track (BS-RoFormer; needs
  `pip install "audio-separator[gpu]"` and `python3-dev`). Slower, and *not* better
  across the board — use it as a second opinion on stubborn episodes.

## Gotchas

- **The video must have Japanese audio.** Multi-audio releases are handled (the
  `jpn`-tagged track is selected automatically), but an English-dub-only file produces
  garbage — the telltale is a tiny "lines placed" count.
- Pure screams/grunts (`ぎゃあーっ`, `うっ…`) are only subtitled when actually heard —
  a wrong grunt is worse than a missing one.
- Opening/ending songs are intentionally unsubtitled (lyrics aren't in the dialogue subs).

## How it works, briefly

Whisper transcribes the episode with word-level timestamps. Both the transcript and the
official lines are converted to hiragana *readings* (MeCab), so kanji/kana spelling
differences don't matter. The two character streams are aligned; every official line
whose characters are found gets stamped with the time those characters were spoken.
Unmatched lines are recovered by windowed fuzzy search, proportional interpolation, and
anchor extrapolation — each gated by structural checks (scene continuity, gap ratios,
neighbor-spacing predictions) so that *no line is placed without either acoustic
evidence or strong structural proof*. Design rationale and war stories: [DEVLOG.md](DEVLOG.md).
