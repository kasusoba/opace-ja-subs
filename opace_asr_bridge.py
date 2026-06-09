#!/usr/bin/env python3
"""
One Pace -> perfectly timed official Japanese subtitles, via ASR alignment.

Usage
-----
  python opace_asr_bridge.py inputs/            # whole arc (folder of episode folders)
  python opace_asr_bridge.py inputs/rd02/       # one episode folder
  python opace_asr_bridge.py --onepace EP.mkv --jasub ep1.vtt ep2.vtt   # explicit

An episode folder holds ONE video file + its official Japanese sub(s)
(.vtt/.srt/.ass). Multiple subs = multi-source episode, used in filename order.
The timed subtitle lands next to the video as <video>.ja.ass (players auto-load
it), with a <video>.ja.ass.debug.tsv sidecar showing how every line was placed.

How it works
------------
Transcribe the One Pace audio with faster-whisper (word timestamps), giving a
stream of Japanese characters each tagged with the time it was spoken. Convert
both that stream and the official subtitle lines to hiragana READINGS (MeCab),
align the two character streams, and emit every sufficiently-covered official
line at the time its characters were actually spoken. The ASR text itself is
only an alignment key and is never shown; lines One Pace cut never match and
drop out. Rescue/interpolation/extrapolation passes (see code) handle lines the
ASR misheard or missed, with structural guards against placing anything wrong.

Requirements
------------
  ffmpeg on PATH; pip install faster-whisper rapidfuzz pysubs2 fugashi unidic-lite
  CUDA: pip install nvidia-cublas-cu12 nvidia-cudnn-cu12 (preloaded below)
  optional --separate: pip install "audio-separator[gpu]" (needs python3-dev)

See README.md for the user guide and DEVLOG.md for design notes.
"""

import os, re, sys, argparse, subprocess, time, unicodedata, difflib, bisect

# make pip-installed CUDA libs visible to ctranslate2: ld.so ignores LD_LIBRARY_PATH
# changes after process start, so preload the .so files directly. (NB: nvidia.* are
# namespace packages -- __file__ is None, use __path__.)
def _preload_cuda_libs():
    try:
        import ctypes, glob
        import nvidia.cublas.lib, nvidia.cudnn.lib
        for pkg in (nvidia.cublas.lib, nvidia.cudnn.lib):
            for so in sorted(glob.glob(os.path.join(pkg.__path__[0], "*.so*"))):
                try:
                    ctypes.CDLL(so, mode=ctypes.RTLD_GLOBAL)
                except OSError:
                    pass  # optional sublibs may fail; cudnn loads what it needs
    except ImportError:
        pass  # system CUDA or CPU mode

_preload_cuda_libs()

import pysubs2
from rapidfuzz import fuzz

# ---- tunables ----
MIN_COVER = 0.50  # fraction of a line's chars that must be block-matched to place it
MIN_BLOCK = 2  # ignore matching blocks shorter than this (noise from ASR errors)
RESCUE_SCORE = 75  # fuzzy score to place an unmatched line between placed neighbors
RESCUE_MIN_CHARS = 5  # never rescue shorter lines: fuzzy scores on them are noise
PART_MIN_CHARS = 4  # \N-part rescue: minimum normalized chars per part
MAX_INTERP = 6  # max consecutive unheard lines to interpolate between placed ones
INTERP_RATIO = (0.8, 1.25)  # OP/VTT gap ratio proving the gap holds exactly this content
INTERP_CORROB = 50  # interp also needs the gap's ASR text to vaguely match the line
CLUSTER_GAP_S = 3.0  # cover hits further apart than this are separate speech
SPAN_MAX_X = 2.5  # cover span > this x the official cue duration = straddling junk

# interjections/grunts (うう… きゃーっ ははっ): only place with acoustic evidence --
# interpolating them guesses, and a wrong grunt is worse than a missing one.
# A kanji in the display text overrides: 撃て！/効かーん！ are words, not grunts.
GRUNT = re.compile(r"^[ぁ-おかきはひふへほわやゆよらゃゅょんっーう…]+$")
LEXICAL = re.compile(r"[一-鿿]")

# known song lyrics (normalized hiragana) -- gaps containing these are themes,
# not missing content. Extend as new OPs/EDs appear across arcs.
SONG_HINTS = ("ありったけのゆめをかきあつめ", "さがしものさがしにいくのさ")
PAD_S = 0.15  # s; pad emitted lines around first/last matched char
MIN_DUR = 0.40  # s; minimum emitted line duration

# ---- jp text cleanup (Netflix CC tags / speaker labels), same as align script ----
TAG = re.compile(r"\{[^}]*\}")
PAREN = re.compile(r"^[（(][^）)]*[）)]")
SFX = re.compile(r"[（(〈][^）)〉]*[）)〉]")


def clean_ja(t):
    t = t.replace(r"\N", "\n")
    t = TAG.sub("", t)
    t = "\n".join(PAREN.sub("", l) for l in t.split("\n"))
    t = SFX.sub("", t).replace("♪", "")
    return "\\N".join(l.strip() for l in t.split("\n") if l.strip())


PUNCT = re.compile(r"[\s。、！？!?…‥・「」『』〈〉（）()\[\]｢｣　~～ー—\-:：;；,.｡･]")

from fugashi import Tagger

_tagger = Tagger()


def _kata2hira(s):
    return "".join(chr(ord(c) - 0x60) if "ァ" <= c <= "ヶ" else c for c in s)


def norm(t):
    """normalize for matching: NFKC, convert to hiragana READINGS via MeCab (so
    kanji/kana orthography still matches: ASR 分かった vs CC わかった), THEN strip
    punctuation -- tagging before stripping keeps MeCab's segmentation context"""
    t = unicodedata.normalize("NFKC", t.replace("\\N", " "))
    out = []
    for w in _tagger(t):
        out.append(_kata2hira(w.feature.kana or w.surface))
    return PUNCT.sub("", "".join(out))


def _fmt(s):
    return f"{int(s // 60)}m{int(s % 60):02d}s"


def hms(x):
    return f"{int(x // 60):02d}:{x % 60:05.2f}"


def run(cmd):
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def jpn_audio_idx(src):
    """index of the first jpn-tagged audio stream, or None (single/untagged)"""
    out = subprocess.check_output(
        ["ffprobe", "-v", "quiet", "-select_streams", "a",
         "-show_entries", "stream=index:stream_tags=language", "-of", "csv=p=0", src]
    ).decode()
    streams = [l.split(",") for l in out.strip().splitlines() if l]
    if len(streams) <= 1:
        return None
    for parts in streams:
        if len(parts) > 1 and parts[1].strip().lower() in ("jpn", "ja", "jp"):
            return parts[0]
    print(f"  WARNING: {len(streams)} audio tracks, none tagged jpn -- using first;")
    print("           if the result is sparse, the wrong (dub) track was picked.")
    return None


