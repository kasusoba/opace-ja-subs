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
5. Flag issues with the buttons (per row, or the bar under the video). Each flag
   **previews live** on the track — the line moves, retimes, trims, appears, or
   vanishes immediately so you can verify it — but the real `.ass` is only changed
   when the pipeline bakes your exported notes (below). The **?** button opens the
   same reference in-app.
   | Button | Meaning |
   |---|---|
   | ✕ remove | shown but shouldn't be (One Pace cut it) |
   | ⚑ I hear it here | an unplaced line *is* spoken at this moment |
   | ⏱ retime | right line, wrong time (e.g. One Pace trimmed it) — drag the start/end **tips** on the slider (video scrubs as you drag) or type a new start/end |
   | ✂ trim | One Pace cut part of the line — **click a character** to set the nearest edge (or drag the tips / ◀▶) and keep a contiguous **substring** of the official text (no typing; timing via retime) |
   | ≠ wrong | wrong line shown — type which **other official line** should be here |
   | ✏ fix text | the official line's **text itself** is wrong — type the corrected text (timing unchanged); the box pre-fills with the current text so you just edit it |
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

   **Multi-select** rows with **Ctrl/⌘-click** (toggle) or **Shift-click** (range)
   for bulk actions, via the bar that appears above the list:
   - **▦ Place block @ playhead** — for a stretch whose official lines exist but are
     all unplaced (a BGM-buried scene): select them, park the playhead where the
     first is spoken, and they're laid out from there by official spacing. Use this
     instead of *no sub here* when the lines *do* exist and you just need them placed.
   - **✕ Remove all** — flag every selected line for removal.
   - To fix a wrong-instance region: select the wrong lines, then **right-click the
     correct line** → *N selected wrong — this is right*.
   <kbd>Esc</kbd> clears the selection.

   The observations panel sits in the right column **under the subtitle list** —
   drag the divider to make it bigger (it eats the list, never the video), or
   click its header to collapse it. The video player never changes size.
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
<kbd>c</kbd> note at current moment · <kbd>m</kbd> mine current line → Anki.
In the flag dialog: <kbd>esc</kbd> cancel, <kbd>ctrl/⌘ + enter</kbd> save.

All shortcuts are **rebindable** (and the seek interval is adjustable) in the
**⚙** settings panel — click a key to rebind, ✕ to unbind it entirely. Settings
persist in the browser.

## Mining words to Anki (⛏ / <kbd>m</kbd>)

Because the reviewer is where the `.ass` is *being made*, it's also where the
**live** subtitle timing lives — so it mints the Anki card itself instead of
routing through asbplayer (no more dragging the sub onto an overlay, and the
audio always matches your latest retime/trim).

Workflow, same muscle memory as asbplayer + Yomitan:
1. Hover a word with **Yomitan** → *add to Anki* (this creates the note: word,
   gloss, sentence).
2. With the line on screen, press **<kbd>m</kbd>** (or click **⛏ mine**). The
   reviewer screenshots the line's start frame, records its audio in real time
   (the playhead jumps to the line and plays it, then returns — just like
   asbplayer), encodes it to **MP3**, and writes `[sound:…]` + `<img>` onto the
   **most-recently-added note** (the Yomitan one). Headless — a toast confirms.

**One-time setup**, in **⚙ Settings → Anki** (everything is a dropdown, populated
live from AnkiConnect — click **↻** to (re)connect; the status line confirms):
- **AnkiConnect URL** — usually `http://127.0.0.1:8765`.
- **Deck** + **Note Type** — pick the same deck and note type Yomitan mines to
  (e.g. `Mining` / `Animecards`). Mining attaches to the **newest note added
  today in this deck + type**, so these scope it to your Yomitan note.
- **Audio field** / **Image field** — chosen from the note type's actual fields
  (e.g. `SentenceAudio`, `Picture`). Leave Image on `(none)` to skip screenshots.
- **Audio pad start/end** — seconds of slack around the line (default 0).

**AnkiConnect must allow this page's origin** (the one cross-machine gotcha):
in Anki → *Tools → Add-ons → AnkiConnect → Config*, add your origin to
`webCorsOriginList`. If you double-click the file (`file://`), add `"null"`; if
you serve it over a local server, add e.g. `"http://localhost:8000"`. Quick and
local-only: set it to `["*"]`. (Yomitan/asbplayer needed the same kind of
whitelisting — it's an Anki security gate, not a reviewer quirk.) Restart Anki
after editing. If mining toasts `mine failed: Failed to fetch`, this is why.

Notes on the capture: it records in real time, so a 3-second line takes ~3
seconds; audio is captured from the `<video>` via `captureStream()` (don't mute
the player while mining). MP3 keeps cards playable on AnkiMobile/AnkiDroid.

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
    { "kind": "trim", "n": 60, "text": "てめえ 今の事態\\N分かってんのか！",
      "keep": "今の事態\\N分かってんのか！", "trimStart": 4, "trimEnd": 17 },
    { "kind": "edit", "n": 73, "text": "海賊王に俺はなる", "shownStart": 412.5,
      "fixed": "海賊王に俺はなる！", "note": "official sub dropped the 「！」" },
    { "kind": "section", "from": 1052.0, "to": 1098.0, "note": "Buggy crew fight — no subs at all" },
    { "kind": "exclude", "n": 438, "text": "よーし\\N曲芸ショーを見せてやれ" }
  ]
}
```

Time fields: `at` (heard-at / note moment), `start`/`end` (retime — either or both),
`from`/`to` (section span). `shownStart` is where the line currently sits, for
reference. `n`/`text` point at the official line; `shouldBe` names the correct one
(for `wrong`); `fixed` is the corrected text (for `edit`).
## Baking notes into the sub

Drop the exported `…​.review-notes.json` next to the video and run the pipeline
(`python opace_asr_bridge.py "<episode>" --redo`). It applies the notes as a
final edit layer — exclude/wrong remove a line, missing/wrong place one at the
heard time, retime moves start/end, trim/edit rewrite the text — baking them into
the `.ja.ass` that mpv/VLC load. No re-matching or re-transcription happens.

`review.json` is deliberately left **raw** (the pre-notes matcher track), so the
reviewer always previews your notes on top of it without ever double-applying.
The notes you couldn't express as a simple edit (a `section`, a `note`, anything
structural) still need a human/code decision; the rest bake automatically.

## Notes

- Browsers play `.mkv` only if they support the codecs inside (these One Pace files
  usually work — it's the same `<video>` path asbplayer's web app uses for local
  files). If a file won't decode, remux losslessly: `ffmpeg -i in.mkv -c copy out.mp4`.
- Plain-text rendering (no ASS styling) — these subs are dialogue-only, so it
  matches what you'll see in mpv/VLC closely enough for review.
