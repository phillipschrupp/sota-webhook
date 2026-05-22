"""
SOTA Content Automation — Webhook Server
========================================
Receives a podcast episode from Zapier, generates all content
pieces via Claude, then creates a Google Doc for each piece.

Deploy on Render (free tier works fine):
  - Runtime: Python 3
  - Build command: pip install -r requirements.txt
  - Start command: python server.py
  - Add env vars: ANTHROPIC_API_KEY, GOOGLE_SCRIPT_URL, WEBHOOK_SECRET
"""

import os, json, hmac, hashlib, textwrap, logging
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.request import Request, urlopen
from urllib.error import URLError
import anthropic

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("sota")

# ── Config (set these as environment variables on Render) ─────────────────────
ANTHROPIC_API_KEY  = os.environ.get("ANTHROPIC_API_KEY", "")
GOOGLE_SCRIPT_URL  = os.environ.get("GOOGLE_SCRIPT_URL", "")   # your existing Apps Script webhook
WEBHOOK_SECRET     = os.environ.get("WEBHOOK_SECRET", "")      # a random string you pick, add to Zapier too
PORT               = int(os.environ.get("PORT", 8080))

# ── SOTA brand context injected into every Claude prompt ─────────────────────
BRAND_SYSTEM = """You are the content strategist for SOTA Personal Training, a boutique
personal training gym in Minnetonka, Minnesota. SOTA coaches busy adults 40+ using a
psychology-forward approach to nutrition and fitness.

Brand voice: straight-talking, warm, systems-minded, community-rooted.
Tagline: "Strength for Life."

Founder Phil recovered from serious spinal and shoulder injuries through strength
training. SOTA believes fitness is not punishment — it's a path to reclaiming your life.

Rules:
- Never mention the podcast or episode
- Write as a standalone article / post / email
- Use "you" freely. Short sentences. No generic fitness clichés.
- Be specific, warm, direct — like a knowledgeable friend."""