def to_wav(src, dst):  # 16k mono wav; skip if already extracted
    if os.path.exists(dst) and os.path.getsize(dst) > 0:
        print(f"  (reusing {os.path.basename(dst)})")
        return
    cmd = ["ffmpeg", "-y", "-i", src]
    idx = jpn_audio_idx(src)
    if idx is not None:
        print(f"  (multi-audio file: selecting jpn track, stream #{idx})")
        cmd += ["-map", f"0:{idx}"]
    cmd += ["-ac", "1", "-ar", "16000", "-vn", dst]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def process(a, onepace, jasubs, outdir):
    """Align one episode: returns (placed, total_lines, dialogue_gap_count)."""
    os.makedirs(outdir, exist_ok=True)
    os.makedirs("work", exist_ok=True)

    # manual kill list: <episode dir>/excludes.txt, one exact display text per
    # line. For evidence-free disputes only a human can settle (One Pace cut a
    # single line whose neighbors are all in -- no algorithm can know).
    excludes = set()
    exc_path = os.path.join(outdir, "excludes.txt")
    if os.path.exists(exc_path):
        with open(exc_path, encoding="utf-8") as f:
            excludes = {l.strip() for l in f if l.strip()}
        print(f"  ({len(excludes)} excluded lines from {exc_path})")
    # ...and the inverse: <episode dir>/pins.txt forces lines the human KNOWS
    # are present (heavy-BGM scenes ASR can't hear). A bare line is placed by
    # official-sub spacing from the nearest placed neighbor; a line prefixed
    # with a timestamp ("11:39  槌！") is placed at that exact start -- the
    # human's ears beat spacing-inference, which drifts across reorders/cuts.
    pins = {}  # display text -> explicit start (s), or None for spacing-inferred
    pin_path = os.path.join(outdir, "pins.txt")
    if os.path.exists(pin_path):
        with open(pin_path, encoding="utf-8") as f:
            for l in f:
                if not l.strip():
                    continue
                m = re.match(r"\s*(\d+):(\d\d(?:\.\d+)?)\s+(.+?)\s*$", l)
                if m:
                    pins[m.group(3)] = int(m.group(1)) * 60 + float(m.group(2))
                else:
                    pins[l.strip()] = None
        print(f"  ({len(pins)} pinned lines from {pin_path})")

    # 1) extract One Pace audio (reuses ./work cache)
    print("extracting audio...")
    stem = os.path.splitext(os.path.basename(onepace))[0]
    wav = os.path.join("work", stem + ".wav")  # per-episode name, NOT shared
    if a.separate:
        # vocal isolation wants quality input (44.1k stereo); whisper wants 16k
        # mono. extract hifi -> separate -> downmix the vocals stem.
        wav = os.path.join("work", stem + ".vocals16k.wav")
        if not (os.path.exists(wav) and os.path.getsize(wav) > 0):
            hifi = os.path.join("work", stem + ".hifi.wav")
            if not (os.path.exists(hifi) and os.path.getsize(hifi) > 0):
                cmd = ["ffmpeg", "-y", "-i", onepace]
                idx = jpn_audio_idx(onepace)
                if idx is not None:
                    print(f"  (multi-audio file: selecting jpn track, stream #{idx})")
                    cmd += ["-map", f"0:{idx}"]
                run(cmd + ["-ac", "2", "-ar", "44100", "-vn", hifi])
            print("  isolating dialogue (BS-RoFormer)...", flush=True)
            t0 = time.time()
            from audio_separator.separator import Separator

            sep = Separator(output_dir="work", log_level=40)  # ERROR only
            sep.load_model("model_bs_roformer_ep_317_sdr_12.9755.ckpt")
            outputs = sep.separate(hifi)
            voc = next(
                os.path.join("work", o) for o in outputs if "(Vocals)" in o
            )
            run(["ffmpeg", "-y", "-i", voc, "-ac", "1", "-ar", "16000", wav])
            os.remove(voc)
            for o in outputs:  # drop the instrumental stem too
                p = os.path.join("work", o)
                if os.path.exists(p):
                    os.remove(p)
            print(f"  done ({_fmt(time.time() - t0)})")
        else:
            print(f"  (reusing {os.path.basename(wav)})")
    else:
        to_wav(onepace, wav)

    # 2) transcribe with word timestamps -> per-char (char, start, end) stream.
    #    Words are cached to disk so matcher tweaks don't re-pay GPU minutes.
    #    No VAD: dialogue over BGM gets dropped by it, and music hallucinations
    #    don't match official text anyway (they just waste a few chars).
    import json

    asr_cache = os.path.join(
        "work",
        f"asr_{stem}.{os.path.basename(a.model)}{'.sep' if a.separate else ''}.json",
    )
    if os.path.exists(asr_cache):
        with open(asr_cache, encoding="utf-8") as f:
            words = json.load(f)
        print(f"  (reusing cached transcript: {len(words)} words)")
    else:
        print(f"loading {a.model} on {a.device}...")
        from faster_whisper import WhisperModel

        t0 = time.time()
        model = WhisperModel(
            a.model, device=a.device,
            compute_type="float16" if a.device == "cuda" else "int8",
        )
        print(f"  loaded ({_fmt(time.time() - t0)}); transcribing...")
        t0 = time.time()
        seg_iter, info = model.transcribe(
            wav,
            language="ja",
            condition_on_previous_text=False,  # avoids hallucination loops in BGM
            word_timestamps=True,
            beam_size=5,
        )
        words = []  # [start, end, raw_text]
        for s in seg_iter:
            for w in s.words or []:
                words.append([w.start, w.end, w.word])
            print(
                f"\r  {hms(s.end)} / {hms(info.duration)}  ({len(words)} words)",
                end="", file=sys.stderr, flush=True,
            )
        print(file=sys.stderr)
        with open(asr_cache, "w", encoding="utf-8") as f:
            json.dump(words, f, ensure_ascii=False)
        print(f"  {len(words)} words in {_fmt(time.time() - t0)} (cached)")

    # Whisper "words" are arbitrary fragments that can split mid-morpheme and wreck
    # MeCab readings (結構重いぜ -> けつかまえ...). So: build the raw char stream
    # with per-char times first, tag it ONCE with full context, then map each
    # token's reading back to the times of its surface chars.
    # char times are END-anchored with a per-char duration cap: whisper word
    # ENDS are reliable but starts smear into preceding silence/SFX (お前 heard
    # as a 2.3s word whose お "starts" 2s early) -- uniform interpolation would
    # launder that smear across all chars
    raw_chars, raw_t0, raw_t1 = [], [], []
    for ws, we, wraw in words:
        wtxt = re.sub(r"\s", "", unicodedata.normalize("NFKC", wraw))
        if not wtxt:
            continue
        n = len(wtxt)
        cd = min(max(we - ws, 1e-3) / n, 0.5)
        for ci, ch in enumerate(wtxt):
            raw_chars.append(ch)
            raw_t0.append(we - (n - ci) * cd)
            raw_t1.append(we - (n - ci - 1) * cd)
    raw_str = "".join(raw_chars)
    asr_chars, asr_t0, asr_t1 = [], [], []
    pos = 0
    for tok in _tagger(raw_str):
        p0_, p1_ = pos, pos + len(tok.surface)
        pos = p1_
        hira = PUNCT.sub("", _kata2hira(tok.feature.kana or tok.surface))
        if not hira:
            continue
        t0_, t1_ = raw_t0[p0_], raw_t1[p1_ - 1]
        for ci, ch in enumerate(hira):
            asr_chars.append(ch)
            asr_t0.append(t0_ + (t1_ - t0_) * ci / len(hira))
            asr_t1.append(t0_ + (t1_ - t0_) * (ci + 1) / len(hira))
    # excise whisper's stock music-hallucinations (ご視聴ありがとうございました et
    # al.) -- they aren't speech, and they make truly-untranscribable regions look
    # like contradicting dialogue
    HALLUC = ["ごしちょうありがとうございました", "ごせいちょうありがとうございました",
              "チャンネルとうろくおねがいします"]
    asr_str = "".join(asr_chars)
    cut = 0
    for h in HALLUC:
        i = asr_str.find(h)
        while i >= 0:
            del asr_chars[i : i + len(h)]
            del asr_t0[i : i + len(h)]
            del asr_t1[i : i + len(h)]
            asr_str = "".join(asr_chars)
            cut += len(h)
            i = asr_str.find(h)
    if cut:
        print(f"  (excised {cut} hallucinated chars)")
    # whisper's OTHER hallucination species: repetition loops ("DXは私の体を…"
    # xN over noisy audio). Collapse immediate repeats of any short chunk.
    loop_cut = 0
    while True:
        m = re.search(r"(.{6,40}?)\1{2,}", asr_str)
        if not m:
            break
        keep_end = m.start() + len(m.group(1))
        del asr_chars[keep_end : m.end()]
        del asr_t0[keep_end : m.end()]
        del asr_t1[keep_end : m.end()]
        loop_cut += m.end() - keep_end
        asr_str = "".join(asr_chars)
    if loop_cut:
        print(f"  (collapsed {loop_cut} repetition-loop chars)")
    print(f"  {len(asr_str)} ASR chars (context-tagged hiragana)")

    # 3) official JA lines -> one normalized char stream with line boundaries.
    #    Netflix CC rolls up: the same cue text repeats at near-identical times --
    #    dedupe, or every copy lands on the same audio as a stack of duplicates.
    lines = []  # (display_text, norm_start, norm_end, vtt_start_s, vtt_end_s)
    off_parts = []
    pos = 0
    dropped_dups = 0
    recent = []  # (norm, vtt_start) of recently kept lines
    for js in jasubs:
        subs = pysubs2.load(js)
        for ev in subs:
            if ev.is_comment:
                continue
            disp = clean_ja(ev.text)
            n = norm(disp)
            if not disp or len(n) < 2:
                continue
            vs = ev.start / 1000.0
            if any(rn == n and vs - rt < 6.0 for rn, rt in recent):
                dropped_dups += 1
                continue
            recent = [(rn, rt) for rn, rt in recent if vs - rt < 6.0][-4:] + [(n, vs)]
            lines.append((disp, pos, pos + len(n), vs, ev.end / 1000.0))
            off_parts.append(n)
            pos += len(n)
    off_str = "".join(off_parts)
    if dropped_dups:
        print(f"  ({dropped_dups} rollup-duplicate cues dropped)")
    print(f"  {len(lines)} official lines ({len(off_str)} chars) from {len(jasubs)} file(s)")

    # 4) align the two char streams; both are monotonic through the episode.
    #    autojunk=False is essential: with chars, every symbol is "popular".
    print("aligning char streams...")
    t0 = time.time()
    sm = difflib.SequenceMatcher(None, asr_str, off_str, autojunk=False)
    blocks = [b for b in sm.get_matching_blocks() if b.size >= MIN_BLOCK]
    covered = sum(b.size for b in blocks)
    print(
        f"  {len(blocks)} blocks, {covered} chars matched "
        f"({100 * covered / max(len(off_str), 1):.0f}% of official) "
        f"({_fmt(time.time() - t0)})"
    )

    # per official-char: matched ASR char index (or -1)
    off2asr = [-1] * len(off_str)
    for b in blocks:
        for k in range(b.size):
            off2asr[b.b + k] = b.a + k

    # 4b) second pass over the leftovers: One Pace REORDERS scenes (RD03 plays
    #    Ep2's ending before its middle), and a single monotonic alignment drops
    #    whatever was displaced. The displaced chunk is still internally ordered,
    #    so aligning (unmatched ASR) x (unmatched official) recovers it.
    asr_used = [False] * len(asr_str)
    for b in blocks:
        for k in range(b.size):
            asr_used[b.a + k] = True
    a_map = [i for i in range(len(asr_str)) if not asr_used[i]]
    o_map = [p for p in range(len(off_str)) if off2asr[p] < 0]
    asr2 = "".join(asr_str[i] for i in a_map)
    off2 = "".join(off_str[p] for p in o_map)
    sm2 = difflib.SequenceMatcher(None, asr2, off2, autojunk=False)
    pass2 = 0
    for b in sm2.get_matching_blocks():
        if b.size < max(MIN_BLOCK, 3):  # leftovers are noisier: demand >= 3
            continue
        for k in range(b.size):
            off2asr[o_map[b.b + k]] = a_map[b.a + k]
            pass2 += 1
    print(f"  reorder pass matched {pass2} more chars")

    # 5a) place each official line whose chars are covered enough. Hits are first
    #    trimmed to their largest time-contiguous cluster: a line whose matched
    #    chars straddle disjoint speech (a stray 2-char hit far away) is junk.
    placement = [None] * len(lines)  # (asr_lo, asr_hi) char-index span
    part_events = []  # (start_s, end_s, part_text, parent_line)
    partial_done = set()  # lines resolved as "only part of the cue is in the cut"
    first_hit = set()  # lines whose first char was acoustically matched
    coverages = []
    for li, (disp, p0, p1, vs, ve) in enumerate(lines):
        hp = [(p, off2asr[p]) for p in range(p0, p1) if off2asr[p] >= 0]
        clusters = []
        for p, h in hp:  # hits come in official-char order, times nearly sorted
            if clusters and abs(asr_t0[h] - asr_t0[clusters[-1][-1][1]]) <= CLUSTER_GAP_S:
                clusters[-1].append((p, h))
            else:
                clusters.append([(p, h)])
        best = max(clusters, key=len) if clusters else []
        cover = len(best) / max(p1 - p0, 1)
        coverages.append(cover)
        ntxt = off_str[p0:p1]
        if len(ntxt) <= 4 and GRUNT.match(ntxt):
            continue  # short interjections cover-match on noise (おい anywhere)
        if cover < MIN_COVER:
            continue
        if any(p == p0 for p, _ in best):
            first_hit.add(li)  # the line's FIRST char was heard (snap guard)
        span_dur = asr_t1[max(h for _, h in best)] - asr_t0[min(h for _, h in best)]
        if span_dur > max(SPAN_MAX_X * (ve - vs), ve - vs + 2.0):
            continue
        # the ASR text under the span must actually resemble the line: block hits
        # can be fragment noise (１発で当たった "matching" 俺は強いって on った/って)
        span_txt = asr_str[min(h for _, h in best) : max(h for _, h in best) + 1]
        if fuzz.ratio(ntxt, span_txt) < 45 and fuzz.partial_ratio(ntxt, span_txt) < 55:
            continue
        if "\\N" in disp:
            # the edit may enter/leave MID-cue: if one display part has hits and
            # another has none, emit only the covered part(s)
            parts = disp.split("\\N")
            plens = [max(len(norm(x)), 1) for x in parts]
            tot = sum(plens)
            bounds, q = [], 0
            for pl in plens:  # proportional char ranges within [p0, p1)
                b0 = p0 + q * (p1 - p0) / tot
                q += pl
                bounds.append((b0, p0 + q * (p1 - p0) / tot))
            pcov = [
                sum(1 for p, _ in best if b0 <= p < b1) / max(b1 - b0, 1)
                for b0, b1 in bounds
            ]
            sel = [i for i, c in enumerate(pcov) if c >= 0.4]
            if sel and any(c < 0.15 for c in pcov):
                sub = [
                    h for p, h in best
                    if any(bounds[i][0] <= p < bounds[i][1] for i in sel)
                ]
                span = asr_t1[max(sub)] - asr_t0[min(sub)] if sub else 0
                # if the heard part already fills the whole cue's duration, the
                # unheard part is CONCURRENT speech (two speakers) -> keep the
                # full cue text; otherwise the rest was cut -> emit parts only
                if sub and span < 0.7 * (ve - vs):
                    part_events.append(
                        (
                            asr_t0[min(sub)] - PAD_S,
                            asr_t1[max(sub)] + PAD_S,
                            "\\N".join(parts[i] for i in sel),
                            li,
                        )
                    )
                    partial_done.add(li)
                    continue
        placement[li] = (min(h for _, h in best), max(h for _, h in best))

    # 5b) rescue: an unplaced line between two placed neighbors can only live in
    #    the ASR span between them -- fuzzy-search just that window (monotonic by
    #    construction, so a decent score there is trustworthy)
    rescued = 0
    rescued_set = set()
    placed_idx = [i for i, p in enumerate(placement) if p]
    for li in range(len(lines)):
        if placement[li] or li in partial_done:
            continue
        prev_p = max((i for i in placed_idx if i < li), default=None)
        next_p = min((i for i in placed_idx if i > li), default=None)
        if prev_p is None or next_p is None:
            continue
        lo = placement[prev_p][1] + 1
        hi = placement[next_p][0]
        ntxt = off_str[lines[li][1] : lines[li][2]]
        if hi - lo < 2 or len(ntxt) < RESCUE_MIN_CHARS:
            continue
        al = fuzz.partial_ratio_alignment(ntxt, asr_str[lo:hi])
        if al is not None and al.score >= RESCUE_SCORE and al.dest_end > al.dest_start:
            disp = lines[li][0]
            if "\\N" in disp:
                # the match may cover only PART of the cue (edit enters mid-cue):
                # check which display parts the matched ntxt range covers
                parts = disp.split("\\N")
                plens = [max(len(norm(p)), 1) for p in parts]
                tot = sum(plens)
                bounds, q = [], 0
                for pl in plens:  # proportional char ranges within ntxt
                    b0 = q * len(ntxt) / tot
                    q += pl
                    bounds.append((b0, q * len(ntxt) / tot))
                sel = [
                    i
                    for i, (b0, b1) in enumerate(bounds)
                    if min(al.src_end, b1) - max(al.src_start, b0) >= 0.6 * (b1 - b0)
                ]
                if 0 < len(sel) < len(parts):
                    part_events.append(
                        (
                            asr_t0[lo + al.dest_start] - PAD_S,
                            asr_t1[lo + al.dest_end - 1] + PAD_S,
                            "\\N".join(parts[i] for i in sel),
                            li,
                        )
                    )
                    partial_done.add(li)  # keep interp away: rest of the cue is cut
                    rescued += 1
                    continue
            placement[li] = (lo + al.dest_start, lo + al.dest_end - 1)
            rescued_set.add(li)
            rescued += 1
    print(f"  rescue pass placed {rescued} more lines")

    # 5b2) part rescue: a still-unplaced multi-part (\N) cue may exist only HALF in
    #    the One Pace cut (e.g. the edit enters mid-cue). Search each part alone;
    #    emit just the parts that match.
    placed_idx = [i for i, p in enumerate(placement) if p]
    for li in range(len(lines)):
        if placement[li] or li in partial_done or "\\N" not in lines[li][0]:
            continue
        prev_p = max((i for i in placed_idx if i < li), default=None)
        next_p = min((i for i in placed_idx if i > li), default=None)
        if prev_p is None or next_p is None:
            continue
        lo = placement[prev_p][1] + 1
        hi = placement[next_p][0]
        if hi - lo < 2:
            continue
        for part in lines[li][0].split("\\N"):
            ptxt = norm(part)
            if (len(ptxt) < PART_MIN_CHARS or GRUNT.match(ptxt)) and not LEXICAL.search(part):
                continue
            al = fuzz.partial_ratio_alignment(ptxt, asr_str[lo:hi])
            if al is not None and al.score >= RESCUE_SCORE and al.dest_end > al.dest_start:
                part_events.append(
                    (
                        asr_t0[lo + al.dest_start] - PAD_S,
                        asr_t1[lo + al.dest_end - 1] + PAD_S,
                        part,
                        li,
                    )
                )
    print(f"  part-rescue placed {len(part_events)} cue parts")

    # 5c) spans -> times. Whisper's aligner smears word boundaries across SFX/music
    #    (ルフィ stretched to 3s over a rubber-band sound): edge chars with artifact
    #    durations (>3x median, or zero-width) are trimmed, backing off one normal
    #    char duration per trimmed char.
    def refine_span(lo, hi):
        if hi - lo + 1 < 2:
            return asr_t0[lo], asr_t1[hi]
        durs = sorted(asr_t1[h] - asr_t0[h] for h in range(lo, hi + 1))
        # lower median: in a short span half the chars can be smear artifacts
        med = max(durs[(len(durs) - 1) // 2], 0.12)
        thr = max(3 * med, 0.8)

        def artifact(h):
            d = asr_t1[h] - asr_t0[h]
            return d > thr or d <= 1e-6

        i, j = lo, hi
        while i < j and artifact(i):
            i += 1
        while j > i and artifact(j):
            j -= 1
        s = max(asr_t0[i] - min((i - lo) * med * 1.5, 1.2), asr_t0[lo])
        e = min(asr_t1[j] + min((hi - j) * med * 1.5, 1.2), asr_t1[hi])
        return s, e

    times = [None] * len(lines)  # (start_s, end_s)
    method = [None] * len(lines)
    for li, span in enumerate(placement):
        if span:
            s, e = refine_span(span[0], span[1])
            times[li] = (s - PAD_S, e + PAD_S)
            method[li] = "rescue" if li in rescued_set else "cover"


    # 5d) interpolate short unplaced runs (soft/overlapping speech ASR can't hear,
    #    e.g. continuation lines) between placed neighbors: the official sub knows
    #    their relative timing, so map it proportionally into the gap.
    interpolated = 0
    li = 0
    while li < len(lines):
        if times[li] or not (0 < li < len(lines)):
            li += 1
            continue
        run0 = li
        while li < len(lines) and not times[li]:
            li += 1
        run1 = li  # run is [run0, run1)
        if run0 == 0 or run1 >= len(lines) or run1 - run0 > MAX_INTERP:
            continue
        if not (times[run0 - 1] and times[run1]):
            continue
        op0, op1 = times[run0 - 1][1], times[run1][0]  # gap in One Pace time
        vt0, vt1 = lines[run0 - 1][4], lines[run1][3]  # same gap in official time
        opw, vtw = op1 - op0, vt1 - vt0
        # only when a near-1 ratio proves the gap holds exactly this content
        if opw <= 0.2 or vtw <= 0 or not (INTERP_RATIO[0] <= opw / vtw <= INTERP_RATIO[1]):
            continue
        scale = opw / vtw
        # the gap's ASR text (even garbled, ASR usually heard SOMETHING of a real
        # line; a cut boundary's gap contains different dialogue instead)
        import bisect

        ai0 = bisect.bisect_left(asr_t0, op0)
        ai1 = bisect.bisect_right(asr_t0, op1)
        gap_txt = asr_str[ai0:ai1]
        for k in range(run0, run1):
            if k in partial_done:
                continue  # already resolved as a partially-cut cue
            ntxt = off_str[lines[k][1] : lines[k][2]]
            if (len(ntxt) < 4 or GRUNT.match(ntxt)) and not LEXICAL.search(lines[k][0]):
                continue  # no acoustic evidence for a grunt/short line -> leave it out
            if not gap_txt or fuzz.partial_ratio(ntxt, gap_txt) < INTERP_CORROB:
                continue  # gap is silent or audibly contains something ELSE
            s = op0 + (lines[k][3] - vt0) * scale
            e = op0 + (lines[k][4] - vt0) * scale
            times[k] = (s, e)
            method[k] = "interp"
            interpolated += 1
    print(f"  interpolation pass placed {interpolated} more lines")

    # 5d2) consistency snap: official inter-line spacing is preserved wherever One
    #    Pace didn't cut. When BOTH neighbors predict the same start (locally
    #    linear -> no cut here) and a line sits >1.2s off that prediction, its
    #    evidence matched the wrong instance / a blurred edge -- snap it.
    frozen = list(times)
    placed_lis = [i for i, t in enumerate(frozen) if t]
    snapped = 0
    junked = 0
    for ix, li in enumerate(placed_lis):
        if ix == 0 or ix == len(placed_lis) - 1 or method[li] == "interp":
            continue
        # HIGH-QUALITY acoustic evidence beats neighbor predictions: a dense
        # cover span that includes the line's first char heard the line start
        # directly -- neighbor-spacing noise must not drag it (俺の宝物に触るな
        # was heard at its true time and snapped 2s early). Lines whose start
        # was NOT heard (はっ はい…) still benefit from prediction snaps, and
        # the guard never shields multi-second deviations: those are the WRONG
        # INSTANCE of a repeated line, not boundary noise (何だ？).
        solid = False
        if li in first_hit and placement[li]:
            n_sp = placement[li][1] - placement[li][0] + 1
            d_sp = asr_t1[placement[li][1]] - asr_t0[placement[li][0]]
            lo_sp = placement[li][0]
            # a tiny span counts as high-quality too if its onset is CLEAN
            # (silence gap before it -- not glued to the previous word like 俺？)
            lead_gap = asr_t0[lo_sp] - asr_t1[lo_sp - 1] if lo_sp > 0 else 9.9
            solid = d_sp / n_sp <= 0.45 and (n_sp >= 5 or lead_gap >= 0.3)
        pv, nx = placed_lis[ix - 1], placed_lis[ix + 1]
        d_pv = lines[li][3] - lines[pv][3]  # vtt start deltas
        d_nx = lines[nx][3] - lines[li][3]
        pred_pv = frozen[pv][0] + d_pv
        pred_nx = frozen[nx][0] - d_nx
        # a TINY line sitting far from BOTH neighbor predictions matched the
        # wrong instance entirely (あったぞ on the OP song's ありったけ) -- drop
        if (
            lines[li][2] - lines[li][1] <= 4
            and abs(frozen[li][0] - pred_pv) > 6
            and abs(frozen[li][0] - pred_nx) > 6
        ):
            times[li] = None
            method[li] = None
            junked += 1
            continue
        if abs(pred_pv - pred_nx) > 2.5:
            # neighbors disagree: a cut lies somewhere here. But if ONE neighbor
            # is vtt-CLOSE (same beat, <=3s -- no cut that tight), its one-sided
            # prediction is still trustworthy (whisper timestamps drift seconds
            # in music; e.g. あっ ありがとう after a scream).
            sided = [(d, p) for d, p in ((d_pv, pred_pv), (d_nx, pred_nx)) if d <= 3.0]
            if len(sided) != 1:
                continue
            pred = sided[0][1]
            dev = frozen[li][0] - pred
            if 1.2 < abs(dev) <= 6.0 and not (solid and abs(dev) <= 2.0):
                s0, e0 = frozen[li]
                times[li] = (pred, pred + (e0 - s0))
                method[li] += "+snap1"
                snapped += 1
            continue
        w_pv, w_nx = 1.0 / max(d_pv, 0.5), 1.0 / max(d_nx, 0.5)
        pred = (pred_pv * w_pv + pred_nx * w_nx) / (w_pv + w_nx)
        dev = frozen[li][0] - pred
        # 1-3 char lines: whisper word boundaries blur into pauses (a reply's
        # beat gets glued to the previous line) -- trust the prediction sooner.
        # Same when both neighbors agree near-exactly: that's a provably linear
        # region, and a deviation there is evidence smear, not a cut.
        short = lines[li][2] - lines[li][1] <= 3
        tol = 0.4 if short or abs(pred_pv - pred_nx) <= 0.3 else 1.2
        if solid and abs(dev) <= 2.0:
            continue  # trust the heard onset over sub-2s prediction pull
        if tol < abs(dev) <= 6.0:
            s0, e0 = frozen[li]
            times[li] = (pred, pred + (e0 - s0))
            method[li] += "+snap"
            snapped += 1
    print(f"  consistency snap moved {snapped} lines, junked {junked} tiny strays")

    # 5d3) times-level dedupe BEFORE extrapolation: the same utterance matched by
    #    several official lines (catchphrase in scene + previews) must lose its
    #    placement NOW -- a surviving false claim would seed 5d4 chains from a
    #    false anchor. Scene continuity (placed neighbors nearby) wins, then
    #    method strength.
    def _ctx_prio(li):
        ctx = any(
            times[li2] and 0 < abs(li2 - li) <= 3
            and abs(times[li2][0] - times[li][0]) <= 30
            for li2 in range(max(li - 3, 0), min(li + 4, len(lines)))
        )
        m = method[li] or ""
        return (ctx, 3 if m.startswith("cover") else (1 if m.startswith("rescue") else 2))

    survivors = []
    dup_cleared = 0
    for li in sorted((i for i, t in enumerate(times) if t), key=lambda i: times[i][0]):
        drop = False
        for s_li in survivors[-3:]:
            ov = min(times[li][1], times[s_li][1]) - max(times[li][0], times[s_li][0])
            if ov > 0.6 * min(
                times[li][1] - times[li][0], times[s_li][1] - times[s_li][0]
            ) and fuzz.ratio(norm(lines[li][0]), norm(lines[s_li][0])) >= 65:
                if _ctx_prio(li) > _ctx_prio(s_li):
                    survivors.remove(s_li)
                    times[s_li] = None
                    method[s_li] = None
                else:
                    drop = True
                dup_cleared += 1
                break
        if drop:
            times[li] = None
            method[li] = None
        else:
            survivors.append(li)
    if dup_cleared:
        print(f"  cleared {dup_cleared} duplicate claims")

    # 5d3.6) pre-extrapolation island drop: a false anchor (short line matching
    #    ED lyrics) must die BEFORE 5d4, or its extrapolation chain becomes its
    #    own alibi against the later island check.
    pre_islands = 0
    placed_now = sorted((i for i, t in enumerate(times) if t), key=lambda i: times[i][0])
    snap = {i: times[i] for i in placed_now}  # frozen: don't read mutating times[] below
    for ix, li in enumerate(placed_now):
        idx_near = any(
            li2 in snap and 0 < abs(li2 - li) <= 3
            for li2 in range(max(li - 3, 0), min(li + 4, len(lines)))
        )
        op_near = (ix > 0 and snap[li][0] - snap[placed_now[ix - 1]][1] <= 30) or (
            ix + 1 < len(placed_now)
            and snap[placed_now[ix + 1]][0] - snap[li][1] <= 30
        )
        if not idx_near and not op_near:
            times[li] = None
            method[li] = None
            pre_islands += 1
    if pre_islands:
        print(f"  dropped {pre_islands} pre-extrapolation islands")

    # 5d4) one-sided contiguous extrapolation: whisper sometimes produces NOTHING
    #    for a loud action scene (hallucinates ご視聴ありがとう instead), leaving a
    #    sub-free hole with no evidence for any pass above. If an unplaced run is
    #    vtt-CONTIGUOUS with a placed anchor (each step <=8s), lay it out by vtt
    #    spacing from that anchor -- only into event-free, ASR-empty territory.
    def asr_density(s, e):
        i0 = bisect.bisect_left(asr_t0, s)
        i1 = bisect.bisect_right(asr_t0, e)
        return (i1 - i0) / max(e - s, 1.0)

    def region_free(s, e, ignore_li):
        for li2, t in enumerate(times):
            if t and li2 != ignore_li and min(e, t[1]) - max(s, t[0]) > 0.3:
                return False
        return True

    one_sided = 0
    for anchor in [i for i, t in enumerate(times) if t]:
        # BACKWARD only: dialogue leading up to an anchored line is continuous
        # with it; walking FORWARD past an anchor steps over scene cuts (a cut
        # right after a scene's last line resurrects cut content into silence)
        for step in (-1,):
            ref = anchor
            li = anchor + step
            while 0 <= li < len(lines) and not times[li]:
                if abs(lines[ref][3] - lines[li][3]) > 8.0:
                    break  # vtt gap: run is not contiguous with the anchor
                if abs(lines[anchor][3] - lines[li][3]) > 35.0:
                    break  # extrapolation horizon: vtt spacing is only locally
                    #        reliable; long chains drift into unrelated territory
                s = times[anchor][0] + (lines[li][3] - lines[anchor][3])
                e = s + (lines[li][4] - lines[li][3])
                ntxt = off_str[lines[li][1] : lines[li][2]]
                ok = (
                    (len(ntxt) >= 4 and not GRUNT.match(ntxt) or LEXICAL.search(lines[li][0]))
                    and s > 0
                    and region_free(s, e, li)
                    and (
                        asr_density(s, e) < 0.3  # silent/hallucination-excised
                        or fuzz.partial_ratio(
                            ntxt,
                            asr_str[
                                bisect.bisect_left(asr_t0, s) : bisect.bisect_right(
                                    asr_t0, e
                                )
                            ],
                        )
                        >= INTERP_CORROB
                    )
                )
                if ok:
                    times[li] = (s, e)
                    method[li] = "interp1"
                    one_sided += 1
                ref = li
                li += step
    # an extrapolated line whose entire run failed to place (1 of 7 in a cut
    # scene) has no scene support -- evidence-free placements need company
    unsupported = 0
    for li in [i for i, m in enumerate(method) if m == "interp1"]:
        if not any(
            times[li2] and 0 < abs(li2 - li) <= 3
            for li2 in range(max(li - 3, 0), min(li + 4, len(lines)))
        ):
            times[li] = None
            method[li] = None
            unsupported += 1
    if one_sided:
        print(
            f"  one-sided extrapolation placed {one_sided} lines"
            + (f" (dropped {unsupported} unsupported)" if unsupported else "")
        )

    # 5d5) pins: human-confirmed lines, placed by official spacing from the
    #    nearest placed line (human evidence outranks every gate above)
    pinned = 0
    for li in range(len(lines)):
        disp = lines[li][0]
        if times[li]:
            continue
        key = disp if disp in pins else (
            disp.replace("\\N", " ") if disp.replace("\\N", " ") in pins else None
        )
        if key is None:
            continue
        explicit = pins[key]
        if explicit is not None:
            s = explicit  # human-timed: place exactly where they heard it
        else:
            anchor = min(
                (i for i, t in enumerate(times) if t),
                key=lambda i: abs(lines[i][3] - lines[li][3]),
                default=None,
            )
            if anchor is None:
                continue
            s = times[anchor][0] + (lines[li][3] - lines[anchor][3])
        times[li] = (s, s + (lines[li][4] - lines[li][3]))
        method[li] = "pin"
        pinned += 1
    if pinned:
        print(f"  pinned {pinned} human-confirmed lines")


    # 5e) emit: extend ends toward the official cue's display duration (speech ends
    #    before a reader is done), then clamp every overlap to the next line's start
    emit = []  # (start_s, end_s, text, display_dur_prior, can_extend_start, line_idx)
    for li in range(len(lines)):
        if times[li]:
            emit.append(
                (
                    times[li][0],
                    times[li][1],
                    lines[li][0],
                    lines[li][4] - lines[li][3],
                    "snap" not in (method[li] or ""),
                    li,
                )
            )
    for s, e, txt, parent in part_events:
        if times[parent]:
            continue  # whole cue got placed later (e.g. interp) -- avoid duplicates
        emit.append((s, e, txt, e - s, False, None))
    emit.sort(key=lambda x: x[0])

    # dedupe: two events overlapping in time with near-identical text are one
    # utterance claimed twice (the catchphrase appears in several official lines
    # across episodes/previews). Keep the best-evidenced one.
    placed_set = sorted(i for i, t in enumerate(times) if t)

    def _prio(ev):
        eli = ev[5]
        if eli is None:
            return (0, 2)  # part event
        # scene continuity first: a line whose line-order neighbors are placed
        # nearby in time is part of a scene; preview/orphan copies of the same
        # text (catchphrases recur in next-episode previews) have no context
        ctx = 0
        j = bisect.bisect_left(placed_set, eli)
        for li2 in placed_set[max(j - 3, 0) : j + 4]:
            if li2 != eli and abs(li2 - eli) <= 3 and times[li2] and abs(
                times[li2][0] - ev[0]
            ) <= 30:
                ctx = 1
                break
        m = method[eli] or ""
        return (ctx, 3 if m.startswith("cover") else (1 if m.startswith("rescue") else 2))

    kept = []
    dup_dropped = 0
    for ev in emit:
        drop = False
        for kev in kept[-3:]:
            ov = min(ev[1], kev[1]) - max(ev[0], kev[0])
            if ov > 0.6 * min(ev[1] - ev[0], kev[1] - kev[0]) and fuzz.ratio(
                norm(ev[2]), norm(kev[2])
            ) >= 65:
                if _prio(ev) > _prio(kev):
                    kept.remove(kev)
                else:
                    drop = True
                dup_dropped += 1
                break
        if not drop:
            kept.append(ev)
    if dup_dropped:
        print(f"  dropped {dup_dropped} duplicate-claim events")

    # island drop: an event with no other surviving event nearby in EITHER line
    # order (±3) or OP time (30s) has no corroborating context -- a stray match
    # (e.g. a short line matching ED song lyrics). Runs AFTER dedupe so deleted
    # duplicate claims can't pose as neighbors.
    kept_lis = sorted(e[5] for e in kept if e[5] is not None)
    import bisect as _bi

    final = []
    dropped_islands = 0
    for i, ev in enumerate(kept):
        op_near = (i > 0 and ev[0] - kept[i - 1][1] <= 30) or (
            i + 1 < len(kept) and kept[i + 1][0] - ev[1] <= 30
        )
        idx_near = True
        if ev[5] is not None:
            j = _bi.bisect_left(kept_lis, ev[5])
            idx_near = (j > 0 and ev[5] - kept_lis[j - 1] <= 3) or (
                j + 1 < len(kept_lis) and kept_lis[j + 1] - ev[5] <= 3
            )
        if not op_near and not idx_near:
            if ev[5] is not None:
                times[ev[5]] = None
                method[ev[5]] = None
            dropped_islands += 1
            continue
        final.append(ev)
    # a part event ADJACENT to an event whose text already contains its words is
    # the same utterance claimed twice (cue 女１人におめえら…何人がかりだ vs
    # another cue's お前ら part) -- absorb it, extending the keeper's end
    absorbed = []
    for i, ev in enumerate(final):
        if (
            ev[5] is None  # part event
            and absorbed
            and ev[0] - absorbed[-1][1] <= 0.5
            and fuzz.partial_ratio(norm(ev[2]), norm(absorbed[-1][2])) >= 75
        ):
            k = absorbed[-1]
            absorbed[-1] = (k[0], max(k[1], ev[1])) + k[2:]
            continue
        absorbed.append(ev)
    emit = absorbed
    if dropped_islands:
        print(f"  dropped {dropped_islands} stray island placements")
    placed = len(emit)
    out = pysubs2.SSAFile()
    final_times = {}  # line_idx -> emitted (start, end), for the debug TSV
    prev_end = None
    emit = [ev for ev in emit if ev[2].replace("\\N", " ") not in excludes
            and ev[2] not in excludes]
    placed = len(emit)
    for oi, (s, e, txt, vtt_dur, can_ext, eli) in enumerate(emit):
        # whisper clips onsets after silence (cut boundaries): when a SHORT span
        # (single word -- longer spans have solid onsets) is compressed well below
        # the cue's official duration and there's room, reconstruct the start
        # backward from the (reliable) end. NB: long cue durations are display
        # holds, not speech -- never stretch a healthy multi-word span to one.
        if can_ext and (e - s) < 1.2 and (e - s) < 0.6 * vtt_dur:
            floor = prev_end + 0.03 if prev_end is not None else 0.0
            new_s = max(e - vtt_dur, floor, s - 2.5)
            if new_s < s - 0.3:
                s = new_s
        e = max(e, s + min(vtt_dur, 7.0))  # display-duration prior, capped
        if e - s < MIN_DUR:
            e = s + MIN_DUR
        if oi + 1 < len(emit):  # no overlap into the next line
            nxt = emit[oi + 1][0]
            if e > nxt - 0.03 and nxt - 0.03 > s + MIN_DUR / 2:
                e = nxt - 0.03
        prev_end = e
        if eli is not None:
            final_times[eli] = (max(s, 0), e)
        out.append(
            pysubs2.SSAEvent(start=int(max(s, 0) * 1000), end=int(e * 1000), text=txt)
        )
    out.sort()
    dst = os.path.join(outdir, stem + ".ja.ass")
    out.save(dst)

    # sidecar for tracing: how every official line was (or wasn't) placed
    with open(dst + ".debug.tsv", "w", encoding="utf-8") as f:
        f.write("line\tmethod\top_start\top_end\tvtt_start\ttext\n")
        for li, (disp, p0, p1, vs, ve) in enumerate(lines):
            if times[li]:
                fs, fe = final_times.get(li, times[li])  # emitted times, not raw
                f.write(
                    f"{li}\t{method[li]}\t{hms(fs)}\t{hms(fe)}\t{hms(vs)}\t{disp}\n"
                )
            else:
                f.write(f"{li}\t-\t-\t-\t{hms(vs)}\t{disp}\n")
        for s, e, txt, parent in sorted(part_events):
            f.write(f"{parent}\tpart\t{hms(s)}\t{hms(e)}\t-\t{txt}\n")

    # machine-readable twin of the TSV for the review player (reviewer/index.html):
    # every official line with its final on-screen times + emitted flag, so the
    # player can render the track AND surface unplaced lines for auditing.
    import json as _json
    review = {
        "episode": stem,
        "video": os.path.basename(onepace),
        "lines": [],
        "parts": [],
    }
    for li, (disp, p0, p1, vs, ve) in enumerate(lines):
        ft = final_times.get(li)
        review["lines"].append(
            {
                "n": li,
                "text": disp,
                "method": method[li] if ft else None,
                "start": round(ft[0], 3) if ft else None,
                "end": round(ft[1], 3) if ft else None,
                "vtt": round(vs, 3),
                "emitted": ft is not None,
            }
        )
    for s, e, txt, parent in sorted(part_events):
        if parent not in final_times:  # a part shows only when its cue didn't
            review["parts"].append(
                {"parent": parent, "text": txt, "start": round(s, 3), "end": round(e, 3)}
            )
    with open(dst + ".review.json", "w", encoding="utf-8") as f:
        _json.dump(review, f, ensure_ascii=False)

    # 6) report
    ev_sorted = sorted((ev.start / 1000.0, ev.end / 1000.0) for ev in out)
    gaps = []
    for (s0, e0), (s1, e1) in zip(ev_sorted, ev_sorted[1:]):
        if s1 - e0 > 20:
            gaps.append((e0, s1))
    print("\n=== ASR BRIDGE REPORT ===")
    print(
        f"official lines placed: {placed}/{len(lines)} "
        f"({100 * placed / max(len(lines), 1):.0f}% -- unplaced should be cut content)"
    )
    cs = sorted(coverages, reverse=True)
    if cs:
        print(
            f"line char coverage   : median {100 * cs[len(cs) // 2]:.0f}%  "
            f"(placed lines are >= {100 * MIN_COVER:.0f}%)"
        )
    dialogue_gaps = 0
    if gaps:
        print("gaps > 20s:")
        unplaced_norms = [
            off_str[p0:p1] for li, (d_, p0, p1, _, _) in enumerate(lines) if not times[li]
        ]
        for g0, g1 in gaps[:10]:
            # quiet gap = song/transition. Dense gap is classified further:
            # known lyrics -> theme; resembles an UNPLACED official line ->
            # garbled-but-present (ASR mush, content not actually missing);
            # otherwise -> likely a missing subtitle source (how RD03's
            # mis-mapped segment was caught).
            ai0 = bisect.bisect_left(asr_t0, g0)
            ai1 = bisect.bisect_right(asr_t0, g1)
            gap_txt = asr_str[ai0:ai1]
            density = len(gap_txt) / max(g1 - g0, 1)
            if density <= 0.5:
                print(f"  {hms(g0)} -> {hms(g1)}  (quiet: song/transition, OK)")
            elif any(h in gap_txt for h in SONG_HINTS):
                print(f"  {hms(g0)} -> {hms(g1)}  (opening/ending theme, OK)")
            else:
                # do the subs have an unplaced run that could OWN this gap? The
                # flanking placed lines bracket it in line order: if the official
                # time span between them roughly fills the gap, the content is in
                # the subs but the audio was too garbled to match. If the flanking
                # lines are vtt-adjacent, the subs have nothing to offer -> a sub
                # source is genuinely missing (the RD03 case).
                placed_by_t = sorted(
                    (t[0], t[1], li) for li, t in enumerate(times) if t
                )
                li0 = max((p for p in placed_by_t if p[1] <= g0 + 0.5), default=None)
                li1 = min((p for p in placed_by_t if p[0] >= g1 - 0.5), default=None)
                vtt_between = 0
                if li0 and li1:
                    vtt_between = max(0, lines[li1[2]][3] - lines[li0[2]][4])
                fuzz_best = max(
                    (fuzz.partial_ratio(n, gap_txt[:200]) for n in unplaced_norms if len(n) >= 5),
                    default=0,
                )
                if vtt_between >= 0.5 * (g1 - g0) or fuzz_best >= 55:
                    print(
                        f"  {hms(g0)} -> {hms(g1)}  (unmatchable audio -- noisy "
                        f"scene; if a line feels missing here, pins.txt it)"
                    )
                else:
                    print(f"  {hms(g0)} -> {hms(g1)}  *** DIALOGUE, UNMATCHED -- missing a sub source? ***")
                    dialogue_gaps += 1
                print(f"      ASR heard: {gap_txt[:40]}...")
    print(f"saved -> {dst}")
    return placed, len(lines), dialogue_gaps


VIDEO_EXT = (".mkv", ".mp4", ".m4v", ".avi", ".webm")
SUB_EXT = (".vtt", ".srt", ".ass")


def find_episode(d):
    """(video, [subs]) for an episode dir, or (None, []) if it isn't one.
    Subs sort by filename = source-episode playback order (Netflix names sort
    naturally: S01E02 < S01E19). Generated *.ja.ass outputs are not inputs."""
    videos, subs = [], []
    for f in sorted(os.listdir(d)):
        p = os.path.join(d, f)
        if not os.path.isfile(p):
            continue
        if f.lower().endswith(VIDEO_EXT):
            videos.append(p)
        elif f.lower().endswith(SUB_EXT) and not f.endswith(".ja.ass"):
            subs.append(p)
    if len(videos) == 1 and subs:
        return videos[0], subs
    return None, []


def main():
    ap = argparse.ArgumentParser(
        description="Time official Japanese subtitles to One Pace episodes by "
        "aligning them against the episode's own audio (ASR).",
        epilog="Folder mode: each episode lives in its own folder holding ONE "
        "video file plus its official Japanese sub(s) (.vtt/.srt/.ass; multiple "
        "subs = multi-source episode, sorted by filename in playback order). "
        "Point this script at an episode folder, or at an arc folder containing "
        "episode folders, and the timed .ja.ass lands next to each video.",
    )
    ap.add_argument(
        "paths",
        nargs="*",
        help="episode folder(s) and/or arc folder(s) of episode folders",
    )
    ap.add_argument("--onepace", help="explicit mode: One Pace video/audio file")
    ap.add_argument("--jasub", nargs="+", help="explicit mode: official JA sub(s)")
    ap.add_argument("--outdir", help="explicit mode: output folder")
    ap.add_argument("--model", default="large-v3", help="faster-whisper model (default %(default)s)")
    ap.add_argument("--device", default="cuda", choices=["cuda", "cpu"])
    ap.add_argument("--force", action="store_true", help="redo episodes whose .ja.ass already exists")
    ap.add_argument(
        "--delete-video",
        action="store_true",
        help="delete each video (and its cached wav) after SUCCESSFUL processing. "
        "Careful: you still need the video to WATCH the subs -- use this for "
        "already-watched arcs or when the video is a copy of a library elsewhere",
    )
    ap.add_argument(
        "--separate",
        action="store_true",
        help="isolate dialogue from music/SFX (BS-RoFormer) before transcribing; "
        "second-opinion tool for hallucination-heavy episodes, not the default",
    )
    a = ap.parse_args()

    if a.onepace:  # explicit single-episode mode
        if not a.jasub:
            ap.error("--onepace requires --jasub")
        process(a, a.onepace, a.jasub, a.outdir or os.path.dirname(a.onepace) or ".")
        return
    if not a.paths:
        ap.error("give episode/arc folder(s), or use --onepace/--jasub")

    # discover episodes recursively: an episode folder = exactly one video +
    # at least one sub; any other folder is just a container (arcs can nest:
    # inputs/orange town/1/, inputs/rd02/, ... organize however you like)
    def discover(root):
        v, s = find_episode(root)
        if v:
            return [(root, v, s)]
        vids = [
            f for f in os.listdir(root)
            if os.path.isfile(os.path.join(root, f)) and f.lower().endswith(VIDEO_EXT)
        ]
        if vids:  # video present but dir doesn't qualify -> don't skip silently
            print(f"WARNING: skipping {root}: {len(vids)} video(s) but no subs (or >1 video)")
        eps = []
        for sub in sorted(os.listdir(root)):
            d = os.path.join(root, sub)
            if os.path.isdir(d):
                eps += discover(d)
        return eps

    episodes = []  # (dir, video, subs)
    for path in a.paths:
        if not os.path.isdir(path):
            sys.exit(f"not a folder: {path}")
        found = discover(path)
        if not found:
            sys.exit(
                f"no episodes under {path} (an episode folder = exactly one "
                f"video + at least one .vtt/.srt/.ass sub)"
            )
        episodes += found

    summary = []
    for d, video, subs in episodes:
        name = os.path.relpath(d)
        done = os.path.splitext(video)[0] + ".ja.ass"
        if os.path.exists(done) and not a.force:
            summary.append((name, "skipped (done; --force to redo)"))
            continue
        print(f"\n=== {name}: {os.path.basename(video)} + {len(subs)} sub(s) ===")
        try:
            placed, total, dgaps = process(a, video, subs, d)
            note = f"{placed}/{total} lines"
            if dgaps:
                note += f"  *** {dgaps} unmatched DIALOGUE gap(s) -- check report! ***"
            if a.delete_video and placed > 0:
                freed = os.path.getsize(video)
                os.remove(video)
                vstem = os.path.splitext(os.path.basename(video))[0]
                for w in os.listdir("work"):
                    if w.startswith(vstem) and w.endswith(".wav"):
                        freed += os.path.getsize(os.path.join("work", w))
                        os.remove(os.path.join("work", w))
                note += f"  (video deleted, {freed / 1e9:.1f}GB freed)"
            summary.append((name, note))
        except Exception as e:
            summary.append((name, f"FAILED: {e}"))
    print("\n=== ARC SUMMARY ===")
    width = max((len(n) for n, _ in summary), default=12) + 2
    for name, note in summary:
        print(f"  {name:{width}s} {note}")


if __name__ == "__main__":
    main()
