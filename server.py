"""
SOTA Content Automation - Webhook Server v2
Receives a podcast episode from Zapier (title + audio URL),
transcribes via OpenAI Whisper, generates 7 content pieces via Claude,
creates a Google Doc for each piece.

Deploy on Render:
  Build command: pip install -r requirements.txt
  Start command: python server.py
  Env vars: ANTHROPIC_API_KEY, OPENAI_API_KEY, GOOGLE_SCRIPT_URL, WEBHOOK_SECRET
"""

import os, json, hmac, hashlib, tempfile, threading, logging, subprocess
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.request import Request, urlopen
import anthropic
import openai

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("sota")

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
OPENAI_API_KEY    = os.environ.get("OPENAI_API_KEY", "")
GOOGLE_SCRIPT_URL = os.environ.get("GOOGLE_SCRIPT_URL", "")
WEBHOOK_SECRET    = os.environ.get("WEBHOOK_SECRET", "")
PORT              = int(os.environ.get("PORT", 8080))
WHISPER_LIMIT     = 24 * 1024 * 1024

BRAND_SYSTEM = (
    "You are the content strategist for SOTA Personal Training, a boutique "
    "personal training gym in Minnetonka, Minnesota. SOTA coaches busy adults 40+ "
    "using a psychology-forward approach to nutrition and fitness.\n\n"
    "Brand voice: straight-talking, warm, systems-minded, community-rooted.\n"
    "Tagline: Strength for Life.\n\n"
    "Founder Phil recovered from serious spinal and shoulder injuries through strength "
    "training. SOTA believes fitness is not punishment - it is a path to reclaiming your life.\n\n"
    "Rules:\n"
    "- Never mention the podcast or episode\n"
    "- Write as a standalone article / post / email\n"
    "- Use 'you' freely. Short sentences. No generic fitness cliches.\n"
    "- Be specific, warm, direct - like a knowledgeable friend."
)

def p_carousel(t, title):
    return (
        "Write a 5-slide Instagram carousel for SOTA Personal Training based on this content.\n\n"
        "Format EXACTLY as:\n"
        "Slide 1: [Hook - bold, scroll-stopping claim or question. Max 15 words.]\n"
        "[2-3 supporting sentences]\n\n"
        "Slide 2: [Title]\n[Content]\n\n"
        "Continue through Slide 5. Slide 5 CTA: Book a free strategy session at sotafitness.com\n\n"
        "Rules: Each slide works standalone. Slide 1 must stop a scroller cold.\n"
        "Do NOT mention podcast or episode.\n\n"
        "CONTENT:\n" + t[:3500]
    )

def p_email_value(t, title):
    return (
        "Write Email 1 of a 2-email nurture sequence for SOTA Personal Training.\n"
        "This email delivers value only - no hard sell.\n"
        "Audience: warm (familiar with SOTA). Topic derived from: " + title + "\n\n"
        "Format:\n"
        "Subject: [compelling subject line]\n\n"
        "[Body - 150-200 words. Relatable hook. Core insight. Soft teaser for Email 2. Sign off as Phil.]\n\n"
        "Do NOT mention podcast or episode.\n\n"
        "CONTENT:\n" + t[:3000]
    )

def p_email_cta(t, title):
    return (
        "Write Email 2 of a 2-email nurture sequence for SOTA Personal Training.\n"
        "This email follows Email 1 and moves toward a free consultation booking.\n"
        "Topic derived from: " + title + "\n\n"
        "Format:\n"
        "Subject: [follow-up subject line]\n\n"
        "[Body - 120-160 words. Story or result reinforcing the lesson. "
        "Natural CTA to book free strategy session at sotafitness.com/discovery-call. Sign off as Phil.]\n\n"
        "Do NOT mention podcast or episode.\n\n"
        "CONTENT:\n" + t[:3000]
    )

