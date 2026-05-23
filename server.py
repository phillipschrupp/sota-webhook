"""
SOTA Content Automation — Webhook Server v2
============================================
Receives a new podcast episode from Zapier (title + audio URL),
transcribes the audio via OpenAI Whisper, generates all 7 content
pieces via Claude, then creates a Google Doc for each piece.

Deploy on Render:
  - Runtime: Python 3
  - Build command: pip install -r requirements.txt
  - Start command: python server.py
  - Env vars: ANTHROPIC_API_KEY, OPENAI_API_KEY,
              GOOGLE_SCRIPT_URL, WEBHOOK_SECRET
"""

import os, json, hmac, hashlib, tempfile, threading, logging
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.request import Request, urlopen
import anthropic
import openai

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("sota")

# ── Config ────────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
OPENAI_API_KEY    = os.environ.get("OPENAI_API_KEY", "")
GOOGLE_SCRIPT_URL = os.environ.get("GOOGLE_SCRIPT_URL", "")
WEBHOOK_SECRET    = os.environ.get("WEBHOOK_SECRET", "")
PORT              = int(os.environ.get("PORT", 8080))

# ── Brand system prompt ───────────────────────────────────────────────────────
BRAND_SYSTEM = """You are the content strategist for SOTA Personal Training, a boutique
personal training gym in Minnetonka, Minnesota. SOTA coaches busy adults 40+ using a
psychology-forward approach to nutrition and fitness.

Brand voice: straight-talking, warm, systems-minded, community-rooted.
Tagline: "Strength for Life."

Founder Phil recovered from serious spinal and shoulder injuries through strength
training. SOTA believes fitness is not punishment — it is a path to reclaiming your life.

Rules:
- Never mention the podcast or episode
- Write as a standalone article / post / email
- Use "you" freely. Short sentences. No generic fitness cliches.
- Be specific, warm, direct — like a knowledgeable friend."""

# ── Content prompts ───────────────────────────────────────────────────────────
PROMPTS = {
    "instagram_carousel": lambda t, title: f"""Write a 5-slide Instagram carousel for SOTA Personal Training based on this content.

Format EXACTLY as:
Slide 1: [Hook — bold, scroll-stopping claim or question. Max 15 words.]
[2-3 supporting sentences]

Slide 2: [Title]
[Content]

...through Slide 5. Slide 5 CTA: "Book a free strategy session at sotafitness.com"

Rules: Each slide works standalone. Slide 1 must stop a scroller cold.
Do NOT mention podcast or episode.

CONTENT:
{t[:3500]}""",

    "email_value": lambda t, title: f"""Write Email 1 of a 2-email nurture sequence for SOTA Personal Training.
This email delivers value only — no hard sell.
Audience: warm (familiar with SOTA). Topic derived from: {title}

Format:
Subject: [compelling subject line]

[Body — 150-200 words. Relatable hook. Core insight. Soft teaser for Email 2. Sign off as Phil.]

Do NOT mention podcast or episode.

CONTENT:
{t[:3000]}""",

    "email_cta": lambda t, title: f"""Write Email 2 of a 2-email nurture sequence for SOTA Personal Training.
This email follows Email 1 and moves toward a free consultation booking.
Topic derived from: {title}

Format:
Subject: [follow-up subject line]

[Body — 120-160 words. Story or result reinforcing the lesson. Natural CTA to book free
strategy session at sotafitness.com/discovery-call. Sign off as Phil.]

Do NOT mention podcast or episode.

CONTENT:
{t[:3000]}""",

    "instagram_caption": lambda t, title: f"""Write an Instagram Reel / Facebook caption for SOTA Personal Training.
Topic derived from: {title}. Audience: busy adults 40+.

Rules:
- First line hook: max 8 words, must stop the scroll
- 150-250 words total
- Generous line breaks for mobile
- End with 3-5 niche hashtags (#strengthafter40 #fitafter40 style — NOT #fitness)
- Second-to-last paragraph: soft CTA to book free consult at sotafitness.com
- Do NOT mention podcast or episode

CONTENT:
{t[:2500]}""",

    "sms": lambda t, title: f"""Write a single SMS blast for SOTA Personal Training. MAX 160 characters.
Sound like a real person texting, not a brand. Natural CTA to book a free consult.
Topic derived from: {title}. Do NOT mention podcast.

Reply with ONLY the SMS text — no labels or explanation.

CONTENT:
{t[:1500]}""",

    "blog_post": lambda t, title: f"""Write a full blog post for SOTA Personal Training. 700-1000 words.

CRITICAL RULES:
- Do NOT mention podcast, episode, or "as discussed"
- Standalone educational article written by a SOTA coach
- Audience: busy adults 40+ in Minnetonka / Twin Cities area

FORMAT (match exactly):
# [Title: bold, specific, benefit-driven]

[1-2 sentence hook intro — relatable frustration, gets right to the point]

### [Section Heading]

#### [Subpoint if needed]

[2-4 short paragraphs. Direct, warm, specific. Short sentences.]

[3-5 total sections with ### headings]

### The Bottom Line

[1-2 tight sentences. One CTA sentence.]

### Need help getting started? [Click here](https://www.sotafitness.com/contact) to book a free strategy session.

VOICE: Use "you" freely. Bold key phrases. No generic motivational quotes.
Be specific: real numbers, real scenarios, real feelings.

SOURCE CONTENT (use ideas, do not reference as transcript):
{t[:4000]}""",

    "pull_quotes": lambda t, title: f"""Extract 4-5 powerful pull quotes from this content for SOTA Personal Training.
Each quote works as a standalone Instagram Story graphic or shareable image.

Rules:
- Under 25 words each
- Surprising, counterintuitive, or emotionally resonant
- Rewrite in SOTA voice if needed — do NOT credit original source
- Do NOT mention podcast or episode
- Format as a numbered list

CONTENT:
{t[:3000]}""",
}

