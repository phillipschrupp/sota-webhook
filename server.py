"""
SOTA Content Automation - Webhook Server v4
===========================================
Two independent pipelines:

1. POST /episode  - Podcast episodes (Spotify RSS via Zapier)
   - Downloads audio
   - Transcript: Spotify's SRT from the RSS feed when available, else Whisper (OpenAI)
   - Speaker attribution with enrolled voiceprints (voiceid.py, voiceprints.json):
     Phil / Sean / Rowen / Guest N / Unknown / Unsure
   - Writes three docs to Google Drive: full transcript, Phil-only voice corpus,
     attribution report (talk time + 20 lines to spot-check)

   POST /backfill - Walk the RSS feed and run /episode for every episode not yet done
   GET  /backfill/status

2. POST /discovery - Discovery calls (Zoom VTT/SRT via Zapier)
   - Parses Zoom transcript (speaker names already embedded)
   - Generates transcript + consolidated Prospect Summary

Deploy on Render:
  Build command: pip install -r requirements.txt
  Start command: python server.py
  Env vars: ANTHROPIC_API_KEY, OPENAI_API_KEY, GOOGLE_SCRIPT_URL,
            WEBHOOK_SECRET, HOST_NAME (default: Phil),
            FEED_URL (podcast RSS; used to find Spotify transcripts and for /backfill),
            VOICEPRINTS_PATH (default: voiceprints.json next to this file)
  Removed in v4: VOICE_REF_GDRIVE_ID (single reference clip). See README.md.
"""

import os, json, hashlib, hmac, tempfile, threading, logging, subprocess, time, datetime
import xml.etree.ElementTree as ET
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.request import Request, urlopen
import anthropic
import openai
import voiceid

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
FEED_URL            = os.environ.get("FEED_URL", "https://anchor.fm/s/ff847b50/podcast/rss")
VOICEPRINTS_PATH    = os.environ.get("VOICEPRINTS_PATH",
                                     os.path.join(os.path.dirname(os.path.abspath(__file__)), "voiceprints.json"))
