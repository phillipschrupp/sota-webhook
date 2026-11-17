"""
voiceid.py - Speaker attribution for SOTA podcast audio.

Replaces the single-reference "is this Phil?" check with enrolled voiceprints
for every regular speaker (voiceprints.json; rebuild with enroll.py).

How a segment gets its label
----------------------------
Every transcript segment (Whisper segment or SRT cue) is embedded once and
compared to all enrolled voices.

  Long segments (>= LONG_SEG seconds) decide on their own:
      top similarity >= SIM_FLOOR and margin over 2nd >= MARGIN  -> that speaker
      top similarity <  UNKNOWN_CEIL                             -> Unknown (not an enrolled voice)
      otherwise                                                  -> Unsure

  Short segments (< LONG_SEG) never decide alone. Whisper cuts speech into many
  1-2 second fragments whose embeddings are noisy. A short segment takes the
  nearest voice only if that voice also owns a confident neighbor within
  NEIGHBOR_GAP seconds; otherwise Unsure. Short segments never seed a guest.

  Pass 2 adapts to the episode: each enrolled speaker's voiceprint is blended
  with the centroid of that speaker's confident long segments from this very
  episode (room, mic, and day-of effects), and the still-undecided segments are
  rescored. Guests are clustered from long Unknown segments and named only if the
  cluster is clearly nobody we know and has real talk time.

Validated 2026-11-16 on three Zoom PodTrak P4 episodes with per-mic ground truth
(see README.md for the numbers).
"""
import os, json, logging, subprocess, types
import numpy as np

log = logging.getLogger("sota.voiceid")

SR            = 16000
SIM_FLOOR     = float(os.environ.get("VOICEID_SIM_FLOOR", "0.75"))   # long segment: accept nearest above this
MARGIN        = float(os.environ.get("VOICEID_MARGIN", "0.05"))      # long segment: nearest must beat 2nd by this
UNKNOWN_CEIL  = float(os.environ.get("VOICEID_UNKNOWN_CEIL", "0.70"))# long segment: below this it is nobody we know
LONG_SEG      = float(os.environ.get("VOICEID_LONG_SEG", "2.5"))     # seconds; shorter segments need a neighbor
SHORT_FLOOR   = float(os.environ.get("VOICEID_SHORT_FLOOR", "0.60"))# short segment: minimum similarity to accept
SHORT_MARGIN  = float(os.environ.get("VOICEID_SHORT_MARGIN", "0.04"))
MIN_SEG       = 0.8                                                  # below this we do not embed at all
MAX_SEG       = 20.0                                                 # embed at most this much of a long segment
NEIGHBOR_GAP  = 1.5                                                  # seconds; neighbor must be this close
GUEST_SIM     = 0.75                                                 # join an existing guest cluster above this
GUEST_MIN_SEC = 20.0                                                 # name a guest only with this much speech
GUEST_FAR     = SIM_FLOOR                                            # and only if no enrolled voice would accept its centroid
ADAPT_MIN_SEC = 60.0                                                 # adapt a speaker only with this much confident speech
ADAPT_WEIGHT  = 0.5                                                  # blend: global voiceprint vs. episode centroid
UNKNOWN       = "Unknown"
UNSURE        = "Unsure"


class Voiceprints:
    """Enrolled speakers: names + unit-norm embeddings."""

    def __init__(self, data):
        self.version = str(data.get("version", "unknown"))
        spk          = data["speakers"]
        self.names   = list(spk.keys())
        m            = np.array([spk[n]["embedding"] for n in self.names], dtype=np.float32)
        self.matrix  = m / np.linalg.norm(m, axis=1, keepdims=True)
        self.minutes = {n: spk[n].get("minutes") for n in self.names}

    @classmethod
    def load(cls, path):
        with open(path) as f:
            return cls(json.load(f))

    def describe(self):
        return "voiceprints v%s: %s" % (
            self.version, ", ".join("%s (%.1f min)" % (n, self.minutes[n] or 0) for n in self.names))


def decode_audio(audio_path):
    """Decode any audio file once to int16 mono 16 kHz with ffmpeg, into a temp file
    that is memory-mapped. Keeping the full signal as float32 in RAM (and the ffmpeg
    stdout buffer beside it) peaked at ~500 MB for a 90-minute seminar, which is
    what killed the Render instance mid-backfill. The memmap is paged by the OS;
    only the slice being embedded is converted to float32."""
    import tempfile
    raw = tempfile.NamedTemporaryFile(suffix=".s16", delete=False)
    raw.close()
    cmd = ["ffmpeg", "-v", "error", "-y", "-i", audio_path, "-ac", "1", "-ar", str(SR),
           "-f", "s16le", "-acodec", "pcm_s16le", raw.name]
    out = subprocess.run(cmd, capture_output=True, timeout=900)
    if out.returncode != 0:
        os.unlink(raw.name)
        raise RuntimeError("ffmpeg decode failed: " + out.stderr.decode("ascii", "ignore")[:200])
    if os.path.getsize(raw.name) < 2:
        os.unlink(raw.name)
        raise RuntimeError("ffmpeg produced no audio")
    audio = np.memmap(raw.name, dtype=np.int16, mode="r")
    audio._voiceid_tmp = raw.name          # released in label_segments
    return audio


