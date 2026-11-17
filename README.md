# SOTA Content Automation (Render webhook)

Two pipelines behind one small HTTP server (`server.py`):

- `POST /episode` — a podcast episode: audio in, three Google Docs out (full transcript with
  speaker names, Phil-only voice corpus, attribution report).
- `POST /discovery` — a Zoom discovery-call transcript in, transcript + prospect summary out.

Docs are written through the Google Apps Script webhook at `GOOGLE_SCRIPT_URL`.

## What changed in v4 (speaker attribution)

v3 compared every transcript segment to **one** short clip of Phil and called it "Phil" above a
fixed cutoff. Measured against per-mic ground truth on *Women's Strength Standards*, those labels
were 71% accurate: half of Sean's lines were labeled Phil, and 39% of the "Phil Voice Corpus" was
Sean. The fallback when matching failed was "first speaker is Phil" — and Sean opens every episode.

v4 replaces that with enrolled **voiceprints** for every regular speaker (`voiceprints.json`,
built by `enroll.py` from Zoom PodTrak P4 per-mic tracks) and a decision rule in `voiceid.py`:

| Situation | Rule |
| --- | --- |
| Segment ≥ 2.5 s | Nearest enrolled voice, only if similarity ≥ 0.75 and it beats second place by ≥ 0.05. Below 0.70 → **Unknown** (not an enrolled voice). Otherwise **Unsure**. |
| Segment < 2.5 s | Never decides alone. Takes the nearest voice only if that voice also owns a confident neighbor within 1.5 s; otherwise Unsure. |
| Pass 2 | Each voiceprint is blended 50/50 with the centroid of that speaker's confident long segments from *this* episode (room, mic, day), and undecided segments are rescored. |
| Guests | Long Unknown segments are clustered by voice; a cluster is named Guest 1, Guest 2… only if it has ≥ 20 s and no enrolled voice would accept its centroid. |
| Corpus | Only segments labeled Phil enter the voice corpus. Unsure, Unknown, and guests never do. |

Validation (2026-11-16, three PodTrak episodes, truth from comparing each mic against the others):

| Test | Result |
| --- | --- |
| Voiceprints from two episodes, scored on a third (*Realistic Fitness Expectations*), Whisper-style segments | 100% precision on Phil; 91% of Phil's words captured; one 1-second Phil fragment labeled Sean |
| Same, with Rowen deliberately **removed** from the voiceprints (*HIIT*) | Rowen clustered as Guest 1 (360 s); **zero** of her lines attributed to Phil or Sean; 100% Phil precision; 88% of Phil's words captured |
| Old v3 labels on the same utterances | 73–78% Phil-vs-not-Phil accuracy |

Precision is the design goal: a smaller corpus that is actually Phil beats a bigger one that is 39% Sean.
The attribution report's 20 sampled lines are the five-minute spot-check per episode.

## Transcripts: Spotify first, Whisper second

The podcast RSS feed (`FEED_URL`, default is the show's Anchor/Spotify feed) carries a
`podcast:transcript` SRT for every episode. `/episode` looks the episode up in the feed by audio URL
or title and uses that SRT when present; Whisper (`OPENAI_API_KEY`) is the fallback. Zapier does not
need to change: it still posts `{title, audio_url}`. It may also post `transcript_url` directly.

## Backfill

```
POST /backfill            {"dry_run": true}                      -> the plan, nothing runs
POST /backfill            {"skip_titles": ["...", "..."], "max": 10, "delay_seconds": 30}
GET  /backfill/status
```

`skip_titles` = episodes already done (paste the Drive folder names). Oldest first by default, so
the early-2025 episodes — different gear or room, if anything — are checked first. One episode at a
time; `delay_seconds` between them. Each episode produces its attribution report; spot-check the
first two batches, then one in five.

## Rebuilding voiceprints

Record every episode multitrack on the PodTrak. To add or refresh a voice:

```
mkdir tracks
# copy per-mic files in, named "<Episode Title> - <Speaker>.wav"  (Phil, Sean, Rowen, ...)
pip install -r requirements.txt
python enroll.py --tracks ./tracks --out voiceprints.json
git commit -am "voiceprints: add <speaker> from <episode>" && git push
```

Two to four minutes of clean speech per speaker is enough. Render redeploys on push.

## Environment variables

| Var | Purpose |
| --- | --- |
| `ANTHROPIC_API_KEY` | Discovery-call summaries |
| `OPENAI_API_KEY` | Whisper fallback transcription |
| `GOOGLE_SCRIPT_URL` | Apps Script webhook that creates the Docs |
| `WEBHOOK_SECRET` | Shared secret checked in `X-SOTA-Secret` |
| `HOST_NAME` | `Phil` — whose lines become the voice corpus |
| `FEED_URL` | Podcast RSS; feed transcripts and `/backfill` |
| `VOICEPRINTS_PATH` | Defaults to `voiceprints.json` beside `server.py` |
| `VOICEID_SIM_FLOOR`, `VOICEID_MARGIN`, `VOICEID_UNKNOWN_CEIL`, `VOICEID_LONG_SEG`, `VOICEID_SHORT_FLOOR`, `VOICEID_SHORT_MARGIN` | Override the decision rule without a code change |

Removed: `VOICE_REF_GDRIVE_ID` (the single reference clip). It is ignored if still set.

## Apps Script side

No change required. The webhook receives `{title, content, folder, master_folder}` as before.
A third document per episode now arrives: `[SOTA] Attribution Report - <title>`.
