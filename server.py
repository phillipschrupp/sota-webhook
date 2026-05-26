"""
SOTA Content Automation - Webhook Server v3
===========================================
Two independent pipelines:

1. POST /episode  - Podcast episodes (Spotify RSS via Zapier)
   - Downloads audio
   - Whisper transcription (OpenAI)
   - resemblyzer voice matching against Phil voice reference clip
     (downloaded from Google Drive on startup)
   - Extracts Phil-only lines into a voice corpus doc
   - Generates 5 content pieces + raw transcript in Google Drive

2. POST /discovery - Discovery calls (Zoom VTT/SRT via Zapier)
   - Parses Zoom transcript (speaker names already embedded)
   - Generates transcript + consolidated Prospect Summary

Deploy on Render:
  Build command: pip install -r requirements.txt
  Start command: python server.py
  Env vars: ANTHROPIC_API_KEY, OPENAI_API_KEY, GOOGLE_SCRIPT_URL,
            WEBHOOK_SECRET, HOST_NAME (default: Phil),
            VOICE_REF_GDRIVE_ID (Google Drive file ID of phil_voice.wav)
"""

import os, json, hashlib, hmac, tempfile, threading, logging, subprocess, time, datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.request import Request, urlopen
import anthropic
import openai

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("sota")

# -- Config --
ANTHROPIC_API_KEY   = os.environ.get("ANTHROPIC_API_KEY", "")
OPENAI_API_KEY      = os.environ.get("OPENAI_API_KEY", "")
GOOGLE_SCRIPT_URL   = os.environ.get("GOOGLE_SCRIPT_URL", "")
WEBHOOK_SECRET      = os.environ.get("WEBHOOK_SECRET", "")
PORT                = int(os.environ.get("PORT", 8080))
HOST_NAME           = os.environ.get("HOST_NAME", "Phil")
WHISPER_LIMIT       = 24 * 1024 * 1024

# Google Drive file ID from the share link of phil_voice.wav
# Share link: https://drive.google.com/file/d/FILE_ID/view -> use FILE_ID
VOICE_REF_GDRIVE_ID = os.environ.get("VOICE_REF_GDRIVE_ID", "")
VOICE_REF_PATH      = "/tmp/phil_voice_ref.wav"

PODCAST_FOLDER      = "Podcast Transcripts"
DISCOVERY_FOLDER    = "Discovery Calls"

#  Brand system prompt 
BRAND_SYSTEM = (
    "You are the content strategist for SOTA Personal Training, a boutique "
    "personal training gym in Minnetonka, Minnesota. SOTA coaches busy adults 40+ "
    "using a psychology-forward approach to nutrition and fitness.\n\n"
    "Brand voice: straight-talking, warm, systems-minded, community-rooted.\n"
    "Tagline: Strength for Life.\n\n"
    "Founder Phil recovered from serious spinal and shoulder injuries through strength "
    "training. SOTA believes fitness is not punishment - it is a path to reclaiming "
    "your life.\n\n"
    "Rules:\n"
    "- Never mention the podcast or episode\n"
    "- Write as a standalone article, post, or email\n"
    "- Use 'you' freely. Short sentences. No generic fitness cliches.\n"
    "- Be specific, warm, direct - like a knowledgeable friend."
)

#  Content prompts (5 pieces) 

def p_summary_quotes(t, title):
    return (
        "Read this podcast transcript and produce an Episode Summary with Pull Quotes "
        "for SOTA Personal Training.\n\n"
        "CRITICAL FORMATTING RULES - follow exactly:\n"
        "- No em dashes (use a comma or semicolon instead)\n"
        "- No asterisks, no markdown symbols, no bold markers\n"
        "- No quotation marks unless directly citing someone\n"
        "- No bullet point dashes or hyphens at the start of lines\n"
        "- Numbered lists only (1. 2. 3.)\n"
        "- One blank line between every text block\n\n"
        "Structure EXACTLY as:\n\n"
        "Episode Summary: " + title + "\n\n"
        "Core Insight\n\n"
        "[2-3 sentences capturing the single most valuable idea. "
        "No em dashes. Use commas or semicolons instead.]\n\n"
        "Key Takeaways\n\n"
        "1. [First takeaway - bold label removed, plain sentence only]\n\n"
        "2. [Second takeaway]\n\n"
        "3. [Third takeaway]\n\n"
        "4. [Fourth takeaway]\n\n"
        "5. [Fifth takeaway if applicable]\n\n"
        "Pull Quotes\n\n"
        "1. [First pull quote - under 25 words, no quotation marks around it]\n\n"
        "2. [Second pull quote]\n\n"
        "3. [Third pull quote]\n\n"
        "4. [Fourth pull quote]\n\n"
        "5. [Fifth pull quote]\n\n"
        "Do NOT mention podcast, episode number, or guests by name.\n\n"
        "TRANSCRIPT:\n" + t[:4000]
    )