def p_caption(t, title):
    return (
        "Write an Instagram Reel / Facebook caption for SOTA Personal Training.\n"
        "Topic derived from: " + title + ". Audience: busy adults 40+.\n\n"
        "Rules:\n"
        "- First line hook: max 8 words, must stop the scroll\n"
        "- 150-250 words total\n"
        "- Generous line breaks for mobile\n"
        "- End with 3-5 niche hashtags (#strengthafter40 #fitafter40 style - NOT #fitness)\n"
        "- Second-to-last paragraph: soft CTA to book free consult at sotafitness.com\n"
        "- Do NOT mention podcast or episode\n\n"
        "CONTENT:\n" + t[:2500]
    )

def p_sms(t, title):
    return (
        "Write a single SMS blast for SOTA Personal Training. MAX 160 characters.\n"
        "Sound like a real person texting, not a brand. Natural CTA to book a free consult.\n"
        "Topic derived from: " + title + ". Do NOT mention podcast.\n\n"
        "Reply with ONLY the SMS text - no labels or explanation.\n\n"
        "CONTENT:\n" + t[:1500]
    )

def p_blog(t, title):
    return (
        "Write a full blog post for SOTA Personal Training. 700-1000 words.\n\n"
        "CRITICAL RULES:\n"
        "- Do NOT mention podcast, episode, or as discussed\n"
        "- Standalone educational article written by a SOTA coach\n"
        "- Audience: busy adults 40+ in Minnetonka / Twin Cities area\n\n"
        "FORMAT:\n"
        "# [Title: specific, benefit-driven]\n\n"
        "[1-2 sentence hook intro]\n\n"
        "### [Section Heading]\n\n"
        "[2-4 short paragraphs per section. 3-5 total sections.]\n\n"
        "### The Bottom Line\n\n"
        "[1-2 tight sentences. One CTA sentence.]\n\n"
        "### Need help getting started? "
        "[Click here](https://www.sotafitness.com/contact) to book a free strategy session.\n\n"
        "VOICE: Use 'you' freely. Bold key phrases. No generic motivational quotes.\n\n"
        "SOURCE CONTENT (use ideas, do not reference as transcript):\n" + t[:4000]
    )

def p_quotes(t, title):
    return (
        "Extract 4-5 powerful pull quotes from this content for SOTA Personal Training.\n"
        "Each quote works as a standalone Instagram Story graphic or shareable image.\n\n"
        "Rules:\n"
        "- Under 25 words each\n"
        "- Surprising, counterintuitive, or emotionally resonant\n"
        "- Rewrite in SOTA voice if needed\n"
        "- Do NOT mention podcast or episode\n"
        "- Format as a numbered list\n\n"
        "CONTENT:\n" + t[:3000]
    )

PROMPTS = {
    "instagram_carousel": p_carousel,
    "email_value":        p_email_value,
    "email_cta":          p_email_cta,
    "instagram_caption":  p_caption,
    "sms":                p_sms,
    "blog_post":          p_blog,
    "pull_quotes":        p_quotes,
}

def clean(text):
    """Remove all non-ASCII characters."""
    return text.encode("ascii", "ignore").decode("ascii")

def compress_audio(input_path):
    output_path = input_path + "_c.mp3"
    cmd = ["ffmpeg", "-y", "-i", input_path,
           "-ac", "1", "-ar", "16000", "-b:a", "32k", "-map", "0:a", output_path]
    result = subprocess.run(cmd, capture_output=True, timeout=120)
    if result.returncode != 0:
        raise RuntimeError("ffmpeg failed: " + result.stderr.decode("ascii", "ignore")[:200])
    log.info("Compressed to %.1f MB", os.path.getsize(output_path) / 1024 / 1024)
    return output_path