def _release_audio(audio):
    path = getattr(audio, "_voiceid_tmp", None)
    try:
        del audio
    except Exception:
        pass
    if path and os.path.exists(path):
        try:
            os.unlink(path)
        except OSError:
            pass


def _embed_all(audio, segments):
    """One embedding per segment (None when too short). Embeds each segment once."""
    from resemblyzer import VoiceEncoder, preprocess_wav
    encoder = VoiceEncoder(verbose=False)
    embs = []
    for seg in segments:
        start = max(0.0, float(seg.start))
        dur   = min(float(seg.end) - start, MAX_SEG)
        a, b  = int(start * SR), int((start + dur) * SR)
        if dur < MIN_SEG or b <= a or a >= len(audio):
            embs.append(None); continue
        chunk = np.asarray(audio[a:b], dtype=np.float32) / 32768.0
        wav = preprocess_wav(chunk, source_sr=SR)
        if len(wav) < int(0.6 * SR):
            embs.append(None); continue
        e = encoder.embed_utterance(wav)
        embs.append(e / (np.linalg.norm(e) + 1e-9))
    return embs


def _score(matrix, names, emb):
    sims   = matrix @ emb
    order  = np.argsort(-sims)
    top    = float(sims[order[0]])
    second = float(sims[order[1]]) if len(order) > 1 else 0.0
    return names[order[0]], top, top - second


def _decide(out, segments, embs, matrix, names, only_undecided):
    """Label long segments on their own; short ones by agreement with a confident neighbor."""
    n = len(segments)
    confident = set(names)
    # long segments first
    for i in range(n):
        if embs[i] is None or (only_undecided and out[i]["label"] in confident):
            continue
        dur = float(segments[i].end) - float(segments[i].start)
        nearest, top, margin = _score(matrix, names, embs[i])
        out[i].update(nearest=nearest, top=top, margin=margin)
        if dur < LONG_SEG:
            continue
        if top >= SIM_FLOOR and margin >= MARGIN:
            out[i]["label"] = nearest
        elif top < UNKNOWN_CEIL:
            out[i]["label"] = UNKNOWN
        else:
            out[i]["label"] = UNSURE
    # short segments: nearest voice must also own a confident neighbor close in time
    for i in range(n):
        if embs[i] is None or out[i]["label"] in confident:
            continue
        dur = float(segments[i].end) - float(segments[i].start)
        if dur >= LONG_SEG:
            continue
        r = out[i]
        if r["top"] < SHORT_FLOOR or r["margin"] < SHORT_MARGIN:
            r["label"] = UNSURE; continue
        agree = False
        for j in (i - 1, i + 1):
            if 0 <= j < n and out[j]["label"] == r["nearest"]:
                gap = (float(segments[i].start) - float(segments[j].end)) if j < i \
                      else (float(segments[j].start) - float(segments[i].end))
                if gap <= NEIGHBOR_GAP:
                    agree = True
        r["label"] = r["nearest"] if agree else UNSURE


def _adapt(vp, out, segments, embs):
    """Blend each voiceprint with this episode's own confident long segments."""
    matrix = vp.matrix.copy()
    adapted = []
    for k, name in enumerate(vp.names):
        idx = [i for i, r in enumerate(out)
               if r["label"] == name and embs[i] is not None
               and float(segments[i].end) - float(segments[i].start) >= LONG_SEG]
        secs = sum(float(segments[i].end) - float(segments[i].start) for i in idx)
        if secs < ADAPT_MIN_SEC:
            continue
        c = np.mean([embs[i] for i in idx], axis=0); c /= np.linalg.norm(c)
        m = ADAPT_WEIGHT * vp.matrix[k] + (1 - ADAPT_WEIGHT) * c
        matrix[k] = m / np.linalg.norm(m)
        adapted.append("%s (%.0fs)" % (name, secs))
    if adapted:
        log.info("voiceid: adapted to episode: %s", ", ".join(adapted))
    return matrix