def p_blog(t, title):
    return (
        "Write a full blog post for SOTA Personal Training. 700-1000 words.\n\n"
        "CRITICAL FORMATTING RULES - follow exactly:\n"
        "- No em dashes anywhere. Use commas, semicolons, or periods instead.\n"
        "- No asterisks, no markdown bold markers, no special characters\n"
        "- No quotation marks unless directly citing someone\n"
        "- No bullet dashes or hyphens at line starts\n"
        "- One blank line between every paragraph and section\n"
        "- Do NOT mention podcast, episode, or as discussed\n"
        "- Standalone educational article written by a SOTA coach\n"
        "- Audience: busy adults 40+ in Minnetonka / Twin Cities area\n\n"
        "FORMAT:\n"
        "[Title: specific, benefit-driven. Not a question. Plain text, no # symbol.]\n\n"
        "[1-2 sentence hook. Relatable frustration or counterintuitive truth.]\n\n"
        "[Section Heading - plain text, no ### symbol]\n\n"
        "[2-4 short paragraphs. Short sentences. Concrete details.]\n\n"
        "[3-5 total sections. Each section heading on its own line.]\n\n"
        "The Bottom Line\n\n"
        "[1-2 tight sentences that land the core message.]\n\n"
        "Ready to build strength that lasts? Book a free strategy session at "
        "https://www.sotafitness.com/contact\n\n"
        "VOICE: Use you freely. No bold markers. No generic motivational quotes. "
        "Real numbers, real scenarios, real feelings.\n\n"
        "SOURCE CONTENT (use the ideas - do not reference as transcript):\n" + t[:4000]
    )


def p_email(t, title):
    return (
        "Write a single high-value marketing email for SOTA Personal Training.\n\n"
        "PURPOSE: Convert non-members on the email list. "
        "This email must earn attention, deliver a genuine insight, and make "
        "booking a free consultation feel like the obvious next step, not a sales pitch.\n\n"
        "AUDIENCE: Busy adults 40+ who are on the SOTA email list but have not yet joined. "
        "They are skeptical, time-poor, and have probably tried other fitness programs before.\n\n"
        "CRITICAL FORMATTING RULES - follow exactly:\n"
        "- No em dashes anywhere. Use commas, semicolons, or periods instead.\n"
        "- No asterisks, no markdown symbols, no bold markers\n"
        "- No quotation marks unless directly citing someone\n"
        "- One blank line between every paragraph\n"
        "- No exclamation marks\n"
        "- No transform, journey, crush, grind, hustle\n"
        "- Total length: 200-280 words\n"
        "- Do NOT mention podcast or episode\n\n"
        "FORMAT:\n"
        "Subject: [Compelling subject line - specific, curiosity-driven, no hype words]\n\n"
        "Preview: [40-char preview text]\n\n"
        "[Opening hook - 1-2 sentences. Specific situation that makes the reader feel seen.]\n\n"
        "[Core insight - 3-4 short paragraphs. Each paragraph separated by blank line.]\n\n"
        "[Transition - 1-2 sentences connecting insight to invitation.]\n\n"
        "[CTA - One sentence. Warm, direct. Link: https://www.sotafitness.com/contact]\n\n"
        "Phil\n\n"
        "SOURCE CONTENT:\n" + t[:3500]
    )


def p_instagram(t, title):
    return (
        "Write a complete Instagram content package for SOTA Personal Training "
        "based on this content. Topic: " + title + ".\n\n"
        "CRITICAL FORMATTING RULES - follow exactly:\n"
        "- No em dashes anywhere. Use commas, semicolons, or periods instead.\n"
        "- No asterisks, no markdown symbols, no special characters\n"
        "- No quotation marks unless directly citing someone\n"
        "- One blank line between every slide and every paragraph\n"
        "- Do NOT mention podcast or episode\n\n"
        "Produce TWO sections in a single document:\n\n"
        "CAROUSEL COPY\n\n"
        "Write 5 slides for an Instagram carousel.\n\n"
        "Slide 1\n"
        "[Hook - scroll-stopping claim or question. Max 12 words.]\n"
        "[2-3 sentences expanding the hook.]\n\n"
        "Slide 2\n"
        "[Slide title]\n"
        "[2-4 short lines of content]\n\n"
        "Slide 3\n"
        "[Slide title]\n"
        "[2-4 short lines of content]\n\n"
        "Slide 4\n"
        "[Slide title]\n"
        "[2-4 short lines of content]\n\n"
        "Slide 5\n"
        "Ready to build strength that fits your actual life?\n"
        "Book a free strategy session at sotafitness.com\n\n"
        "Each slide works as a standalone idea. No jargon. "
        "Write for a 47-year-old professional, not a 22-year-old athlete.\n\n"
        "POST CAPTION\n\n"
        "Write a caption for the carousel post or a standalone Reel.\n\n"
        "First line: max 8 words, must stop the scroll.\n"
        "150-220 words total.\n"
        "Generous line breaks for mobile reading.\n"
        "Second-to-last paragraph: soft CTA to book free consult at sotafitness.com\n"
        "Last line: 3-4 niche hashtags (#strengthafter40 #fitafter40 style)\n\n"
        "SOURCE CONTENT:\n" + t[:3000]
    )