VOICEPRINTS         = None   # loaded at startup

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
        "PURPOSE: This document is used internally by the SOTA team and as raw material "
        "for content creation. The summary should capture not just the information but "
        "the emotion and human truth behind it. Pull quotes should stop a reader cold, "
        "not just summarize a point.\n\n"
        "FORMATTING RULES:\n"
        "- No em dashes. Use commas, semicolons, or periods instead\n"
        "- No asterisks, no markdown, no bold markers\n"
        "- No quotation marks around pull quotes\n"
        "- No bullet dashes or hyphens at line starts\n"
        "- Numbered lists only\n"
        "- One blank line between every text block\n"
        "- Do NOT mention podcast, episode number, or guests by name\n\n"
        "Structure EXACTLY as:\n\n"
        "Episode Summary: " + title + "\n\n"
        "Core Insight\n\n"
        "[2-3 sentences. The single most important idea from this episode, "
        "stated as a standalone truth. Written so that someone who has never heard "
        "the episode walks away with something genuinely useful.]\n\n"
        "Key Takeaways\n\n"
        "1. [Takeaway stated as a plain, specific insight. "
        "Not a vague motivational line. Something concrete and actionable.]\n\n"
        "2. [Next takeaway]\n\n"
        "3. [Next takeaway]\n\n"
        "4. [Next takeaway]\n\n"
        "5. [Next takeaway if the content supports it]\n\n"
        "Pull Quotes\n\n"
        "[5 standalone lines. Each one should land like a punch. "
        "Under 25 words. Surprising, specific, emotionally resonant. "
        "Written in plain language a busy adult would actually say out loud. "
        "No quotation marks around them.]\n\n"
        "1. [First pull quote]\n\n"
        "2. [Second pull quote]\n\n"
        "3. [Third pull quote]\n\n"
        "4. [Fourth pull quote]\n\n"
        "5. [Fifth pull quote]\n\n"
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
    social_proof = """REAL CLIENT TESTIMONIALS TO DRAW FROM (use these directly, paraphrase, or echo their language):
- Mike Gavin: "SOTA has the best trainers I have ever worked with. They've helped me lose nearly sixty pounds and completely transform my health and appearance. I haven't ever been in this good of shape in my life. They really help working people like me who are overweight and out of shape change their lives."
- Jason Lucas: "I was very overweight and very out of shape. I was embarrassed as well as not sure what to expect. Phil made me feel comfortable. Even after major surgery he cheered me along. He listens, understands, but also does not let me get away with my own negative talk and feelings."
- Jenny Brunn: "I have tried everything under the sun to try and shed pounds over the past 10 years. The 1:1 training and HIIT classes have changed my life. I never thought I would say I enjoy working out and eating healthy."
- Hannah Krutsch: "For years I have been working out on and off with no real results and a lot of frustration. I am amazed with how my body has transformed after just a month. The program is so straightforward, effective and efficient."
- Karen M: "The coaches, community, and culture here are unmatched. They've helped me build not just physical strength but also become a stronger and more confident person."
- Amanda Cefalu: "They've helped me lose a ton without being bossy or making me feel bad when I have a bad week."
- Ann Schneider: "Phil is all about accountability and most importantly identifying what works for your lifestyle and personality to maintain that accountability."
- Catherine (500 sessions): "Between juggling mom life and meetings, she still finds a way to show up."
- SOTA overall: 5-star rated across 6,000+ verified reviews on Google, Mindbody, ClassPass, Facebook, and Yelp."""
    return (
        "Write a single high-value marketing email for SOTA Personal Training.\n\n"
        "PURPOSE: Convert non-members on the email list. "
        "This is not a newsletter. It is a personal note from Phil that happens to land "
        "at exactly the right moment in someone's life. It must feel like it was written "
        "for one person, not broadcast to a list.\n\n"
        "PHILOSOPHY: Lead with a story or a moment the reader recognizes from their own life. "
        "Deliver one genuine, research-backed insight. Let a real client outcome show what "
        "is possible. Then offer a next step so gently that the reader thinks of it themselves.\n\n"
        "TARGET READER: A busy professional in their 40s or 50s in the Twin Cities. "
        "Has tried other programs. Puts everyone else first. "
        "Quietly wonders if they are too far gone to turn things around. "
        "The right email makes them feel seen, not sold to.\n\n"
        "REAL CLIENT TESTIMONIALS (weave one in naturally):\n"
        + social_proof + "\n\n"
        "FORMATTING RULES:\n"
        "- No em dashes. Use commas, semicolons, or periods instead\n"
        "- No asterisks, no markdown, no bold markers\n"
        "- No quotation marks unless directly citing a client\n"
        "- One blank line between every paragraph\n"
        "- No exclamation marks\n"
        "- No transform, journey, crush, grind, hustle\n"
        "- Total length: 220-300 words\n"
        "- Do NOT mention podcast or episode\n\n"
        "FORMAT:\n"
        "Subject: [One line. Specific, human, curiosity-driven. No hype.]\n\n"
        "Preview: [40 characters max. Complements the subject line.]\n\n"
        "[Opening: 1-2 sentences. Paint a scene or name a feeling the reader knows well. "
        "No how are you, no I hope this finds you well.]\n\n"
        "[Body: 3-4 short paragraphs. One idea per paragraph. "
        "Weave in the core insight from the content. "
        "Include one peer-reviewed finding stated plainly, no academic format. "
        "Include one client story or outcome paraphrased from the testimonials above. "
        "Write like a trusted friend who happens to know a lot about this.]\n\n"
        "[Closing: 1-2 sentences. A warm, low-pressure invitation. "
        "Something like: if this resonates, I would love to talk. "
        "https://www.sotafitness.com/contact]\n\n"
        "Phil\n\n"
        "SOURCE CONTENT:\n" + t[:3500]
    )


