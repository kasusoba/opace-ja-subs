#!/usr/bin/env python3
"""
One Pace -> Japanese subtitles via AUDIO ALIGNMENT (no ASR, no English).

How it works
------------
One Pace's audio is the source-episode audio cut up and reordered. We acoustically
fingerprint the source episode(s), slide a window across the One Pace audio to find
where each moment came from, stitch those hits into constant-offset segments, then
slide the official Japanese subtitle (which is timed to the source episode) onto the
One Pace timeline through that map. Text and timing are both official; nothing is
transcribed or paraphrased.

Inputs
------
  --onepace  One Pace video OR audio (only the audio is used)
  --source   one or more source One Piece videos/audio
  --jasub    one Japanese sub (.srt/.ass/.vtt) per source, matched by FILENAME STEM
             e.g. source "ep01.mkv"  <->  jasub "ep01.ja.srt"  (stems must start equal)
  --outdir   output folder (default ./out)

  Single-source (RD02 = Ep1):  --source ep01.mkv --jasub ep01.ja.vtt
  Multi-source  (RD03 = 2+19): --source ep02.mkv ep19.mkv --jasub ep02.ja.vtt ep19.ja.vtt
                               (the fingerprint DB sorts out which chunk is which)

Requirements
------------
  ffmpeg on PATH ;  pip install audalign pysubs2

Run
---
  python opace_audio_align.py --onepace RD02.mkv \
         --source OnePiece_Ep01.mkv --jasub OnePiece_Ep01.ja.vtt --outdir out

This same command is also the RD02 prototype test: run it, then read the printed
SEGMENT MAP. If the segments show clean, steadily-increasing source times with a few
sane jumps, the alignment is trustworthy. If it's noise, the fan-edit transitions are
too aggressive and you fall back to the ASR bridge.
"""

import os, re, sys, glob, math, argparse, subprocess, tempfile, time, contextlib
import audalign as ad
import pysubs2

_DEVNULL = open(os.devnull, "w")


def _fmt(s):
    return f"{int(s // 60)}m{int(s % 60):02d}s"


def _first(x):  # audalign returns lists for confidence/offset_seconds
    if isinstance(x, (list, tuple)):
        return x[0] if x else 0
    return x if x is not None else 0


# ---- tunables ----
WIN = 6.0  # seconds per probe window
HOP = 3.0  # seconds between probe windows
ACC = 4  # audalign fingerprint accuracy (1-4)
MIN_CONF = 10  # ignore window matches below this confidence
DELTA_TOL = 0.40  # seconds; windows within this offset belong to one segment
MIN_WINDOWS = 2  # drop segments shorter than this many windows
DUAL_EN_ASS = None  # set via --en-ass to also show small English underneath

# ---- jp text cleanup (Netflix CC tags / speaker labels) ----
TAG = re.compile(r"\{[^}]*\}")
PAREN = re.compile(r"^[（(][^）)]*[）)]")
SFX = re.compile(r"[（(〈][^）)〉]*[）)〉]")


def clean_ja(t):
    t = t.replace(r"\N", "\n")
    t = TAG.sub("", t)
    t = "\n".join(PAREN.sub("", l) for l in t.split("\n"))
    t = SFX.sub("", t).replace("♪", "")
    return "\\N".join(l.strip() for l in t.split("\n") if l.strip())


def run(cmd):
    subprocess.run(
        cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )


def duration(path):
    out = subprocess.check_output(
        [
            "ffprobe",
            "-v",
            "quiet",
            "-show_entries",
            "format=duration",
            "-of",
            "csv=p=0",
            path,
        ]
    )
    return float(out.strip())


def to_wav(src, dst):  # 16k mono wav
    run(["ffmpeg", "-y", "-i", src, "-ac", "1", "-ar", "16000", "-vn", dst])