PROMPTS = {
    "summary_quotes": p_summary_quotes,
    "blog_post":      p_blog,
    "email":          p_email,
    "instagram":      p_instagram,
}

PODCAST_PIECES = [
    ("summary_quotes", "Episode Summary and Pull Quotes"),
    ("blog_post",      "Blog Post"),
    ("email",          "Marketing Email"),
    ("instagram",      "Instagram Carousel and Caption"),
]

#  Utility 
def clean(text):
    return text.encode("ascii", "ignore").decode("ascii")


#  Audio download + compression 
def download_audio(audio_url):
    log.info("Downloading audio: %s", audio_url[:80])
    req = Request(audio_url, headers={"User-Agent": "Mozilla/5.0 (compatible; SOTA-Bot/1.0)"})
    with urlopen(req, timeout=120) as resp:
        audio_data = resp.read()
    log.info("Downloaded %.1f MB", len(audio_data) / 1024 / 1024)
    return audio_data


def compress_audio(input_path):
    output_path = input_path + "_c.mp3"
    cmd = ["ffmpeg", "-y", "-i", input_path,
           "-ac", "1", "-ar", "16000", "-b:a", "32k", "-map", "0:a", output_path]
    result = subprocess.run(cmd, capture_output=True, timeout=120)
    if result.returncode != 0:
        raise RuntimeError("ffmpeg failed: " + result.stderr.decode("ascii", "ignore")[:200])
    log.info("Compressed to %.1f MB", os.path.getsize(output_path) / 1024 / 1024)
    return output_path


def save_audio_temp(audio_data, audio_url):
    suffix = ".m4a" if audio_url.lower().endswith(".m4a") else (
             ".wav" if audio_url.lower().endswith(".wav") else ".mp3")
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(audio_data)
        return tmp.name


#  Voice identification with resemblyzer 
def identify_host_speaker(audio_path, speaker_labels, utterances):
    """
    Use resemblyzer to compare each speaker's audio segments against
    Phil's reference voice clip. Returns the speaker label that best
    matches Phil's voice.
    Falls back to first-speaker heuristic if resemblyzer unavailable
    or reference clip not found.
    """
    # Check prerequisites
    if not os.path.exists(VOICE_REF_PATH):
        log.warning("Voice reference clip not found at %s - using first-speaker fallback", VOICE_REF_PATH)
        return _first_speaker_fallback(utterances)

    try:
        from resemblyzer import VoiceEncoder, preprocess_wav
        from pathlib import Path
        import numpy as np

        log.info("Loading resemblyzer encoder...")
        encoder = VoiceEncoder()

        # Embed Phil's reference voice
        ref_wav = preprocess_wav(Path(VOICE_REF_PATH))
        ref_embed = encoder.embed_utterance(ref_wav)
        log.info("Reference voice embedded.")

        # For each speaker, extract a sample segment and embed it
        # We use the longest utterance per speaker for best accuracy
        best_match   = None
        best_score   = -1.0

        for label in speaker_labels:
            # Find the longest utterance for this speaker
            speaker_utts = [u for u in utterances if u.get("speaker") == label]
            if not speaker_utts:
                continue

            longest = max(speaker_utts, key=lambda u: u.get("end", 0) - u.get("start", 0))
            start_ms = longest.get("start", 0)
            end_ms   = longest.get("end", start_ms + 10000)

            # Extract that segment with ffmpeg
            seg_path = audio_path + "_spk_" + label + ".wav"
            start_s  = start_ms / 1000.0
            dur_s    = min((end_ms - start_ms) / 1000.0, 30.0)  # cap at 30s

            cmd = [
                "ffmpeg", "-y", "-i", audio_path,
                "-ss", str(start_s), "-t", str(dur_s),
                "-ac", "1", "-ar", "16000",
                seg_path
            ]
            result = subprocess.run(cmd, capture_output=True, timeout=60)
            if result.returncode != 0:
                log.warning("ffmpeg segment extract failed for speaker %s", label)
                continue

            try:
                seg_wav   = preprocess_wav(Path(seg_path))
                seg_embed = encoder.embed_utterance(seg_wav)
                # Cosine similarity
                score = float(np.dot(ref_embed, seg_embed) /
                              (np.linalg.norm(ref_embed) * np.linalg.norm(seg_embed)))
                log.info("Speaker %s similarity to Phil: %.3f", label, score)
                if score > best_score:
                    best_score = score
                    best_match = label
            finally:
                if os.path.exists(seg_path):
                    os.unlink(seg_path)

        if best_match and best_score > 0.75:
            log.info("Voice match: %s identified as %s (score: %.3f)", best_match, HOST_NAME, best_score)
            return best_match
        else:
            log.warning("No confident voice match (best: %.3f) - using first-speaker fallback", best_score)
            return _first_speaker_fallback(utterances)

    except ImportError:
        log.warning("resemblyzer not installed - using first-speaker fallback")
        return _first_speaker_fallback(utterances)
    except Exception as e:
        log.warning("Voice identification failed (%s) - using first-speaker fallback", e)
        return _first_speaker_fallback(utterances)


