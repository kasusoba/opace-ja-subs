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
5. Flag issues with the buttons (per row, or the bar under the video). Each is
   just a captured *observation* — nothing is auto-fixed. The **?** button opens
   the same reference in-app.
   | Button | Meaning |
   |---|---|
   | ✕ remove | shown but shouldn't be (One Pace cut it) |
   | ⚑ I hear it here | an unplaced line *is* spoken at this moment |
   | ⏱ retime | right line, wrong time — set a new start and/or end (blank = keep) |
   | ≠ wrong | wrong line shown — type what it should be |
   | ✎ note | anything else, tied to the moment |
   | ▭ no sub here | a whole stretch (from → to) has no subtitle |

   Subtitles on the video are **selectable** — hover with Yomitan to look up words.

   **Right-click any line** for quick actions at the current playhead — including
   *Set start → now* / *Set end → now* (emitted lines) and *Place here → now*
   (unplaced). This is the fast path for a **late/early sub**: park the playhead
   where the line should start, right-click it, *Set start → now* — no dialog, no
   copy/paste. When a wrong line is on screen, right-click the **correct** one and
   pick *Shown #X is wrong — this is right*. Clicking a blue timestamp also
   **copies** it (and the "now" clock under the video is click-to-copy).

   The observations strip under the video is a fixed height that scrolls (it never
   shrinks the video); click its header to collapse it for a bigger picture.
6. When the episode's done, **Export** (or **Copy JSON**) and send the
   `…​.review-notes.json` over.

### Your notes are safe across refreshes

Every flag is **autosaved** to the browser (`localStorage`, keyed by episode) the
moment you make it. After a refresh or crash, just re-drop the **same** video +
`.review.json` — your observations reappear (the header shows "↩ restored N notes").
For a hard backup or moving machines, **Export** periodically and use **Import** to
merge a notes file back in (duplicates are skipped). Re-dropping local files is
required because browsers can't silently re-open them — but the *notes* persist.

## Keys

<kbd>space</kbd> play/pause · <kbd>←</kbd>/<kbd>→</kbd> previous/next subtitle ·
<kbd>a</kbd>/<kbd>d</kbd> seek back/forward by the interval (default 3s) ·
<kbd>,</kbd>/<kbd>.</kbd> nudge 0.1s · <kbd>x</kbd> remove current line ·
<kbd>c</kbd> note at current moment. In the flag dialog: <kbd>esc</kbd> cancel,
<kbd>ctrl/⌘ + enter</kbd> save.

All shortcuts are **rebindable** (and the seek interval is adjustable) in the
**⚙** settings panel — click a key to rebind, ✕ to unbind it entirely. Settings
persist in the browser.

## Export shape

```json
{
  "episode": "[One Pace][19-21] Orange Town 03 …",
  "video": "….mkv",
  "observations": [
    { "kind": "missing", "n": 206, "text": "ハーッ…", "at": 671.3 },
    { "kind": "retime",  "n": 82,  "text": "シュシュ？", "shownStart": 235.98,
      "start": 238.0, "note": "barking smeared the onset" },
    { "kind": "retime",  "n": 624, "text": "クソーッ…", "shownStart": 1898.3, "end": 1903.0 },
    { "kind": "wrong",   "n": 50,  "text": "あいー あいあいあい", "shownStart": 147.8,
      "shouldBe": "あっ 犬！ ヘヘッ (line 49)" },
    { "kind": "section", "from": 1052.0, "to": 1098.0, "note": "Buggy crew fight — no subs at all" },
    { "kind": "exclude", "n": 438, "text": "よーし\\N曲芸ショーを見せてやれ" }
  ]
}
```

Time fields: `at` (heard-at / note moment), `start`/`end` (retime — either or both),
`from`/`to` (section span). `shownStart` is where the line currently sits, for
reference. `n`/`text` point at the official line; `shouldBe` names the correct one.
The judge turns each observation into the appropriate `excludes.txt`/`pins.txt`
entry, a retime, or a code change.

## Notes

- Browsers play `.mkv` only if they support the codecs inside (these One Pace files
  usually work — it's the same `<video>` path asbplayer's web app uses for local
  files). If a file won't decode, remux losslessly: `ffmpeg -i in.mkv -c copy out.mp4`.
- Plain-text rendering (no ASS styling) — these subs are dialogue-only, so it
  matches what you'll see in mpv/VLC closely enough for review.