# ── Whisper transcription ─────────────────────────────────────────────────────
WHISPER_LIMIT = 24 * 1024 * 1024  # 24 MB — stay just under Whisper's 25 MB cap

def compress_audio(input_path: str) -> str:
    """Compress audio to mono MP3 at 32kbps using ffmpeg. Returns path to compressed file."""
    import subprocess
    output_path = input_path + "_compressed.mp3"
    cmd = [
        "ffmpeg", "-y", "-i", input_path,
        "-ac", "1",           # mono
        "-ar", "16000",       # 16kHz sample rate — plenty for speech
        "-b:a", "32k",        # 32kbps bitrate — ~14 MB/hr of audio
        "-map", "0:a",        # audio only
        output_path
    ]
    result = subprocess.run(cmd, capture_output=True, timeout=120)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {result.stderr.decode()[:300]}")
    size = os.path.getsize(output_path)
    log.info(f"  Compressed to {size // 1024 // 1024:.1f} MB")
    return output_path

def transcribe_audio(audio_url: str) -> str:
    log.info(f"  Downloading audio: {audio_url[:80]}...")
    req = Request(audio_url, headers={
        "User-Agent": "Mozilla/5.0 (compatible; SOTA-Bot/1.0)"
    })
    with urlopen(req, timeout=120) as resp:
        audio_data = resp.read()

    size_mb = len(audio_data) / 1024 / 1024
    log.info(f"  Downloaded {size_mb:.1f} MB")

    suffix = ".mp3"
    if audio_url.lower().endswith(".m4a"): suffix = ".m4a"
    elif audio_url.lower().endswith(".wav"): suffix = ".wav"

    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(audio_data)
        tmp_path = tmp.name

    compressed_path = None
    try:
        # Compress if over Whisper's limit
        if len(audio_data) > WHISPER_LIMIT:
            log.info(f"  File exceeds 24 MB — compressing with ffmpeg...")
            compressed_path = compress_audio(tmp_path)
            send_path = compressed_path
        else:
            send_path = tmp_path

        log.info(f"  Sending to Whisper...")
        client = openai.OpenAI(api_key=OPENAI_API_KEY)
        with open(send_path, "rb") as audio_file:
            result = client.audio.transcriptions.create(
                model="whisper-1",
                file=audio_file,
                response_format="text",
            )
        transcript = result if isinstance(result, str) else result.text
        log.info(f"  Transcription complete — {len(transcript)} chars")
        return transcript
    finally:
        os.unlink(tmp_path)
        if compressed_path and os.path.exists(compressed_path):
            os.unlink(compressed_path)


# ── Claude content generation ─────────────────────────────────────────────────
def generate_content(piece_name: str, transcript: str, episode_title: str) -> str:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    prompt_fn = PROMPTS.get(piece_name)
    if not prompt_fn:
        raise ValueError(f"Unknown piece: {piece_name}")
    # Sanitize any non-ASCII characters from transcript and title
    clean_transcript = transcript.encode("ascii", "ignore").decode("ascii")
    clean_title = episode_title.encode("ascii", "ignore").decode("ascii")
    log.info(f"  Generating {piece_name}...")
    message = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=1500,
        system=BRAND_SYSTEM,
        messages=[{"role": "user", "content": prompt_fn(clean_transcript, clean_title)}],
    )
    return message.content[0].text.strip()