def p_instagram(t, title):
    social_proof = """REAL CLIENT TESTIMONIALS TO DRAW FROM (use these directly, paraphrase, or echo their language):
- Mike Gavin: "SOTA has the best trainers I have ever worked with. They've helped me lose nearly sixty pounds and completely transform my health and appearance. I haven't ever been in this good of shape in my life. They really help working people like me who are overweight and out of shape change their lives."
- Jason Lucas: "I was very overweight and very out of shape. I was embarrassed as well as not sure what to expect. Phil made me feel comfortable. Even after major surgery he cheered me along. He listens, understands, but also does not let me get away with my own negative talk and feelings."
- Jenny Brunn: "I have tried everything under the sun to try and shed pounds over the past 10 years. The 1:1 training and HIIT classes have changed my life. I never thought I would say I enjoy working out and eating healthy."
- Hannah Krutsch: "For years I have been working out on and off with no real results and a lot of frustration. I am amazed with how my body has transformed after just a month. The program is so straightforward, effective and efficient."
- Karen M: "The coaches, community, and culture here are unmatched. They've helped me build not just physical strength but also become a stronger and more confident person."
- Amanda Cefalu: "They've helped me lose a ton without being bossy or making me feel bad when I have a bad week."
- Ann Schneider: "Phil is all about accountability and most importantly identifying what works for your lifestyle and personality to maintain that accountability."
- Catherine (500 sessions): "Between juggling mom life and meetings, she still finds a way to show up."
- SOTA overall: 5-star rated across 6,000+ verified reviews on Google, Mindbody, ClassPass, Facebook, and Yelp."""
    return (
        "Write a complete Instagram content package for SOTA Personal Training. "
        "Topic: " + title + ".\n\n"
        "PHILOSOPHY: Every slide must do one of three things: stop someone mid-scroll with "
        "a truth they have never heard stated that clearly, make them feel deeply understood, "
        "or show them that change is actually possible for someone like them. "
        "Rooted in storytelling, emotion, education, and inspiration. "
        "The reader should think: this feels like it was written specifically for me.\n\n"
        "TARGET READER: A 47-year-old professional in the Twin Cities. Busy. Skeptical. "
        "Has tried other programs and stopped. Puts everyone else first. "
        "Knows they need to do something but has a quiet voice in the back of their head "
        "saying it probably will not work this time either.\n\n"
        "CONTENT REQUIREMENTS:\n"
        "- Ground every claim in the transcript content\n"
        "- Weave in at least one peer-reviewed research insight naturally (no citation format, "
        "just the finding stated plainly)\n"
        "- Include at least one real client story or testimonial paraphrase from the list below\n"
        "- Use storytelling: open with a scene, a moment, a feeling, not a fact\n"
        "- CTA is warm and low-pressure, never salesy\n\n"
        "FORMATTING RULES:\n"
        "- No em dashes. Use commas, semicolons, or periods instead\n"
        "- No asterisks, no markdown, no special characters\n"
        "- No quotation marks unless directly citing a client\n"
        "- One blank line between every slide and every paragraph\n"
        "- Do NOT mention podcast or episode\n\n"
        "REAL CLIENT TESTIMONIALS (draw from these):\n"
        + social_proof + "\n\n"
        "Produce TWO sections:\n\n"
        "CAROUSEL COPY\n\n"
        "5 slides. Each one earns the swipe to the next.\n\n"
        "Slide 1\n"
        "[Hook: A scene, a feeling, or a truth that stops the scroll. Max 12 words. "
        "Make the reader feel seen before they even know what this is about.]\n"
        "[2-3 sentences that deepen the hook. Draw them in with a story or a specific moment "
        "they will recognize from their own life.]\n\n"
        "Slide 2\n"
        "[Title: plain text, no special characters]\n"
        "[2-4 lines. Educate with a specific insight from the content or research. "
        "Make it feel like a revelation, not a lesson.]\n\n"
        "Slide 3\n"
        "[Title]\n"
        "[2-4 lines. This is the emotional core. A client story, a real outcome, "
        "a moment of transformation. Make it human.]\n\n"
        "Slide 4\n"
        "[Title]\n"
        "[2-4 lines. The practical takeaway. What they can do differently starting today. "
        "Concrete and specific.]\n\n"
        "Slide 5\n"
        "[1-2 lines. Warm, low-pressure invitation. Not a command. Something like: "
        "If any of this sounds familiar, we would love to talk. "
        "sotafitness.com]\n\n"
        "POST CAPTION\n\n"
        "[First line: 8 words max. A feeling or truth that stops the scroll.]\n\n"
        "[150-220 words. Open with a brief story or scene. "
        "Weave in the core insight from the transcript. "
        "Include one real client echo or outcome. "
        "One blank line between every paragraph. "
        "Generous line breaks for mobile.]\n\n"
        "[Second-to-last paragraph: one warm sentence pointing to sotafitness.com]\n\n"
        "[Last line: 3-4 niche hashtags. #strengthafter40 #fitafter40 style]\n\n"
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


#  Feed + transcript sources
def fetch_text(url, timeout=60):
    req = Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; SOTA-Bot/1.0)"})
    with urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "ignore")