def slice_wav(src, start, length, dst):
    run(
        [
            "ffmpeg",
            "-y",
            "-ss",
            f"{start:.3f}",
            "-t",
            f"{length:.3f}",
            "-i",
            src,
            "-c",
            "copy",
            dst,
        ]
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--onepace", required=True)
    ap.add_argument("--source", nargs="+", required=True)
    ap.add_argument("--jasub", nargs="+", required=True)
    ap.add_argument("--outdir", default="out")
    ap.add_argument(
        "--en-ass",
        default=None,
        help="optional One Pace EN .ass for small dual subtitle",
    )
    a = ap.parse_args()
    os.makedirs(a.outdir, exist_ok=True)
    work = tempfile.mkdtemp(prefix="opace_")

    # pair each source with its JA sub
    def stem(p):
        return os.path.splitext(os.path.basename(p))[0]

    jamap = {}
    if len(a.jasub) == len(a.source):
        # equal counts -> pair by order given on the command line (most reliable)
        for src, js in zip(a.source, a.jasub):
            jamap[stem(src)] = js
    else:
        # unequal counts -> best-effort match by filename stem
        for js in a.jasub:
            s = stem(js)
            for src in a.source:
                if stem(src).startswith(s) or s.startswith(stem(src)):
                    jamap[stem(src)] = js
        missing = [stem(s) for s in a.source if stem(s) not in jamap]
        if missing:
            sys.exit(
                f"Could not match a JA sub to: {missing}.\n"
                "Easiest fix: pass --source and --jasub in the SAME ORDER with equal counts."
            )

    # 1) extract audio
    print("extracting audio...")
    op_wav = os.path.join(work, "onepace.wav")
    to_wav(a.onepace, op_wav)
    src_wavs = {}
    for src in a.source:
        w = os.path.join(work, stem(src) + ".wav")
        to_wav(src, w)
        src_wavs[stem(src)] = w

    # 2) fingerprint the source episode(s) into one database
    print(f"fingerprinting {len(src_wavs)} source episode(s)...")
    rec = ad.FingerprintRecognizer()
    rec.config.set_accuracy(ACC)
    for name, w in src_wavs.items():
        t0 = time.time()
        print(f"  [{name}] ...", end=" ", flush=True)
        rec.fingerprint_file(w)
        print(f"done ({_fmt(time.time() - t0)})")

    # 3) slide a window across One Pace audio; locate each window in the source DB
    op_dur = duration(op_wav)
    total = int(op_dur // HOP) + 1
    print(
        f"locating One Pace chunks in source: {total} windows to probe (slow part)..."
    )
    windows = []
    t = 0.0
    i = 0
    t_start = time.time()
    while t < op_dur:
        clip = os.path.join(work, "clip.wav")
        slice_wav(op_wav, t, WIN, clip)
        try:
            with contextlib.redirect_stdout(_DEVNULL):  # mute audalign's own chatter
                res = ad.recognize(clip, recognizer=rec)
        except Exception:
            res = None
        mi = res.get("match_info") if res else None
        if mi:
            name, info = max(mi.items(), key=lambda kv: _first(kv[1].get("confidence")))
            conf = _first(info.get("confidence"))
            off0 = _first(info.get("offset_seconds"))
            if conf >= MIN_CONF:
                src_key = next(
                    (k for k in src_wavs if k in name or name in k or k == stem(name)),
                    None,
                )
                if src_key:
                    delta = float(off0) - t  # ~ constant within a contiguous segment
                    windows.append((t, src_key, delta, conf))
        i += 1
        el = time.time() - t_start
        eta = el / i * (total - i)
        bar = "#" * int(24 * i / total)
        bar += "-" * (24 - len(bar))
        print(
            f"\r  [{bar}] {i}/{total} {100 * i / total:4.1f}%  elapsed {_fmt(el)}  eta {_fmt(eta)}  hits {len(windows)}   ",
            end="",
            file=sys.stderr,
            flush=True,
        )
        t += HOP
    print(file=sys.stderr)
    print(f"  {len(windows)} confident windows out of {total} probed")

    # 4) cluster consecutive windows of same source + near-constant delta into segments
    windows.sort(key=lambda x: x[0])
    segs = []
    cur = []
    for w in windows:
        if cur and (
            w[1] == cur[-1][1]
            and abs(w[2] - cur[-1][2]) <= DELTA_TOL
            and w[0] - cur[-1][0] <= HOP * 2.5
        ):
            cur.append(w)
        else:
            if len(cur) >= MIN_WINDOWS:
                segs.append(cur)
            cur = [w]
    if len(cur) >= MIN_WINDOWS:
        segs.append(cur)
    segments = []
    for c in segs:
        deltas = sorted(x[2] for x in c)
        d = deltas[len(deltas) // 2]
        segments.append(
            {"src": c[0][1], "op0": c[0][0], "op1": c[-1][0] + WIN, "delta": d}
        )

    # choose mapping sign that lands the most JA lines inside [0, op_dur]
    def map_lines(sign):
        out = 0
        for seg in segments:
            subs = pysubs2.load(jamap[seg["src"]])
            for ev in subs:
                if ev.is_comment:
                    continue
                s, e = ev.start / 1000.0, ev.end / 1000.0
                op_s = s - sign * seg["delta"]
                if seg["op0"] - 1 <= op_s <= seg["op1"] + 1:
                    out += 1
        return out

    sign = 1 if map_lines(1) >= map_lines(-1) else -1

    # 5) map official JA lines onto the One Pace timeline
    out = pysubs2.SSAFile()
    enmap = {}
    if a.en_ass:
        en = pysubs2.load(a.en_ass)
        enmap = [
            (ev.start / 1000.0, ev.end / 1000.0, ev.plaintext)
            for ev in en
            if not ev.is_comment
        ]
    placed = 0
    for seg in segments:
        subs = pysubs2.load(jamap[seg["src"]])
        for ev in subs:
            if ev.is_comment or not ev.plaintext.strip():
                continue
            s, e = ev.start / 1000.0, ev.end / 1000.0
            op_s = s - sign * seg["delta"]
            op_e = e - sign * seg["delta"]
            if op_e < seg["op0"] or op_s > seg["op1"]:
                continue  # outside this segment
            op_s = max(op_s, seg["op0"])
            op_e = min(op_e, seg["op1"])  # clip at cut boundary
            if op_e - op_s < 0.2:
                continue
            txt = clean_ja(ev.text)
            if not txt:
                continue
            line = pysubs2.SSAEvent(
                start=int(op_s * 1000), end=int(op_e * 1000), text=txt
            )
            out.append(line)
            placed += 1

    out.sort()
    dst = os.path.join(a.outdir, stem(a.onepace) + ".ja.ass")
    out.save(dst)

    # 6) report (read this on the RD02 test run!)
    print("\n=== SEGMENT MAP (One Pace time  <-  source time) ===")

    def hms(x):
        return f"{int(x // 60):02d}:{x % 60:05.2f}"

    for seg in segments:
        s0 = seg["op0"] + sign * seg["delta"]
        s1 = seg["op1"] + sign * seg["delta"]
        print(
            f"  OP {hms(seg['op0'])}-{hms(seg['op1'])}  <-  {seg['src']} {hms(s0)}-{hms(s1)}"
        )
    print(f"\nsegments: {len(segments)} | JA lines placed: {placed} | sign={sign:+d}")
    print(f"saved -> {dst}")


if __name__ == "__main__":
    main()
