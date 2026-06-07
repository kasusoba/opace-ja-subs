#!/usr/bin/env python3
"""Probe a few One Pace windows and dump audalign's raw match output,
to determine the offset sign/convention that broke segment clustering."""
import os, contextlib, subprocess
import audalign as ad

HERE = os.path.dirname(os.path.abspath(__file__))
WORK = os.path.join(HERE, "work")
os.makedirs(WORK, exist_ok=True)
SRC_VID = os.path.join(HERE, "one piece official 01.mkv")
OP_VID = os.path.join(HERE, "[One Pace][2] Romance Dawn 02 [480p][En CC][7CEC60A5].mp4")
SRC = os.path.join(WORK, "one piece official 01.wav")
OP = os.path.join(WORK, "onepace.wav")
WIN = 6.0


def to_wav(src, dst):
    if os.path.exists(dst):
        return
    subprocess.run(
        ["ffmpeg", "-y", "-i", src, "-ac", "1", "-ar", "16000", "-vn", dst],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


print("extracting...", flush=True)
to_wav(SRC_VID, SRC)
to_wav(OP_VID, OP)

rec = ad.FingerprintRecognizer()
rec.config.set_accuracy(4)
print("fingerprinting source...", flush=True)
with contextlib.redirect_stdout(open(os.devnull, "w")):
    rec.fingerprint_file(SRC)

# consecutive windows (to see how delta moves) + a few scattered ones
for t in [60.0, 63.0, 66.0, 69.0, 300.0, 600.0, 900.0, 1100.0]:
    clip = os.path.join(WORK, f"dbg_{int(t)}.wav")
    subprocess.run(
        ["ffmpeg", "-y", "-ss", f"{t:.3f}", "-t", f"{WIN:.3f}", "-i", OP, "-c", "copy", clip],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    with contextlib.redirect_stdout(open(os.devnull, "w")):
        res = ad.recognize(clip, recognizer=rec)
    if not res or not res.get("match_info"):
        print(f"t={t:7.1f}  NO MATCH")
        continue
    for name, info in res["match_info"].items():
        offs = info.get("offset_seconds", [])[:5]
        confs = info.get("confidence", [])[:5]
        print(f"t={t:7.1f}  match={name!r}")
        print(f"           offsets[:5]={[round(o, 2) for o in offs]}")
        print(f"           confs[:5]={confs}")
        print(f"           keys={sorted(info.keys())}")