def _first_speaker_fallback(utterances):
    """Return the speaker label that appears first in the transcript."""
    if not utterances:
        return "A"
    first = min(utterances, key=lambda u: u.get("start", 0))
    label = first.get("speaker", "A")
    log.info("First-speaker fallback: %s -> %s", label, HOST_NAME)
    return label


# -- Startup: download Phil voice reference from Google Drive --
def download_voice_reference():
    """Download Phil voice reference clip from Google Drive on server startup."""
    if not VOICE_REF_GDRIVE_ID:
        log.warning("VOICE_REF_GDRIVE_ID not set - voice matching unavailable")
        return False
    if os.path.exists(VOICE_REF_PATH):
        log.info("Voice reference already cached at %s", VOICE_REF_PATH)
        return True
    try:
        url = "https://drive.google.com/uc?export=download&id=" + VOICE_REF_GDRIVE_ID
        log.info("Downloading voice reference from Google Drive...")
        req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urlopen(req, timeout=30) as resp:
            data = resp.read()
        with open(VOICE_REF_PATH, "wb") as f:
            f.write(data)
        log.info("Voice reference saved: %d bytes", len(data))
        return True
    except Exception as e:
        log.error("Failed to download voice reference: %s", e)
        return False


# -- Whisper transcription --
def transcribe_whisper(audio_path):
    """Transcribe audio using OpenAI Whisper with verbose JSON for segment timestamps."""
    log.info("Sending to Whisper...")
    client = openai.OpenAI(api_key=OPENAI_API_KEY)
    with open(audio_path, "rb") as f:
        result = client.audio.transcriptions.create(
            model="whisper-1",
            file=f,
            response_format="verbose_json",
            timestamp_granularities=["segment"]
        )
    segments = result.segments if hasattr(result, "segments") else []
    plain = " ".join(seg.text.strip() for seg in segments)
    log.info("Whisper complete - %d chars, %d segments", len(plain), len(segments))
    return plain, segments


# -- Voice identification with resemblyzer --
def identify_host_segments(audio_path, segments):
    """
    Use resemblyzer to compare each Whisper segment against Phil voice reference.
    Returns a list of booleans: True = this segment is Phil speaking.
    Falls back to first-speaker heuristic if resemblyzer unavailable.
    """
    if not os.path.exists(VOICE_REF_PATH):
        log.warning("Voice reference not found - using first-speaker fallback")
        return _first_speaker_fallback_segments(segments)

    try:
        from resemblyzer import VoiceEncoder, preprocess_wav
        from pathlib import Path
        import numpy as np

        log.info("Loading resemblyzer encoder...")
        encoder   = VoiceEncoder()
        ref_wav   = preprocess_wav(Path(VOICE_REF_PATH))
        ref_embed = encoder.embed_utterance(ref_wav)
        log.info("Reference voice embedded.")

        is_host = []
        for seg in segments:
            start_s = seg.start
            dur_s   = min(seg.end - seg.start, 20.0)
            if dur_s < 1.0:
                is_host.append(None)
                continue

            seg_path = audio_path + "_seg.wav"
            cmd = ["ffmpeg", "-y", "-i", audio_path,
                   "-ss", str(start_s), "-t", str(dur_s),
                   "-ac", "1", "-ar", "16000", seg_path]
            result = subprocess.run(cmd, capture_output=True, timeout=30)
            if result.returncode != 0:
                is_host.append(None)
                continue

            try:
                seg_wav   = preprocess_wav(Path(seg_path))
                seg_embed = encoder.embed_utterance(seg_wav)
                score = float(np.dot(ref_embed, seg_embed) /
                              (np.linalg.norm(ref_embed) * np.linalg.norm(seg_embed)))
                is_host.append(score > 0.72)
            except Exception:
                is_host.append(None)
            finally:
                if os.path.exists(seg_path):
                    os.unlink(seg_path)

        host_count  = sum(1 for v in is_host if v is True)
        guest_count = sum(1 for v in is_host if v is False)
        log.info("Voice ID complete: %d host segments, %d guest segments, %d unclear",
                 host_count, guest_count, sum(1 for v in is_host if v is None))
        return is_host

    except ImportError:
        log.warning("resemblyzer not installed - using first-speaker fallback")
        return _first_speaker_fallback_segments(segments)
    except Exception as e:
        log.warning("Voice ID failed (%s) - using first-speaker fallback", e)
        return _first_speaker_fallback_segments(segments)