def fetch_feed(feed_url=None):
    """
    Parse the podcast RSS. Returns a list (newest first) of dicts:
    title, pub_date (RFC 2822 string), audio_url, transcript_url (Spotify SRT or None), duration.
    """
    xml_text = fetch_text(feed_url or FEED_URL, timeout=60)
    root = ET.fromstring(xml_text)
    ns = {"podcast": "https://podcastindex.org/namespace/1.0",
          "itunes":  "http://www.itunes.com/dtds/podcast-1.0.dtd"}
    items = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        enc   = it.find("enclosure")
        audio = enc.get("url") if enc is not None else None
        tr    = None
        for t in it.findall("podcast:transcript", ns):
            typ = (t.get("type") or "").lower()
            if "subrip" in typ or t.get("url", "").endswith(".srt"):
                tr = t.get("url"); break
        if tr is None:
            t = it.find("podcast:transcript", ns)
            tr = t.get("url") if t is not None else None
        items.append(dict(title=title, pub_date=(it.findtext("pubDate") or "").strip(),
                          audio_url=audio, transcript_url=tr,
                          duration=(it.findtext("itunes:duration", default="", namespaces=ns) or "").strip()))
    log.info("Feed: %d items", len(items))
    return items


def _norm_title(t):
    return "".join(ch for ch in clean(t).lower() if ch.isalnum() or ch == " ").strip()


def find_feed_item(title=None, audio_url=None, items=None):
    """Match an episode in the feed by audio URL (exact) or title (normalized, prefix-tolerant)."""
    try:
        items = items or fetch_feed()
    except Exception as e:
        log.warning("Feed lookup failed: %s", e)
        return None
    if audio_url:
        for it in items:
            if it["audio_url"] and it["audio_url"].split("?")[0] == audio_url.split("?")[0]:
                return it
    if title:
        nt = _norm_title(title)
        for it in items:
            ni = _norm_title(it["title"])
            if ni == nt or ni.startswith(nt[:40]) or nt.startswith(ni[:40]):
                return it
    return None


def load_srt_segments(transcript_url):
    """Spotify's machine transcript for the episode, as timed segments."""
    text = fetch_text(transcript_url, timeout=60)
    segs = voiceid.parse_srt_segments(text)
    if len(segs) < 20:
        raise RuntimeError("transcript too short (%d cues)" % len(segs))
    log.info("Loaded %d cues from feed transcript", len(segs))
    return segs


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


# -- Speaker attribution --
def attribute_speakers(audio_path, segments):
    """Label every segment with voiceid. Never guesses: on any failure everything is Unsure
    and the failure reason is returned so it lands in the Attribution Report, not just the log."""
    if VOICEPRINTS is None:
        msg = "No voiceprints loaded - check VOICEPRINTS_PATH"
        log.error(msg)
        return voiceid.all_unsure(segments), msg
    try:
        return voiceid.label_segments(audio_path, segments, VOICEPRINTS), None
    except Exception as e:
        msg = "%s: %s" % (type(e).__name__, str(e)[:400])
        log.error("voiceid failed (%s) - all segments marked Unsure", msg, exc_info=True)
        return voiceid.all_unsure(segments), msg