# ── Prompts for each content piece ───────────────────────────────────────────
PROMPTS = {
    "instagram_carousel": lambda t, title: f"""Write a 5-slide Instagram carousel for SOTA Personal Training based on this content.

Format EXACTLY as:
Slide 1: [Hook — bold, scroll-stopping claim or question. Max 15 words.]
[2–3 supporting sentences]

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

[Body — 150–200 words. Relatable hook. Core insight. Soft teaser for Email 2. Sign off as Phil.]

Do NOT mention podcast or episode.

CONTENT:
{t[:3000]}""",

    "email_cta": lambda t, title: f"""Write Email 2 of a 2-email nurture sequence for SOTA Personal Training.
This email follows Email 1 and moves toward a free consultation booking.
Topic derived from: {title}

Format:
Subject: [follow-up subject line]

[Body — 120–160 words. Story or result reinforcing the lesson. Natural CTA to book free
strategy session at sotafitness.com/discovery-call. Sign off as Phil.]

Do NOT mention podcast or episode.

CONTENT:
{t[:3000]}""",

    "instagram_caption": lambda t, title: f"""Write an Instagram Reel / Facebook caption for SOTA Personal Training.
Topic derived from: {title}. Audience: busy adults 40+.

Rules:
- First line hook: max 8 words, must stop the scroll
- 150–250 words total
- Generous line breaks for mobile
- End with 3–5 niche hashtags (#strengthafter40 #fitafter40 style — NOT #fitness)
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

    "blog_post": lambda t, title: f"""Write a full blog post for SOTA Personal Training. 700–1000 words.

CRITICAL RULES:
- Do NOT mention podcast, episode, or "as discussed"
- Standalone educational article written by a SOTA coach
- Audience: busy adults 40+ in Minnetonka / Twin Cities area

FORMAT (match exactly):
# [Title: bold, specific, benefit-driven]

[1–2 sentence hook intro — relatable frustration, gets right to the point]

### [Section Heading]

#### [Subpoint if needed]

[2–4 short paragraphs. Direct, warm, specific. Short sentences.]

[3–5 total sections with ### headings]

### The Bottom Line

[1–2 tight sentences. One CTA sentence.]

### Need help getting started? [Click here](https://www.sotafitness.com/contact) to book a free strategy session.

VOICE: Use "you" freely. **Bold** key phrases. No generic motivational quotes.
Be specific: real numbers, real scenarios, real feelings.

SOURCE CONTENT (use ideas, do not reference as transcript):
{t[:4000]}""",

    "pull_quotes": lambda t, title: f"""Extract 4–5 powerful pull quotes from this content for SOTA Personal Training.
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

# ── Claude API call ───────────────────────────────────────────────────────────
def generate_content(piece_name: str, transcript: str, episode_title: str) -> str:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    prompt_fn = PROMPTS.get(piece_name)
    if not prompt_fn:
        raise ValueError(f"Unknown piece: {piece_name}")

    log.info(f"  Generating {piece_name}…")
    message = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=1500,
        system=BRAND_SYSTEM,
        messages=[{"role": "user", "content": prompt_fn(transcript, episode_title)}],
    )
    return message.content[0].text.strip()


# ── Google Doc creation ───────────────────────────────────────────────────────
def create_google_doc(title: str, content: str, folder: str) -> bool:
    payload = json.dumps({
        "title": title,
        "content": content,
        "folder": folder,
    }).encode()
    req = Request(
        GOOGLE_SCRIPT_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urlopen(req, timeout=20) as resp:
            resp.read()
        log.info(f"  Doc created: {title}")
        return True
    except Exception as e:
        log.error(f"  Doc creation failed for '{title}': {e}")
        return False


# ── Main pipeline ─────────────────────────────────────────────────────────────
def run_pipeline(episode_title: str, transcript: str) -> dict:
    """Generate all content pieces and push each to Google Drive."""
    log.info(f"Pipeline start — '{episode_title}' ({len(transcript)} chars)")

    # Sanitise title for use in doc names
    safe_title = episode_title[:60].strip()
    folder_name = f"SOTA Podcast — {safe_title}"

    pieces = [
        ("instagram_carousel", "Instagram Carousel"),
        ("pull_quotes",        "Pull Quotes"),
        ("email_value",        "Email 1 — Value"),
        ("email_cta",          "Email 2 — CTA"),
        ("instagram_caption",  "Instagram Caption"),
        ("sms",                "SMS Blast"),
        ("blog_post",          "Blog Post"),
    ]

    results = {"episode": episode_title, "docs": [], "errors": []}

    for piece_key, piece_label in pieces:
        try:
            content = generate_content(piece_key, transcript, episode_title)
            doc_title = f"[SOTA] {piece_label} — {safe_title}"
            ok = create_google_doc(doc_title, content, folder_name)
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
            return True  # skip verification if not configured
        sig = self.headers.get("X-SOTA-Secret", "")
        expected = hmac.new(
            WEBHOOK_SECRET.encode(), body, hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(sig, expected)

    def do_GET(self):
        if self.path == "/health":
            self.send_json(200, {"status": "ok", "service": "SOTA Content Automation"})
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
        transcript    = payload.get("transcript", "").strip()

        if not episode_title:
            self.send_json(400, {"error": "missing 'title' field"})
            return
        if len(transcript) < 100:
            self.send_json(400, {"error": "transcript too short (< 100 chars)"})
            return

        # Respond immediately so Zapier doesn't time out, then run pipeline
        self.send_json(202, {"status": "accepted", "episode": episode_title})

        # Run pipeline (in production consider threading this)
        try:
            run_pipeline(episode_title, transcript)
        except Exception as e:
            log.error(f"Pipeline crashed: {e}")


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    if not ANTHROPIC_API_KEY:
        log.warning("ANTHROPIC_API_KEY not set — Claude calls will fail")
    if not GOOGLE_SCRIPT_URL:
        log.warning("GOOGLE_SCRIPT_URL not set — doc creation will fail")

    server = HTTPServer(("0.0.0.0", PORT), Handler)
    log.info(f"SOTA Webhook Server running on port {PORT}")
    log.info(f"Endpoint: POST /episode")
    log.info(f"Health:   GET  /health")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Stopped.")