def _first_speaker_fallback_segments(segments):
    """
    Fallback: alternate host/guest based on natural pause gaps.
    First speaker = Phil. Speaker changes on pauses > 1.5s.
    """
    if not segments:
        return []
    is_host     = []
    current     = True
    prev_end    = 0.0
    PAUSE_THRESH = 1.5
    for seg in segments:
        if seg.start - prev_end > PAUSE_THRESH and prev_end > 0:
            current = not current
        is_host.append(current)
        prev_end = seg.end
    return is_host


def build_formatted_transcript(segments, is_host):
    """Build full formatted transcript with Phil / Guest labels and timestamps."""
    lines = []
    for seg, host in zip(segments, is_host):
        text = seg.text.strip()
        if not text:
            continue
        mins = int(seg.start // 60)
        secs = int(seg.start % 60)
        if host is True:
            label = "**" + HOST_NAME + "** [%02d:%02d]" % (mins, secs)
        elif host is False:
            label = "**Guest** [%02d:%02d]" % (mins, secs)
        else:
            label = "[%02d:%02d]" % (mins, secs)
        lines.append(label + "\n" + text)
    return "\n\n".join(lines)


def build_phil_corpus(segments, is_host, episode_title):
    """
    Extract only Phil's lines, clean them into flowing paragraphs.
    Strips timestamps. Groups consecutive Phil segments into paragraphs.
    This becomes the voice training corpus doc.
    """
    phil_chunks   = []
    current_chunk = []

    for seg, host in zip(segments, is_host):
        text = seg.text.strip()
        if not text:
            continue
        if host is True:
            current_chunk.append(text)
        else:
            if current_chunk:
                phil_chunks.append(" ".join(current_chunk))
                current_chunk = []

    if current_chunk:
        phil_chunks.append(" ".join(current_chunk))

    if not phil_chunks:
        return None

    header  = "# " + HOST_NAME + " Voice Corpus: " + episode_title + "\n\n"
    header += "## About This Document\n\n"
    header += ("This document contains only " + HOST_NAME + "'s spoken words from this "
               "episode, cleaned into flowing paragraphs without timestamps or guest "
               "dialogue. It is used to train Claude to write in " + HOST_NAME +
               "'s voice and tone.\n\n")
    header += "---\n\n"
    body = "\n\n".join(phil_chunks)
    return header + body


# -- Content generation --
def generate_content(piece_name, transcript, episode_title):
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    fn = PROMPTS.get(piece_name)
    if not fn:
        raise ValueError("Unknown piece: " + piece_name)
    log.info("Generating %s...", piece_name)
    msg = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=2000,
        system=BRAND_SYSTEM,
        messages=[{"role": "user", "content": fn(transcript, episode_title)}],
    )
    return msg.content[0].text.strip()


#  Google Doc creation 
def create_google_doc(title, content, folder, master_folder=None):
    payload = json.dumps(
        {"title": title, "content": content,
         "folder": folder, "master_folder": master_folder or ""},
        ensure_ascii=False
    ).encode("utf-8")
    req = Request(GOOGLE_SCRIPT_URL, data=payload,
                  headers={"Content-Type": "application/json; charset=utf-8"})
    try:
        with urlopen(req, timeout=20) as resp:
            resp.read()
        log.info("Doc created: %s", title)
        return True
    except Exception as e:
        log.error("Doc creation failed for '%s': %s", title, e)
        return False