def _cluster_guests(out, segments, embs, matrix):
    """Cluster long Unknown segments into Guest 1, Guest 2, ...; name only clear, substantial clusters."""
    cands = [i for i, r in enumerate(out)
             if r["label"] == UNKNOWN and embs[i] is not None
             and float(segments[i].end) - float(segments[i].start) >= LONG_SEG]
    clusters = []
    for i in cands:
        emb = embs[i]
        best, best_sim = None, -1.0
        for c in clusters:
            s = float(np.dot(c["centroid"], emb))
            if s > best_sim:
                best, best_sim = c, s
        dur = float(segments[i].end) - float(segments[i].start)
        if best is not None and best_sim >= GUEST_SIM:
            best["sum"] += emb; best["members"].append(i); best["seconds"] += dur
            best["centroid"] = best["sum"] / np.linalg.norm(best["sum"])
        else:
            clusters.append(dict(sum=emb.copy(), centroid=emb.copy(), members=[i], seconds=dur))
    clusters.sort(key=lambda c: -c["seconds"])
    named = []
    for c in clusters:
        if c["seconds"] < GUEST_MIN_SEC:
            continue
        if float(np.max(matrix @ c["centroid"])) >= GUEST_FAR:
            continue   # too close to someone we know; leave as Unknown rather than invent a guest
        named.append(c)
    for k, c in enumerate(named, 1):
        for i in c["members"]:
            out[i]["label"] = "Guest %d" % k
        # short Unknown/Unsure segments adjacent to this guest's segments inherit the guest label
    if named:
        log.info("voiceid: %d guest voice(s): %s", len(named),
                 ", ".join("Guest %d %.0fs" % (k, c["seconds"]) for k, c in enumerate(named, 1)))
    return [c["centroid"] for c in named]


def _smooth(out, segments, label_set):
    """Unsure or unlabeled segments between two identical confident labels inherit that label."""
    n = len(out)
    for i in range(1, n - 1):
        if out[i]["label"] not in (None, UNSURE):
            continue
        prev, nxt = out[i - 1]["label"], out[i + 1]["label"]
        if prev is None or prev != nxt or prev not in label_set:
            continue
        if (float(segments[i].start) - float(segments[i - 1].end) <= NEIGHBOR_GAP and
                float(segments[i + 1].start) - float(segments[i].end) <= NEIGHBOR_GAP):
            out[i]["label"] = prev
            out[i]["smoothed"] = True


def label_segments(audio_path, segments, vp):
    """
    segments: objects with .start, .end (seconds) and .text.
    Returns a list (same length) of dicts: label, top, margin, nearest, smoothed.
    label is a speaker name | "Guest N" | "Unknown" | "Unsure" | None (too short to embed).
    """
    audio = decode_audio(audio_path)
    try:
        embs = _embed_all(audio, segments)
    finally:
        _release_audio(audio)
    out   = [dict(label=None, top=0.0, margin=0.0, nearest=None, smoothed=False) for _ in segments]

    # pass 1: global voiceprints
    _decide(out, segments, embs, vp.matrix, vp.names, only_undecided=False)
    # pass 2: adapt to this episode, rescore what is still undecided
    matrix = _adapt(vp, out, segments, embs)
    _decide(out, segments, embs, matrix, vp.names, only_undecided=True)
    # guests, then smoothing over everything confident (speakers and named guests)
    guest_cents = _cluster_guests(out, segments, embs, matrix)
    label_set = set(vp.names) | {"Guest %d" % k for k in range(1, len(guest_cents) + 1)}
    _smooth(out, segments, label_set)

    counts = {}
    for r in out:
        counts[str(r["label"])] = counts.get(str(r["label"]), 0) + 1
    log.info("voiceid labels: %s", counts)
    return out


def talk_time(segments, labels):
    """Seconds of speech per label."""
    t = {}
    for seg, r in zip(segments, labels):
        lab = r["label"] or "(unlabeled)"
        t[lab] = t.get(lab, 0.0) + max(0.0, float(seg.end) - float(seg.start))
    return t


def all_unsure(segments):
    """Fallback when no voiceprints are available: never guess, mark everything Unsure."""
    return [dict(label=UNSURE, top=0.0, margin=0.0, nearest=None, smoothed=False) for _ in segments]


def parse_srt_segments(text):
    """Parse SRT text into segment objects (.start, .end in seconds, .text)."""
    def ts(s):
        s = s.strip().replace(",", ".")
        parts = s.split(":")
        if len(parts) == 3:
            h, m, sec = parts
            return int(h) * 3600 + int(m) * 60 + float(sec)
        m, sec = parts
        return int(m) * 60 + float(sec)

    segs, cur_start, cur_end, buf = [], None, None, []
    for raw in text.replace("\r", "").splitlines() + [""]:
        line = raw.strip()
        if "-->" in line:
            a, b = line.split("-->")[:2]
            cur_start, cur_end = ts(a), ts(b.split()[0]); buf = []
        elif line == "":
            if cur_start is not None and buf:
                segs.append(types.SimpleNamespace(start=cur_start, end=cur_end, text=" ".join(buf)))
            cur_start, cur_end, buf = None, None, []
        elif line.isdigit() and cur_start is None:
            continue
        elif cur_start is not None:
            buf.append(line)
    return segs