# ── Google Doc creation ───────────────────────────────────────────────────────
def create_google_doc(title: str, content: str, folder: str) -> bool:
    payload = json.dumps({"title": title, "content": content, "folder": folder}, ensure_ascii=False).encode("utf-8")
    req = Request(GOOGLE_SCRIPT_URL, data=payload,
                  headers={"Content-Type": "application/json"})
    try:
        with urlopen(req, timeout=20) as resp:
            resp.read()
        log.info(f"  Doc created: {title}")
        return True
    except Exception as e:
        log.error(f"  Doc creation failed for '{title}': {e}")
        return False


# ── Main pipeline ─────────────────────────────────────────────────────────────
def run_pipeline(episode_title: str, audio_url: str) -> dict:
    # Sanitize all text to ASCII-safe at entry — handles em dashes, curly quotes, etc.
    episode_title = episode_title.encode("ascii", "ignore").decode("ascii")
    log.info(f"Pipeline start - '{episode_title}'")
    safe_title  = episode_title[:60].strip()
    folder_name = f"SOTA Podcast — {safe_title}"
    results     = {"episode": episode_title, "docs": [], "errors": []}

    try:
        transcript = transcribe_audio(audio_url)
        transcript = transcript.encode("ascii", "ignore").decode("ascii")
    except Exception as e:
        log.error(f"  Transcription failed: {e}")
        results["errors"].append(f"Transcription: {e}")
        return results

    pieces = [
        ("instagram_carousel", "Instagram Carousel"),
        ("pull_quotes",        "Pull Quotes"),
        ("email_value",        "Email 1 — Value"),
        ("email_cta",          "Email 2 — CTA"),
        ("instagram_caption",  "Instagram Caption"),
        ("sms",                "SMS Blast"),
        ("blog_post",          "Blog Post"),
    ]

    for piece_key, piece_label in pieces:
        try:
            content   = generate_content(piece_key, transcript, episode_title)
            doc_title = f"[SOTA] {piece_label} — {safe_title}"
            ok        = create_google_doc(doc_title, content, folder_name)
            if ok:
                results["docs"].append(doc_title)
            else:
                results["errors"].append(f"{piece_label}: doc creation failed")
        except Exception as e:
            log.error(f"  Error on {piece_key}: {e}")
            results["errors"].append(f"{piece_label}: {str(e)}")

    log.info(f"Pipeline done — {len(results['docs'])} docs, {len(results['errors'])} errors")
    return results


# ── HTTP handler ──────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        log.info(fmt % args)

    def send_json(self, code: int, data: dict):
        body = json.dumps(data, indent=2).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def verify_secret(self, body: bytes) -> bool:
        if not WEBHOOK_SECRET:
            return True
        sig = self.headers.get("X-SOTA-Secret", "")
        return sig == WEBHOOK_SECRET

    def do_GET(self):
        if self.path == "/health":
            self.send_json(200, {"status": "ok", "service": "SOTA Content Automation v2"})
        else:
            self.send_json(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/episode":
            self.send_json(404, {"error": "not found"})
            return

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

        episode_title = payload.get("title", "").strip()
        audio_url     = payload.get("audio_url", "").strip()

        if not episode_title:
            self.send_json(400, {"error": "missing 'title' field"})
            return
        if not audio_url:
            self.send_json(400, {"error": "missing 'audio_url' field"})
            return

        self.send_json(202, {"status": "accepted", "episode": episode_title})

        threading.Thread(
            target=run_pipeline,
            args=(episode_title, audio_url),
            daemon=True
        ).start()


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    if not ANTHROPIC_API_KEY: log.warning("ANTHROPIC_API_KEY not set")
    if not OPENAI_API_KEY:    log.warning("OPENAI_API_KEY not set")
    if not GOOGLE_SCRIPT_URL: log.warning("GOOGLE_SCRIPT_URL not set")

    server = HTTPServer(("0.0.0.0", PORT), Handler)
    log.info(f"SOTA Webhook Server v2 running on port {PORT}")
    log.info(f"Endpoint: POST /episode  (fields: title, audio_url)")
    log.info(f"Health:   GET  /health")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Stopped.")