#  Podcast pipeline 
def run_podcast_pipeline(episode_title, audio_url):
    episode_title = clean(episode_title)
    log.info("Podcast pipeline start - '%s'", episode_title)
    safe_title      = episode_title[:60].strip()
    results         = {"episode": episode_title, "docs": [], "errors": []}
    audio_path      = None
    compressed_path = None

    try:
        # Download audio
        audio_data  = download_audio(audio_url)
        audio_path  = save_audio_temp(audio_data, audio_url)

        # Compress if over Whisper 25MB limit
        if len(audio_data) > WHISPER_LIMIT:
            log.info("Compressing audio...")
            compressed_path = compress_audio(audio_path)
            process_path = compressed_path
        else:
            process_path = audio_path

        # Transcribe with Whisper
        plain, segments = transcribe_whisper(process_path)
        plain = clean(plain)

        # Identify which segments are Phil using resemblyzer
        is_host = identify_host_segments(process_path, segments)

        # Build full formatted transcript (Phil + Guest labeled)
        formatted = clean(build_formatted_transcript(segments, is_host))

        # Build Phil-only corpus (flowing paragraphs, no timestamps)
        corpus = build_phil_corpus(segments, is_host, episode_title)
        if corpus:
            corpus = clean(corpus)

    except Exception as e:
        log.error("Audio processing failed: %s", e)
        results["errors"].append("Audio processing: " + str(e))
        return results
    finally:
        if audio_path and os.path.exists(audio_path):
            os.unlink(audio_path)
        if compressed_path and os.path.exists(compressed_path):
            os.unlink(compressed_path)

    # 1. Full transcript doc
    transcript_title = "[SOTA] Transcript - " + safe_title
    t_header  = "# " + episode_title + "\n\n"
    t_header += "## Full Episode Transcript\n\n"
    t_header += "Speaker labels by resemblyzer voice matching.\n\n"
    t_header += "---\n\n"
    ok = create_google_doc(transcript_title, t_header + formatted,
                           safe_title, master_folder=PODCAST_FOLDER)
    if ok:
        results["docs"].append(transcript_title)
    else:
        results["errors"].append("Transcript: doc creation failed")

    # 2. Phil voice corpus doc
    if corpus:
        corpus_title = "[SOTA] " + HOST_NAME + " Voice Corpus - " + safe_title
        ok = create_google_doc(corpus_title, corpus,
                               safe_title, master_folder=PODCAST_FOLDER)
        if ok:
            results["docs"].append(corpus_title)
        else:
            results["errors"].append("Voice corpus: doc creation failed")

    # 3-6. Content pieces
    for piece_key, piece_label in PODCAST_PIECES:
        try:
            piece_content = clean(generate_content(piece_key, plain, episode_title))
            doc_title     = "[SOTA] " + piece_label + " - " + safe_title
            ok = create_google_doc(doc_title, piece_content,
                                   safe_title, master_folder=PODCAST_FOLDER)
            if ok:
                results["docs"].append(doc_title)
            else:
                results["errors"].append(piece_label + ": doc creation failed")
        except Exception as e:
            log.error("Error on %s: %s", piece_key, e)
            results["errors"].append(piece_label + ": " + str(e))

    log.info("Podcast pipeline done - %d docs, %d errors",
             len(results["docs"]), len(results["errors"]))
    return results