def _fmt_ts(seconds):
    return "[%02d:%02d]" % (int(seconds // 60), int(seconds % 60))


def build_formatted_transcript(segments, labels):
    """Full transcript with speaker names and timestamps."""
    lines = []
    for seg, r in zip(segments, labels):
        text = seg.text.strip()
        if not text:
            continue
        lab = r["label"]
        if lab is None:
            lines.append(_fmt_ts(seg.start) + "\n" + text)
        else:
            lines.append("**" + lab + "** " + _fmt_ts(seg.start) + "\n" + text)
    return "\n\n".join(lines)


def build_phil_corpus(segments, labels, episode_title):
    """
    Only segments attributed to HOST_NAME with confidence, grouped into paragraphs
    on consecutive segments. Unsure, Unknown, and guest segments break a paragraph
    and never enter the corpus.
    """
    chunks, cur = [], []
    for seg, r in zip(segments, labels):
        text = seg.text.strip()
        if not text:
            continue
        if r["label"] == HOST_NAME:
            cur.append(text)
        elif cur:
            chunks.append(" ".join(cur)); cur = []
    if cur:
        chunks.append(" ".join(cur))
    chunks = [c for c in chunks if len(c.split()) >= 4]
    if not chunks:
        return None
    header  = "# " + HOST_NAME + " Voice Corpus: " + episode_title + "\n\n"
    header += "## About This Document\n\n"
    header += ("This document contains only " + HOST_NAME + "'s spoken words from this episode, "
               "attributed by voiceprint matching with high confidence, cleaned into flowing "
               "paragraphs without timestamps or other speakers. Wording is untouched. "
               "It is used to train Claude to write in " + HOST_NAME + "'s voice and tone.\n\n")
    header += "---\n\n"
    return header + "\n\n".join(chunks)


def build_attribution_report(segments, labels, episode_title, source, failure=None):
    """Talk time per speaker, confidence summary, and 20 Phil lines to spot-check."""
    tt = voiceid.talk_time(segments, labels)
    total = sum(tt.values()) or 1.0
    order = sorted(tt.items(), key=lambda kv: -kv[1])
    vp_desc = VOICEPRINTS.describe() if VOICEPRINTS else "no voiceprints loaded"
    out  = "# Attribution Report: " + episode_title + "\n\n"
    out += "Transcript source: " + source + "\n"
    out += "Speaker attribution: " + vp_desc + "\n"
    if failure:
        out += ("\n**ATTRIBUTION DID NOT RUN.** Every segment is Unsure because the speaker-ID step failed, "
                "not because the audio was hard to match. Error: " + failure + "\n")
    out += ("Rule: long segments need similarity >= %.2f and margin >= %.2f; short segments must agree "
            "with a confident neighbor; Unknown = not an enrolled voice; Unsure = too close to call.\n\n"
            % (voiceid.SIM_FLOOR, voiceid.MARGIN))
    out += "## Talk time\n\n"
    for lab, secs in order:
        out += "%s: %d:%02d (%.0f%%)\n" % (lab, int(secs // 60), int(secs % 60), secs / total * 100)
    n = len(segments)
    conf = sum(1 for r in labels if r["label"] and r["label"] not in (voiceid.UNSURE, voiceid.UNKNOWN))
    out += "\nSegments: %d total, %d labeled with a name (%.0f%%), %d Unsure, %d Unknown, %d too short to embed.\n" % (
        n, conf, conf / max(n, 1) * 100,
        sum(1 for r in labels if r["label"] == voiceid.UNSURE),
        sum(1 for r in labels if r["label"] == voiceid.UNKNOWN),
        sum(1 for r in labels if r["label"] is None))
    phil = [(seg, r) for seg, r in zip(segments, labels) if r["label"] == HOST_NAME and len(seg.text.split()) >= 6]
    step = max(1, len(phil) // 20)
    sample = phil[::step][:20]
    out += "\n## Spot check: lines attributed to " + HOST_NAME + "\n\n"
    out += "Mark any line that is not " + HOST_NAME + ".\n\n"
    for k, (seg, r) in enumerate(sample, 1):
        out += "%d. %s %s\n" % (k, _fmt_ts(seg.start), seg.text.strip()[:160])
    return out


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
    # Apps Script builds the doc line by line; a 950-line transcript can take well over
    # 20 seconds. Long timeout, and retry once only when the request never reached the
    # script (a timeout may mean the doc was created anyway, so never retry those).
    for attempt in (1, 2):
        try:
            with urlopen(req, timeout=180) as resp:
                resp.read()
            log.info("Doc created: %s", title)
            return True
        except Exception as e:
            timed_out = isinstance(e, TimeoutError) or isinstance(getattr(e, "reason", None), TimeoutError)
            if timed_out or attempt == 2:
                log.error("Doc creation failed for '%s': %s", title, e)
                return False
            log.warning("Doc creation attempt %d failed for '%s' (%s) - retrying", attempt, title, e)
            time.sleep(5)
    return False


#  Podcast pipeline
def run_podcast_pipeline(episode_title, audio_url, transcript_url=None):
    episode_title = clean(episode_title)
    log.info("Podcast pipeline start - '%s'", episode_title)
    safe_title      = episode_title[:60].strip()
    results         = {"episode": episode_title, "docs": [], "errors": []}
    audio_path      = None
    compressed_path = None

    try:
        # Audio (original quality is what voiceid listens to)
        audio_data = download_audio(audio_url)
        audio_path = save_audio_temp(audio_data, audio_url)

        # Transcript: feed SRT when we have one, else Whisper
        segments, source = None, None
        if transcript_url:
            try:
                segments = load_srt_segments(transcript_url)
                source   = "Spotify transcript (SRT from RSS feed)"
            except Exception as e:
                log.warning("Feed transcript unavailable (%s) - falling back to Whisper", e)
        if segments is None:
            process_path = audio_path
            if len(audio_data) > WHISPER_LIMIT:
                log.info("Compressing audio for Whisper...")
                compressed_path = compress_audio(audio_path)
                process_path = compressed_path
            _, segments = transcribe_whisper(process_path)
            source = "OpenAI Whisper"

        # Who said what
        labels, attr_failure = attribute_speakers(audio_path, segments)
        if attr_failure:
            results["errors"].append("Speaker attribution: " + attr_failure)

        formatted = clean(build_formatted_transcript(segments, labels))
        corpus    = build_phil_corpus(segments, labels, episode_title)
        corpus    = clean(corpus) if corpus else None
        report    = clean(build_attribution_report(segments, labels, episode_title, source, attr_failure))

    except Exception as e:
        log.error("Audio processing failed: %s", e)
        results["errors"].append("Audio processing: " + str(e))
        return results
    finally:
        for p in (audio_path, compressed_path):
            if p and os.path.exists(p):
                os.unlink(p)

    vp_version = VOICEPRINTS.version if VOICEPRINTS else "none"

    # 1. Full transcript doc
    transcript_title = "[SOTA] Transcript - " + safe_title
    t_header  = "# " + episode_title + "\n\n"
    t_header += "## Full Episode Transcript\n\n"
    t_header += ("Transcript: " + source + ". Speaker labels by voiceprint matching (voiceid, voiceprints v"
                 + vp_version + "). Unknown = voice not enrolled. Unsure = too close to call.\n\n")
    t_header += "---\n\n"
    if create_google_doc(transcript_title, t_header + formatted, safe_title, master_folder=PODCAST_FOLDER):
        results["docs"].append(transcript_title)
    else:
        results["errors"].append("Transcript: doc creation failed")

    # 2. Phil voice corpus doc
    if corpus:
        corpus_title = "[SOTA] " + HOST_NAME + " Voice Corpus - " + safe_title
        if create_google_doc(corpus_title, corpus, safe_title, master_folder=PODCAST_FOLDER):
            results["docs"].append(corpus_title)
        else:
            results["errors"].append("Voice corpus: doc creation failed")
    else:
        results["errors"].append("Voice corpus: no confident " + HOST_NAME + " segments")

    # 3. Attribution report
    report_title = "[SOTA] Attribution Report - " + safe_title
    if create_google_doc(report_title, report, safe_title, master_folder=PODCAST_FOLDER):
        results["docs"].append(report_title)
    else:
        results["errors"].append("Attribution report: doc creation failed")

    log.info("Podcast pipeline done - %d docs, %d errors", len(results["docs"]), len(results["errors"]))
    return results


#  Backfill: walk the feed
BACKFILL = {"running": False, "started": None, "finished": None, "plan": [], "done": [], "errors": [], "current": None}


def run_backfill(items, delay_seconds):
    BACKFILL.update(running=True, started=datetime.datetime.utcnow().isoformat(), finished=None,
                    done=[], errors=[], current=None)
    try:
        for it in items:
            BACKFILL["current"] = it["title"]
            log.info("Backfill: %s", it["title"])
            try:
                res = run_podcast_pipeline(it["title"], it["audio_url"], it.get("transcript_url"))
                BACKFILL["done"].append({"title": it["title"], "docs": len(res["docs"]), "errors": res["errors"]})
            except Exception as e:
                log.error("Backfill failed for '%s': %s", it["title"], e)
                BACKFILL["errors"].append({"title": it["title"], "error": str(e)})
            time.sleep(delay_seconds)
    finally:
        BACKFILL.update(running=False, current=None, finished=datetime.datetime.utcnow().isoformat())
        log.info("Backfill finished: %d done, %d errors", len(BACKFILL["done"]), len(BACKFILL["errors"]))


def plan_backfill(payload):
    """
    Choose which feed items to process.
      feed_url      optional, defaults to FEED_URL
      skip_titles   titles already done (matched loosely); typically the Drive folder names
      only_titles   process only these
      oldest_first  default true
      max           cap on number of episodes
    """
    items = fetch_feed(payload.get("feed_url") or FEED_URL)
    items = [it for it in items if it.get("audio_url")]
    skip  = {_norm_title(t)[:40] for t in payload.get("skip_titles", [])}
    only  = {_norm_title(t)[:40] for t in payload.get("only_titles", [])}
    if only:
        items = [it for it in items if _norm_title(it["title"])[:40] in only]
    if skip:
        items = [it for it in items if _norm_title(it["title"])[:40] not in skip]
    if payload.get("oldest_first", True):
        items = list(reversed(items))
    mx = int(payload.get("max", 0) or 0)
    if mx > 0:
        items = items[:mx]
    return items


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
            self.send_json(200, {"status": "ok", "service": "SOTA Content Automation v4",
                                 "voiceprints": VOICEPRINTS.describe() if VOICEPRINTS else None})
        elif self.path == "/backfill/status":
            self.send_json(200, BACKFILL)
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
            title          = payload.get("title", "").strip()
            audio_url      = payload.get("audio_url", "").strip()
            transcript_url = payload.get("transcript_url", "").strip() or None
            if not title:
                self.send_json(400, {"error": "missing title"})
                return
            if not audio_url:
                self.send_json(400, {"error": "missing audio_url"})
                return
            self.send_json(202, {"status": "accepted", "episode": title})

            def episode_job():
                tr = transcript_url
                if not tr and FEED_URL:
                    # Zapier only sends title + audio_url; look up Spotify's transcript in the feed
                    item = find_feed_item(title=title, audio_url=audio_url)
                    if item and item.get("transcript_url"):
                        tr = item["transcript_url"]
                        log.info("Found feed transcript for '%s'", title)
                run_podcast_pipeline(title, audio_url, tr)

            threading.Thread(target=episode_job, daemon=True).start()

        #  Backfill from the RSS feed
        elif self.path == "/backfill":
            if BACKFILL["running"]:
                self.send_json(409, {"error": "backfill already running", "current": BACKFILL["current"]})
                return
            try:
                items = plan_backfill(payload)
            except Exception as e:
                self.send_json(502, {"error": "feed error: " + str(e)})
                return
            plan = [{"title": it["title"], "pub_date": it["pub_date"],
                     "has_transcript": bool(it.get("transcript_url"))} for it in items]
            BACKFILL["plan"] = plan
            if payload.get("dry_run", False):
                self.send_json(200, {"status": "dry_run", "count": len(plan), "plan": plan})
                return
            delay = float(payload.get("delay_seconds", 30))
            self.send_json(202, {"status": "accepted", "count": len(plan), "plan": plan})
            threading.Thread(target=run_backfill, args=(items, delay), daemon=True).start()

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

    # Load enrolled voiceprints (voiceprints.json). Without them nothing is labeled Phil.
    try:
        VOICEPRINTS = voiceid.Voiceprints.load(VOICEPRINTS_PATH)
        log.info("Loaded %s", VOICEPRINTS.describe())
    except Exception as e:
        log.error("Could not load voiceprints from %s: %s", VOICEPRINTS_PATH, e)

    server = HTTPServer(("0.0.0.0", PORT), Handler)
    log.info("SOTA Content Automation v4 running on port %d", PORT)
    log.info("Endpoints: POST /episode | POST /backfill | POST /discovery")
    log.info("Health:    GET  /health | GET /backfill/status")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Stopped.")