def transcribe_audio(audio_url):
    log.info("Downloading audio: %s", audio_url[:80])
    req = Request(audio_url, headers={"User-Agent": "Mozilla/5.0 (compatible; SOTA-Bot/1.0)"})
    with urlopen(req, timeout=120) as resp:
        audio_data = resp.read()
    log.info("Downloaded %.1f MB", len(audio_data) / 1024 / 1024)

    suffix = ".m4a" if audio_url.lower().endswith(".m4a") else (
             ".wav" if audio_url.lower().endswith(".wav") else ".mp3")
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(audio_data)
        tmp_path = tmp.name

    compressed_path = None
    try:
        if len(audio_data) > WHISPER_LIMIT:
            log.info("File exceeds 24 MB - compressing with ffmpeg...")
            compressed_path = compress_audio(tmp_path)
            send_path = compressed_path
        else:
            send_path = tmp_path

        log.info("Sending to Whisper...")
        client = openai.OpenAI(api_key=OPENAI_API_KEY)
        with open(send_path, "rb") as f:
            result = client.audio.transcriptions.create(
                model="whisper-1", file=f, response_format="text")
        transcript = result if isinstance(result, str) else result.text
        log.info("Transcription complete - %d chars", len(transcript))
        return transcript
    finally:
        os.unlink(tmp_path)
        if compressed_path and os.path.exists(compressed_path):
            os.unlink(compressed_path)

def generate_content(piece_name, transcript, episode_title):
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    fn = PROMPTS.get(piece_name)
    if not fn:
        raise ValueError("Unknown piece: " + piece_name)
    log.info("Generating %s...", piece_name)
    msg = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=1500,
        system=BRAND_SYSTEM,
        messages=[{"role": "user", "content": fn(transcript, episode_title)}],
    )
    return msg.content[0].text.strip()

def create_google_doc(title, content, folder):
    payload = json.dumps(
        {"title": title, "content": content, "folder": folder},
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

def run_pipeline(episode_title, audio_url):
    episode_title = clean(episode_title)
    log.info("Pipeline start - '%s'", episode_title)
    safe_title  = episode_title[:60].strip()
    folder_name = safe_title
    results     = {"episode": episode_title, "docs": [], "errors": []}

    try:
        transcript = clean(transcribe_audio(audio_url))
    except Exception as e:
        log.error("Transcription failed: %s", e)
        results["errors"].append("Transcription: " + str(e))
        return results

    pieces = [
        ("instagram_carousel", "Instagram Carousel"),
        ("pull_quotes",        "Pull Quotes"),
        ("email_value",        "Email 1 - Value"),
        ("email_cta",          "Email 2 - CTA"),
        ("instagram_caption",  "Instagram Caption"),
        ("sms",                "SMS Blast"),
        ("blog_post",          "Blog Post"),
    ]

    for piece_key, piece_label in pieces:
        try:
            content   = clean(generate_content(piece_key, transcript, episode_title))
            doc_title = "[SOTA] " + piece_label + " - " + safe_title
            ok        = create_google_doc(doc_title, content, folder_name)
            if ok:
                results["docs"].append(doc_title)
            else:
                results["errors"].append(piece_label + ": doc creation failed")
        except Exception as e:
            log.error("Error on %s: %s", piece_key, e)
            results["errors"].append(piece_label + ": " + str(e))

    log.info("Pipeline done - %d docs, %d errors", len(results["docs"]), len(results["errors"]))
    return results

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
            self.send_json(400, {"error": "missing title"})
            return
        if not audio_url:
            self.send_json(400, {"error": "missing audio_url"})
            return
        self.send_json(202, {"status": "accepted", "episode": episode_title})
        threading.Thread(target=run_pipeline, args=(episode_title, audio_url), daemon=True).start()

if __name__ == "__main__":
    if not ANTHROPIC_API_KEY: log.warning("ANTHROPIC_API_KEY not set")
    if not OPENAI_API_KEY:    log.warning("OPENAI_API_KEY not set")
    if not GOOGLE_SCRIPT_URL: log.warning("GOOGLE_SCRIPT_URL not set")
    server = HTTPServer(("0.0.0.0", PORT), Handler)
    log.info("SOTA Webhook Server v2 running on port %d", PORT)
    log.info("Endpoint: POST /episode  (fields: title, audio_url)")
    log.info("Health:   GET  /health")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Stopped.")
