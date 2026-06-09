# opace subtitle reviewer

A tiny single-file web app for **reviewing** a generated `.ja.ass` while you watch,
and **capturing** what's wrong as a structured list — without copying Japanese,
cross-referencing `debug.tsv`, or writing prose.

It is a *capture* tool, not a resolver: it records observations (this line
shouldn't be here / I hear a missing line here / this is mistimed / wrong line /
free note) and exports them as a list for a human to judge into the right fix
(exclude, pin, retime, or a code change). It never edits subtitles itself.

## Use

1. Process the episode so it has a `…​.ja.ass.review.json` sidecar (any normal
   `opace_asr_bridge.py` run now writes one next to the `.ass`/`.tsv`).
2. Open `reviewer/index.html` in a browser (double-click it — no server needed).
3. Drag in the **video** and its **`.review.json`** (or use the pick buttons).
4. Watch. The right panel is every official line in order:
   - **shown** lines (blue time) — click to seek; the current one highlights and
     auto-follows;
   - **unplaced** lines (greyed) — and when one falls in the gap you're currently
     watching, it lights up (`in gap`) as a "is this spoken here?" candidate. This
     is the auditor: it catches missing lines you'd otherwise have to notice by ear.
5. Flag issues with the buttons (per row, or the bar under the video):
   | Button | Meaning |
   |---|---|
   | ✕ remove | shown but shouldn't be (One Pace cut it) |
   | ⚑ I hear it here | an unplaced line *is* spoken at this moment |
   | ⏱ retime | right line, wrong time — seek to the correct start first |
   | ≠ wrong | the wrong line is shown here (say what it should be in the note) |
   | ✎ note | anything else, tied to the moment |
6. When the episode's done, **Export** (or **Copy JSON**) and send the
   `…​.review-notes.json` over. Observations autosave to the browser as you go,
   so closing the tab doesn't lose them.

## Keys

<kbd>space</kbd> play/pause · <kbd>←</kbd>/<kbd>→</kbd> seek 2s
(<kbd>shift</kbd> = 10s) · <kbd>,</kbd>/<kbd>.</kbd> nudge 0.1s ·
<kbd>x</kbd> remove current line · <kbd>c</kbd> note at current moment.
In the flag dialog: <kbd>esc</kbd> cancel, <kbd>ctrl/⌘ + enter</kbd> save.

## Export shape

```json
{
  "episode": "[One Pace][19-21] Orange Town 03 …",
  "video": "….mkv",
  "observations": [
    { "kind": "missing", "n": 206, "text": "ハーッ…", "at": 671.3, "note": null },
    { "kind": "retime",  "n": 82,  "text": "シュシュ？", "at": 238.0,
      "shownStart": 235.98, "note": "barking smeared the onset" },
    { "kind": "exclude", "n": 438, "text": "よーし\\N曲芸ショーを見せてやれ", "note": null }
  ]
}
```

`at` = the timestamp you flagged (heard-at, or the corrected start for retime).
`n`/`text` reference the official line. The judge turns each observation into the
appropriate `excludes.txt`/`pins.txt` entry or code change.

## Notes

- Browsers play `.mkv` only if they support the codecs inside (these One Pace files
  usually work — it's the same `<video>` path asbplayer's web app uses for local
  files). If a file won't decode, remux losslessly: `ffmpeg -i in.mkv -c copy out.mp4`.
- Plain-text rendering (no ASS styling) — these subs are dialogue-only, so it
  matches what you'll see in mpv/VLC closely enough for review.