#  Transcript parsers (Discovery calls) 
def _parse_blocks(lines):
    plain_lines = []
    formatted_lines = []
    current_speaker = None
    current_text    = []
    current_time    = None
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if "-->" in line:
            if current_text:
                block_text = " ".join(current_text).strip()
                if block_text:
                    plain_lines.append(block_text)
                    label = ("**" + current_speaker + "** " + current_time + "\n") \
                            if current_speaker else (current_time + " ")
                    formatted_lines.append(label + block_text)
                current_text = []
            ts = line.split("-->")[0].strip().replace(",", ".")
            parts = ts.split(":")
            try:
                if len(parts) == 3:
                    h, m, s = parts
                    total_secs = int(h)*3600 + int(m)*60 + float(s)
                else:
                    m, s = parts
                    total_secs = int(m)*60 + float(s)
                current_time = "[%02d:%02d]" % (int(total_secs//60), int(total_secs%60))
            except Exception:
                current_time = ""
            i += 1
            continue
        if line.isdigit() or not line:
            i += 1
            continue
        if ":" in line and len(line.split(":")[0]) < 40:
            spk = line.split(":")[0].strip()
            if "-->" not in spk and not spk.isdigit() and spk.upper() != "WEBVTT":
                current_speaker = spk
                line = ":".join(line.split(":")[1:]).strip()
        if line:
            current_text.append(line)
        i += 1
    if current_text:
        block_text = " ".join(current_text).strip()
        if block_text:
            plain_lines.append(block_text)
            label = ("**" + current_speaker + "** " + current_time + "\n") \
                    if current_speaker else (current_time + " ")
            formatted_lines.append(label + block_text)
    return " ".join(plain_lines), "\n\n".join(formatted_lines)


def parse_srt(t):
    return _parse_blocks(t.strip().splitlines())

def parse_vtt(t):
    lines = [l for l in t.strip().splitlines() if l.strip() != "WEBVTT"]
    return _parse_blocks(lines)

def parse_transcript(text):
    text = text.strip()
    if "WEBVTT" in text[:50]:
        return parse_vtt(text)
    elif "-->" in text:
        first_ts = [l for l in text.splitlines() if "-->" in l]
        if first_ts and "," in first_ts[0].split("-->")[0]:
            return parse_srt(text)
        return parse_vtt(text)
    return text, text


#  Discovery call pipeline 
DISCOVERY_SYSTEM = (
    "You are a senior coach analyst for SOTA Personal Training, a boutique "
    "personal training gym in Minnetonka, Minnesota specializing in adults 40+.\n\n"
    "Your job is to read discovery call transcripts and extract structured, "
    "actionable information that helps Phil and the SOTA coaching team "
    "understand the prospect and craft the right approach.\n\n"
    "Be specific and direct. Pull exact quotes where relevant. "
    "Flag anything that signals readiness, hesitation, or a strong fit."
)


def dc_extract_name(t):
    return (
        "Read the first part of this discovery call transcript and extract the "
        "prospect's first and last name.\n\n"
        "Look for how Phil greets them at the start of the call. "
        "Examples: 'Hey Sarah', 'Thanks for joining Jane', 'Great to meet you John Smith'.\n\n"
        "Reply with ONLY the prospect's name - nothing else. "
        "If you cannot find a name, reply with exactly: Unknown.\n\n"
        "TRANSCRIPT (first 1500 chars):\n" + t[:1500]
    )


def dc_full_summary(t, name):
    return (
        "Read this discovery call transcript and produce a complete Prospect Summary "
        "document for " + name + " at SOTA Personal Training.\n\n"
        "Use this structure exactly:\n\n"
        "# Prospect Summary: " + name + "\n\n"
        "## Overview\n"
        "- Name and personal context (job, family, lifestyle)\n"
        "- Why they reached out and what prompted the call\n"
        "- Fitness history in brief\n"
        "- Readiness to commit: High / Medium / Low (one sentence reasoning)\n"
        "- Key quote that reveals their mindset\n\n"
        "## Goals\n\n"
        "### Primary Goal\n"
        "[Single biggest stated goal]\n\n"
        "### Secondary Goals\n"
        "[2-4 additional goals]\n\n"
        "### Deeper Why\n"
        "[Emotional or life reason. Pull quotes where possible.]\n\n"
        "### Timeline Expectations\n"
        "[What timeframe? Realistic or not?]\n\n"
        "## Pain Points\n\n"
        "### Primary Pain Point\n"
        "[Biggest frustration or obstacle]\n\n"
        "### Other Pain Points\n"
        "[Additional frustrations]\n\n"
        "### Previous Attempts\n"
        "[What have they tried? What worked, what did not, why did they stop?]\n\n"
        "### Hidden Objections\n"
        "[Hesitations hinted at - price, time, skepticism, fear of injury, past failure]\n\n"
        "## Injury and Health History\n\n"
        "### Known Injuries or Conditions\n"
        "[Each one with details - severity, current status, duration]\n\n"
        "### Movement Limitations\n"
        "[Exercises or positions they avoid or struggle with]\n\n"
        "### Medical Clearance\n"
        "[Doctors, physios, surgeries, medications, pending medical things]\n\n"
        "### Coaching Flags\n"
        "[Things to assess in person before programming]\n\n"
        "## Proposal Outline\n\n"
        "### Recommended Program\n"
        "[1-on-1, small group, or online? Why?]\n\n"
        "### Suggested Starting Point\n"
        "[Phase, frequency, and focus given their history and goals]\n\n"
        "### Key Coaching Priorities (First 90 Days)\n"
        "[2-4 specific priorities from this call]\n\n"
        "### How to Frame the Value\n"
        "[Most compelling way to present SOTA to this specific person. "
        "What language resonates with them?]\n\n"
        "### Suggested Next Step\n"
        "[What should Phil say or send to move this prospect forward?]\n\n"
        "If nothing was mentioned in a category, write: None disclosed.\n"
        "Pull direct quotes where they add weight.\n\n"
        "TRANSCRIPT:\n" + t[:5000]
    )


def extract_prospect_name(transcript):
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    msg = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=20,
        system="You extract names from text. Reply with only the name or Unknown.",
        messages=[{"role": "user", "content": dc_extract_name(transcript)}],
    )
    name = msg.content[0].text.strip()
    if not name or name.lower() == "unknown" or len(name) > 50:
        return None
    return name


def run_discovery_pipeline(meeting_title, plain_transcript, formatted_transcript):
    meeting_title        = clean(meeting_title)
    plain_transcript     = clean(plain_transcript)
    formatted_transcript = clean(formatted_transcript)

    log.info("Discovery pipeline start - '%s'", meeting_title)
    results = {"meeting": meeting_title, "docs": [], "errors": []}

    # Extract prospect name from transcript
    log.info("Extracting prospect name...")
    try:
        prospect_name = extract_prospect_name(plain_transcript)
    except Exception as e:
        log.warning("Name extraction failed: %s", e)
        prospect_name = None

    if not prospect_name:
        date_str = datetime.date.today().strftime("%Y-%m-%d")
        prospect_name = "Unknown Prospect " + date_str
        log.info("Name not found - using fallback: %s", prospect_name)
    else:
        log.info("Prospect identified as: %s", prospect_name)

    folder_name = "Discovery Call - " + prospect_name

    # Save transcript
    transcript_title = "[SOTA] Transcript - " + prospect_name
    header  = "# Discovery Call: " + prospect_name + "\n\n"
    header += "## Full Call Transcript\n\n"
    header += "Speaker labels from Zoom recording.\n\n"
    header += "---\n\n"
    ok = create_google_doc(transcript_title, header + formatted_transcript,
                           folder_name, master_folder=DISCOVERY_FOLDER)
    if ok:
        results["docs"].append(transcript_title)
    else:
        results["errors"].append("Transcript: doc creation failed")

    # Generate consolidated Prospect Summary
    try:
        client  = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        msg     = client.messages.create(
            model="claude-opus-4-5",
            max_tokens=2000,
            system=DISCOVERY_SYSTEM,
            messages=[{"role": "user", "content": dc_full_summary(plain_transcript, prospect_name)}],
        )
        summary = clean(msg.content[0].text.strip())
        doc_title = "[SOTA] Prospect Summary - " + prospect_name
        ok = create_google_doc(doc_title, summary, folder_name,
                               master_folder=DISCOVERY_FOLDER)
        if ok:
            results["docs"].append(doc_title)
        else:
            results["errors"].append("Prospect Summary: doc creation failed")
    except Exception as e:
        log.error("Error generating Prospect Summary: %s", e)
        results["errors"].append("Prospect Summary: " + str(e))

    log.info("Discovery pipeline done - %d docs, %d errors",
             len(results["docs"]), len(results["errors"]))
    return results


#  HTTP handler 
class Handler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        log.info(fmt, *args)

    def send_json(self, code, data):
        body = json.dumps(data, indent=2).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def verify_secret(self, body):
        if not WEBHOOK_SECRET:
            return True
        return self.headers.get("X-SOTA-Secret", "") == WEBHOOK_SECRET

    def do_GET(self):
        if self.path == "/health":
            self.send_json(200, {"status": "ok", "service": "SOTA Content Automation v3"})
        else:
            self.send_json(404, {"error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body   = self.rfile.read(length)

        if not self.verify_secret(body):
            self.send_json(401, {"error": "invalid secret"})
            return

        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            self.send_json(400, {"error": "invalid JSON"})
            return

        #  Podcast episode 
        if self.path == "/episode":
            title     = payload.get("title", "").strip()
            audio_url = payload.get("audio_url", "").strip()
            if not title:
                self.send_json(400, {"error": "missing title"})
                return
            if not audio_url:
                self.send_json(400, {"error": "missing audio_url"})
                return
            self.send_json(202, {"status": "accepted", "episode": title})
            threading.Thread(target=run_podcast_pipeline,
                             args=(title, audio_url), daemon=True).start()

        #  Discovery call 
        elif self.path == "/discovery":
            title          = payload.get("title", "").strip()
            vtt_transcript = payload.get("transcript", "").strip()
            if not title:
                self.send_json(400, {"error": "missing title"})
                return
            if len(vtt_transcript) < 50:
                self.send_json(400, {"error": "transcript too short"})
                return
            self.send_json(202, {"status": "accepted", "meeting": title})

            def discovery_job():
                plain, formatted = parse_transcript(vtt_transcript)
                run_discovery_pipeline(title, plain, formatted)

            threading.Thread(target=discovery_job, daemon=True).start()

        else:
            self.send_json(404, {"error": "not found"})


#  Entry point 
if __name__ == "__main__":
    if not ANTHROPIC_API_KEY: log.warning("ANTHROPIC_API_KEY not set")
    if not OPENAI_API_KEY:    log.warning("OPENAI_API_KEY not set")
    if not GOOGLE_SCRIPT_URL: log.warning("GOOGLE_SCRIPT_URL not set")

    # Download Phil voice reference clip from Google Drive on startup
    download_voice_reference()

    server = HTTPServer(("0.0.0.0", PORT), Handler)
    log.info("SOTA Content Automation v3 running on port %d", PORT)
    log.info("Endpoints: POST /episode | POST /discovery")
    log.info("Health:    GET  /health")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Stopped.")
