"""
enroll.py - Build voiceprints.json from per-mic podcast tracks.

Usage:
    python enroll.py --tracks ./tracks --out voiceprints.json [--minutes-note "..."]

./tracks holds one file per mic per episode, named "<episode title> - <Speaker>.<ext>":
    Women's Strength Standards - Phil.wav
    Women's Strength Standards - Sean.wav
    HIIT Training Miracle or Myth - Phil.mp3
    HIIT Training Miracle or Myth - Sean.mp3
    HIIT Training Miracle or Myth - Rowen.mp3

Files with the same episode prefix are compared against each other frame by frame:
a speaker owns a frame only when their mic is at least 6 dB louder than every other
mic (so bleed and crosstalk are excluded). Each speaker's clean speech across all
episodes is embedded in 5-second chunks and averaged into one unit-norm voiceprint.

Two or more episodes per speaker, two to four minutes of clean speech each, is plenty.
Adding a new regular voice later = drop their tracks in and rerun. Re-deploy afterwards.
"""
import argparse, json, os, re, subprocess, sys, datetime
import numpy as np

SR = 16000; FRAME = 0.1; DOM_DB = 6.0; FLOOR_DB = 12.0

def decode(path):
    cmd = ["ffmpeg", "-v", "error", "-i", path, "-ac", "1", "-ar", str(SR), "-f", "s16le", "-acodec", "pcm_s16le", "-"]
    out = subprocess.run(cmd, capture_output=True, timeout=1200)
    if out.returncode != 0:
        raise RuntimeError(path + ": " + out.stderr.decode("ascii", "ignore")[:200])
    return np.frombuffer(out.stdout, dtype=np.int16).astype(np.float32) / 32768.0

def frame_db(x):
    n = int(FRAME * SR); m = len(x) // n
    fr = x[: m * n].reshape(m, n)
    return 20 * np.log10(np.sqrt((fr ** 2).mean(axis=1) + 1e-12) + 1e-9)

def clean_speech(tracks):
    """tracks: {speaker: waveform}. Returns {speaker: concatenated audio where that mic dominates}."""
    d = {k: frame_db(v) for k, v in tracks.items()}
    fl = {k: np.percentile(v, 20) for k, v in d.items()}
    m = min(len(v) for v in d.values())
    owner = np.full(m, "", dtype=object)
    for i in range(m):
        active = {k: d[k][i] - fl[k] for k in tracks if d[k][i] > fl[k] + FLOOR_DB}
        if not active:
            continue
        r = sorted(active.items(), key=lambda kv: -kv[1])
        if len(r) == 1 or r[0][1] - r[1][1] >= DOM_DB:
            owner[i] = r[0][0]
    n = int(FRAME * SR)
    out = {}
    for k, x in tracks.items():
        idx = np.where(owner == k)[0]
        if len(idx) == 0:
            out[k] = np.zeros(0, dtype=np.float32); continue
        # keep only runs of >= 5 frames (0.5 s)
        runs, start = [], idx[0]
        for a, b in zip(idx[:-1], idx[1:]):
            if b != a + 1:
                runs.append((start, a)); start = b
        runs.append((start, idx[-1]))
        parts = [x[s * n:(e + 1) * n] for s, e in runs if e - s + 1 >= 5]
        out[k] = np.concatenate(parts) if parts else np.zeros(0, dtype=np.float32)
    return out

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tracks", required=True)
    ap.add_argument("--out", default="voiceprints.json")
    ap.add_argument("--note", default="")
    args = ap.parse_args()

    pat = re.compile(r"^(?P<ep>.+?)\s*-\s*(?P<spk>[A-Za-z]+)\.(wav|mp3|m4a|flac|WAV|MP3)$")
    episodes = {}
    for f in sorted(os.listdir(args.tracks)):
        m = pat.match(f)
        if not m or m.group("spk").lower() == "mix":
            continue
        episodes.setdefault(m.group("ep").strip(), {})[m.group("spk")] = os.path.join(args.tracks, f)
    if not episodes:
        sys.exit("no tracks found; expected files like 'Episode Title - Phil.wav'")

    from resemblyzer import VoiceEncoder, preprocess_wav
    enc = VoiceEncoder(verbose=False)
    speech = {}
    for ep, files in episodes.items():
        if len(files) < 2:
            print("skip %s: need at least two mic tracks to separate speakers" % ep); continue
        print("episode: %s  (%s)" % (ep, ", ".join(files)))
        tracks = {k: decode(p) for k, p in files.items()}
        cs = clean_speech(tracks)
        for k, a in cs.items():
            print("   %s: %.1f min clean speech" % (k, len(a) / SR / 60))
            speech.setdefault(k, []).append(a)

    speakers, sources = {}, []
    for k, parts in speech.items():
        audio = np.concatenate(parts)
        wav = preprocess_wav(audio, source_sr=SR)
        ch = 5 * SR
        embs = [enc.embed_utterance(wav[i:i + ch]) for i in range(0, len(wav) - ch, ch)]
        if len(embs) < 12:
            print("WARNING: %s has under a minute of clean speech; voiceprint will be weak" % k)
        c = np.mean(embs, axis=0); c = c / np.linalg.norm(c)
        speakers[k] = {"embedding": [round(float(x), 6) for x in c], "minutes": round(len(audio) / SR / 60, 1)}
    out = {
        "version": datetime.date.today().strftime("%Y%m%d"),
        "created": datetime.date.today().isoformat(),
        "source": "Per-mic tracks from: " + "; ".join(sorted(episodes)) + ". " + args.note,
        "rule": {"sim_floor": 0.75, "margin": 0.05, "unknown_ceil": 0.70, "long_segment_seconds": 2.5},
        "speakers": speakers,
    }
    with open(args.out, "w") as f:
        json.dump(out, f, indent=1)
    print("wrote %s: %s" % (args.out, ", ".join("%s %.1f min" % (k, v["minutes"]) for k, v in speakers.items())))

if __name__ == "__main__":
    main()
