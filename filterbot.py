import asyncio
import logging
import time
import sys
from telethon import TelegramClient, events
from deep_translator import GoogleTranslator
from groq import AsyncGroq
import json
from pydantic import BaseModel, ValidationError
from langdetect import detect, LangDetectException
from collections import deque
from sentence_transformers import SentenceTransformer, util
import torch
from dotenv import load_dotenv
import os

# ==========================================
# [TAG: WINDOWS UTF-8 FIX]
# Windows consoles default to cp1252, which cannot encode the
# emoji characters used in log messages. Reconfigure to UTF-8
# so print() never throws UnicodeEncodeError.
# ==========================================
sys.stdout.reconfigure(encoding='utf-8')
sys.stderr.reconfigure(encoding='utf-8')

# ==========================================
# [TAG: LOAD DOTENV]
# ==========================================
load_dotenv('private.env')

# ==========================================
# [TAG: LOGGING SETUP]
# Configures the console to show what the bot is doing.
# Useful for debugging translation failures or LLM errors.
# ==========================================
logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ==========================================
# [TAG: SEMANTIC DUPLICATE FILTER]
# Uses sentence-transformers to detect paraphrased duplicates.
# Runs AFTER the keyword pre-filter, BEFORE translation + Groq.
# ==========================================
logger.info("Loading semantic duplicate detector (CPU)...")
embedding_model = SentenceTransformer('all-MiniLM-L6-v2', device='cpu')

RECENT_POSTS_CACHE = deque(maxlen=13)
SEMANTIC_THRESHOLD = 0.65  # Tuned to capture paraphrased duplicates

# ==========================================
# [TAG: CONFIGURATION]
# Loads channel config from channels.json.
# Run (TOOL)export_telegram_channels.py to generate this file.
# ==========================================
TELEGRAM_API_ID = int(os.getenv('TELEGRAM_API_ID'))
TELEGRAM_API_HASH = os.getenv('TELEGRAM_API_HASH')
SESSION_NAME = 'my_local_aggregator' # String: Name for the local sqlite session file

# ── Load channel config from JSON ────────────────────────────
_config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "channels.json")
if not os.path.isfile(_config_path):
    logger.error(
        "channels.json not found! Run the export tool first:\n"
        "  python \"(TOOL)export_telegram_channels.py\"\n"
        "Or copy channels.example.json to channels.json and fill it in."
    )
    sys.exit(1)

with open(_config_path, "r", encoding="utf-8") as _f:
    _channel_config = json.load(_f)

# Convert string keys back to integers for Telethon compatibility
CHANNEL_NAMES = {int(k): v for k, v in _channel_config["channels"].items()}
CHANNEL_TIERS = {int(k): v for k, v in _channel_config.get("channel_tiers", {}).items()}
RAPID_UPDATE_CHANNELS = set(_channel_config.get("rapid_update_channels", []))
DESTINATION_CHANNEL = _channel_config["destination_channel"]

logger.info(f"Loaded {len(CHANNEL_NAMES)} channels from channels.json")
logger.info(f"Destination channel: {DESTINATION_CHANNEL}")

# Groq Config
GROQ_API_KEY = os.getenv('GROQ_API_KEY')
GROQ_MODEL = 'llama-3.1-8b-instant'
groq_client = AsyncGroq(api_key=GROQ_API_KEY)

# ==========================================
# [TAG: BOT STATS]
# Runtime counters for the heartbeat monitor.
# ==========================================
BOT_START_TIME = time.time()
stats = {
    "messages_received": 0,
    "prefilter_passed": 0,
    "prefilter_blocked": 0,
    "semantic_deduped": 0,
    "messages_forwarded": 0,
    "messages_rejected": 0,
    "groq_failures": 0,
}

# Snapshot of stats at last daily digest — used to compute 24h deltas
last_digest_stats = dict(stats)

# ==========================================
# [TAG: KEYWORD PRE-FILTER CONFIG]
# Three-tier filter that runs BEFORE translation and Groq.
# Cost: ~0ms. Eliminates the majority of junk without touching the AI.
#
# Tier 0 — OVERRIDE:      If ANY of these appear, pass IMMEDIATELY.
#                          Reject list is never checked. These are
#                          high-confidence breaking-news signals that
#                          must never be blocked by a noise word.
#
# Tier 1 — INSTANT REJECT: If ANY of these appear (and no override
#                          matched), drop immediately.
#
# Tier 2 — MUST MATCH:    Message must contain at least one of these
#                          to proceed to Groq. Everything else drops.
#
# Keywords are lowercased and matched as substrings.
# ==========================================

OVERRIDE_KEYWORDS = {
    # These are unambiguous hard-news signals. A message containing
    # any of these passes regardless of what else it contains.
    # Keep this list SHORT and HIGH-CONFIDENCE only.

    # Strongest kinetic signals
    "airstrike", "air strike", "air raid",
    "missile strike", "rocket attack", "drone strike",
    "ballistic missile", "cruise missile", "hypersonic",
    "shelling", "artillery fire", "mortar attack",
    "car bomb", "suicide bomb", "ied",
    "bombing", "bombardment",

    # Confirmed casualties / destruction
    "confirmed killed", "confirmed dead", "mass casualty",
    "civilian casualties", "civilian deaths",

    # Unambiguous hard events
    "coup", "coup attempt", "assassinated", "assassination attempt",
    "martial law", "state of emergency",
    "nuclear", "chemical weapon", "nerve agent", "bioweapon",
    "terrorist attack", "terror attack",
    "declared war", "war declared", "invasion", "explosion",
    "killed", "dead", "mourning", "fire",

    # OSINT / breaking markers
    "osint", "breaking:", "flash:", "urgent:", "⚠️",
    "intercepted", "satellite imagery", "footage shows",
}

INSTANT_REJECT_KEYWORDS = {
    # Fundraising / commerce
    "donate", "donation", "fundrais", "gofundme", "paypal", "crypto wallet",
    "buy now", "discount", "sale", "promo code", "subscribe", "shop now",
    "limited offer", "click here", "link in bio", "merch",

    # Engagement bait / channel promotion
    "follow us", "join our channel", "join our group", "share this",
    "repost", "check out our", "our telegram", "subscribe to",
    "turn on notifications", "support us", "become a member",

    # Pure opinion / analysis framing with no event
    "i think", "in my opinion", "imo ", "tbh ", "ngl ", "let's be honest",
    "the truth is", "people need to understand", "thread:",
    "unpopular opinion", "hot take", "change my mind",

    # Historical / educational non-breaking content
    "on this day in", "years ago today", "history of", "did you know",
    "fun fact", "reminder that", "as a reminder",

    # Predictions / speculation with no event
    "i predict", "will probably", "might happen", "could happen",
    "what if", "imagine if", "hypothetically",

    # Soft commentary (no event)
    "explains why", "is proof that", "this is why", "the reason why",
    "a lesson in", "what this means for", "the implications of",
    "analysis:", "opinion:", "perspective:",

    # Prayer / condolences
    "pray for", "prayers for", "god bless", "allah bless", "amen",
    "rest in peace", "rip ", "condolences",

    # Spam patterns
    "limited time", "act now", "forward this",
}

MUST_MATCH_KEYWORDS = {
    # ── Kinetic military action ────────────────────────────────
    "air raid", "airspace", "rocket fire", "rocket barrage",
    "shell", "howitzer", "explosion", "blast", "detonation", "blew up",
    "kamikaze drone", "shahed", "uav",
    "gunfire", "firefight", "sniper fire", "ambush",
    "sortie",

    # ── Territorial / troop movement ──────────────────────────
    "captured", "liberated", "seized", "fell to", "taken by",
    "occupied", "stormed", "overrun", "surrounded", "encircled",
    "breakthrough", "pushed back", "retreated",
    "crossed into", "incursion",
    "frontline", "front line", "line of contact",
    "counter-offensive", "operation launched",
    "ground operation", "ground offensive", "troops deployed",
    "troops entered", "special operation",

    # ── Casualties / damage ────────────────────────────────────
    "killed", "dead", "fatalities", "casualties", "deaths",
    "wounded", "injured", "missing", "presumed dead",
    "destroyed", "demolished", "leveled", "flattened",
    "struck", "hit by", "targeted", "eliminated", "protesters", "protest",
    "attacked", "killed in action", "kia", "killed by"

    # ── High-value events ─────────────────────────────────────
    "shot dead", "executed",
    "overthrown", "seized power",
    "arrested", "detained", "captured alive", "surrendered",
    "indicted", "sanctioned", "designated terrorist",

    # ── Weapons / WMD ─────────────────────────────────────────
    "radiological", "dirty bomb", "enrichment",
    "sarin", "chlorine gas", "biological weapon",
    "weaponized", "icbm", "warhead", "fatah", "rocket launched",
    "aircraft shot down", "missile launched", "missile fired",
    "drones launched", "drones fired", "drones shot down", "shahed",
    "attempted", "failed",

    # ── Cyber / infrastructure attacks ────────────────────────
    "cyberattack", "cyber attack", "hacked", "ransomware",
    "data breach", "infrastructure attack", "grid attack",
    "ddos", "malware deployed", "systems compromised",
    "blackout", "power outage", "water supply",
    "pipeline attack", "pipeline explosion", "pipeline shut",

    # ── Terrorism / mass violence ──────────────────────────────
    "mass shooting", "hostage", "hostages taken", "kidnapped",
    "beheaded", "execution video", "claimed responsibility",
    "isis", "al-qaeda", "hamas", "hezbollah", "wagner",
    "iof", "idf", "idf forces", "idf soldier", "israeli forces",
    "hamas military wing", "russian forces", "russian army", "ukrainian army",
    "jews", "muslims", "kurds", "kurdistan",

    # ── Major geopolitical events ─────────────────────────────
    "ceasefire", "ceasefire violated", "ceasefire collapsed",
    "peace deal", "peace talks collapsed", "negotiations broke",
    "sanctions imposed", "sanctions package", "embargo",
    "expulsion", "expelled ambassador", "diplomatic crisis",
    "nato article 5", "nato activated", "un security council",
    "emergency session", "resolution passed", "veto",

    # ── Economic shocks ───────────────────────────────────────
    "market crash", "stock crash", "currency collapsed",
    "sovereign default", "debt crisis",
    "bank run", "bank collapse", "financial crisis",
    "oil embargo", "energy crisis",
    "hyperinflation", "economic collapse",

    # ── Disasters / mass incidents ────────────────────────────
    "earthquake", "magnitude", "tsunami", "volcanic eruption",
    "catastrophic flood", "catastrophic fire", "wildfire",
    "plane crash", "train crash", "ship sunk",
    "mass evacuation", "displaced", "refugee crisis",
    "famine", "outbreak", "epidemic", "pandemic declared",

    # ── Intelligence / OSINT signals ──────────────────────────
    "intercepted", "leaked", "declassified",
    "exclusive:", "confirmed by",
    "video evidence",
}

# ==========================================
# [TAG: DATA STRUCTURES]
# Pydantic validates the JSON returned by Groq,
# preventing it from returning conversational text like "Here is your evaluation..."
# ==========================================
class FilterDecision(BaseModel):
    important: bool
    urgency: int  # 1 = routine, 2 = notable, 3 = flash/breaking
    reason: str

# ==========================================
# [TAG: KEYWORD PRE-FILTER ENGINE]
# Returns a tuple: (should_proceed: bool, reason: str)
#
# Language-aware, three-tier order of operations:
#   1. Detect language (~1ms, no network).
#   2. If non-English → translate first so English keyword lists apply.
#   3. OVERRIDE check — high-confidence signals that bypass everything.
#      A match here passes instantly, reject list is never consulted.
#   4. INSTANT REJECT — noise patterns that disqualify the message.
#   5. MUST MATCH — at least one signal keyword required to proceed.
# ==========================================
def keyword_prefilter(text: str) -> tuple[bool, str]:

    # ── Step 1: Language detection ─────────────────────────────
    check_text = text
    try:
        lang = detect(text)
        if lang != 'en':
            logger.info(f"⚡ Pre-filter: detected '{lang}', translating before keyword check...")
            try:
                check_text = GoogleTranslator(source='auto', target='en').translate(text)
            except Exception as e:
                logger.warning(f"⚡ Pre-filter translation failed ({e}). Falling back to raw text.")
                check_text = text
    except LangDetectException:
        logger.debug("⚡ Pre-filter: language detection failed, using raw text.")

    lowered = check_text.lower()

    # ── Step 2: Override check (bypasses reject list entirely) ─
    # These are unambiguous hard-news signals. If any match, the
    # message passes immediately — no further checks run.
    for kw in OVERRIDE_KEYWORDS:
        if kw in lowered:
            return True, f"override keyword: '{kw}'"

    # ── Step 3: Instant reject ─────────────────────────────────
    for kw in INSTANT_REJECT_KEYWORDS:
        if kw in lowered:
            return False, f"instant-reject keyword: '{kw}'"

    # ── Step 4: Must-match ─────────────────────────────────────
    for kw in MUST_MATCH_KEYWORDS:
        if kw in lowered:
            return True, f"matched signal keyword: '{kw}'"

    return False, "no signal keywords found"


# ==========================================
# [TAG: TRANSLATION ENGINE]
# Uses deep-translator. We run this in a separate thread later 
# so its synchronous web requests don't freeze the Telegram event loop.
# ==========================================
def translate_text(text: str) -> str:
    if not text or len(text.strip()) == 0:
        return text
        
    try:
        # source='auto' detects the language. target='en' forces English.
        translator = GoogleTranslator(source='auto', target='en')
        translated = translator.translate(text)
        return translated
    except Exception as e:
        logger.error(f"Translation error: {e}")
        return text # Fallback to original text if translation fails

# ==========================================
# [TAG: LLM FILTERING ENGINE]
# Sends the translated text to Groq (Llama 3.1 8B).
# Asks it to evaluate the text against your specific criteria.
# Now includes urgency scoring and source credibility context.
# ==========================================
async def evaluate_message(text: str, source_tier: str = "medium") -> FilterDecision | None:
    # Customize this prompt to define what "important" means to you.
    system_prompt = (
    
"You are a ruthless, high-signal intelligence filter. Your single purpose is to evaluate Telegram messages "
        "and output a strict decision: is this message critical to forward, or is it noise?\n\n"
        
"**🚨 ABSOLUTE OVERRIDE RULE 🚨**\n"
        "If a message reports kinetic military action (e.g., airstrikes, bombings, drone strikes, gunfire, troop movements), Or alerts for kinetic military action." 
        "Or if a message includes the word OSINT in it "
        "it is ALWAYS IMPORTANT: TRUE with URGENCY: 3. Do NOT classify it as subjective noise, even if it is written poorly, contains slang, or includes emotional reactions.\n\n"

        "**CRITERIA FOR 'IMPORTANT: TRUE' (Approve if it meets ANY of these):**\n"
        "* **Flash Alerts:** Short, unpolished, breaking reports of kinetic military action, strikes, or major accidents.\n"
        "* **Hard Data:** Contains empirical data, statistics, or deep technical/cyber security analysis.\n"
        "* **Major Breaking Events:** a significant geopolitical, economic, or global security event.\n\n"
        
        "**CRITERIA FOR 'IMPORTANT: FALSE' (Reject if it meets ANY of these):**\n"
        "* **Noise:** rants, emotional venting, or commentary.\n"
        "* **Engagement Junk:** Memes, jokes, engagement bait, or conversational filler.\n"
        "* **Propaganda:** Blatant state-sponsored PR, obvious bias, or unsourced hype.\n\n"

        "**URGENCY LEVELS (set 'urgency' field):**\n"
        "* **3 — FLASH:** Active kinetic events, mass casualty incidents, breaking military strikes, confirmed assassinations.\n"
        "* **2 — NOTABLE:** Significant geopolitical developments, sanctions, diplomatic events, major arrests.\n"
        "* **1 — ROUTINE:** General newsworthy updates that pass the importance filter but are not time-critical.\n\n"

        "Return a JSON object with 'important' (boolean), 'urgency' (integer 1-3), and 'reason' (string). "
        "Output ONLY the JSON object, no other text."
    )
    
    try:
        # Using AsyncGroq ensures the Telegram bot keeps listening while the LLM "thinks"
        response = await groq_client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[
                {'role': 'system', 'content': system_prompt},
                {'role': 'user', 'content': f"Source credibility tier: {source_tier}\nMessage payload: {text}"}
            ],
            response_format={"type": "json_object"},
            temperature=0.1,
            max_completion_tokens=256,
            stream=False,
        )
        
        # Parse the JSON string returned by Groq back into our Pydantic object
        raw_json_string = response.choices[0].message.content
        decision = FilterDecision.model_validate_json(raw_json_string)
        return decision

    except ValidationError as e:
        logger.error(f"Groq returned invalid JSON: {e}")
        return None
    except Exception as e:
        logger.error(f"Groq API error: {e}")
        return None

# ==========================================
# [TAG: GROQ RETRY WRAPPER]
# Retries evaluate_message with exponential backoff.
# Prevents a single Groq API hiccup from silently dropping messages.
# ==========================================
async def evaluate_message_with_retry(text: str, source_tier: str = "medium", max_retries: int = 3) -> FilterDecision | None:
    for attempt in range(max_retries):
        result = await evaluate_message(text, source_tier)
        if result is not None:
            return result
        wait = 2 ** attempt
        logger.warning(f"⚠️ Groq attempt {attempt+1}/{max_retries} failed. Retrying in {wait}s...")
        await asyncio.sleep(wait)
    logger.error("All Groq retries exhausted. Dropping message.")
    return None

# ==========================================
# [TAG: INITIALIZE CLIENT]
# Creates the Telethon client tied to your user account.
# ==========================================
client = TelegramClient(SESSION_NAME, TELEGRAM_API_ID, TELEGRAM_API_HASH)

# ==========================================
# [TAG: TELEGRAM PIPELINE]
# This event listener triggers automatically every time a new 
# message arrives in one of your SOURCE_CHANNELS.
# ==========================================
@client.on(events.NewMessage(chats=CHANNEL_NAMES))
async def process_new_message(event):
    raw_text = event.message.text
    stats["messages_received"] += 1
    
    # [TAG: MEDIA DETECTION]
    # Check if the message contains a photo, document, or video
    has_media = event.message.media is not None

    # Skip if there is no text to evaluate
    if not raw_text:
        if has_media:
            logger.info("📸 Received media with no caption. Skipping (no captionless images).")
        else:
            logger.info("Received empty text message. Skipping.")
        return

    # ──────────────────────────────────────────────
    # [TAG: KEYWORD PRE-FILTER]
    # Runs FIRST — before translation, before Groq.
    # Zero cost: pure string matching, ~0ms per message.
    # Drops obvious noise and passes only signal-bearing
    # messages downstream. Expect 70-90% of messages to
    # be dropped here, never touching the AI.
    # ──────────────────────────────────────────────
    should_proceed, filter_reason = keyword_prefilter(raw_text)
    if not should_proceed:
        stats["prefilter_blocked"] += 1
        logger.info(f"⚡ Pre-filter blocked message — {filter_reason}. Skipping Groq.")
        return

    stats["prefilter_passed"] += 1
    logger.info(f"⚡ Pre-filter passed ({filter_reason}). Proceeding to semantic check...")
    # ──────────────────────────────────────────────

    # ──────────────────────────────────────────────
    # [TAG: SEMANTIC DUPLICATE FILTER]
    # Runs AFTER keyword pre-filter, BEFORE translation + Ollama.
    # Embeds the raw text and compares against a rolling cache.
    # Drops paraphrased duplicates so Ollama isn't wasted on them.
    #
    # Per-channel threshold: rapid-update channels (alert sirens,
    # rapid strike reports) get a LOWER bar so legitimate
    # rapid-fire updates aren't caught as duplicates.
    # ──────────────────────────────────────────────
    threshold = 0.55 if event.chat_id in RAPID_UPDATE_CHANNELS else SEMANTIC_THRESHOLD
    new_embedding = embedding_model.encode(raw_text, convert_to_tensor=True)

    for old_text, old_embedding in RECENT_POSTS_CACHE:
        cosine_score = util.cos_sim(new_embedding, old_embedding).item()
        if cosine_score >= threshold:
            stats["semantic_deduped"] += 1
            logger.info(f"⏭️ Semantic duplicate ({cosine_score*100:.1f}% match, threshold={threshold}). Skipping.")
            return

    # Unique — add to cache so subsequent messages are checked against it
    RECENT_POSTS_CACHE.append((raw_text, new_embedding))
    logger.info("🧠 Semantic check passed. Proceeding to translate + evaluate.")
    # ──────────────────────────────────────────────

    logger.info(f"New message received (Media: {has_media}) from {event.chat_id}. Processing...")

    # 2. Translate Text (Running blocking code in a background thread)
    # This prevents the bot from freezing while waiting for Google Translate
    english_text = await asyncio.to_thread(translate_text, raw_text)
    
    # 3. LLM Filtering (with retry and source credibility)
    source_tier = CHANNEL_TIERS.get(event.chat_id, "") or "medium"
    logger.info(f"Sending to Groq for evaluation (source tier: {source_tier})...")
    decision = await evaluate_message_with_retry(english_text, source_tier)
    
    # ==========================================
    # [TAG: ROUTING WITH MEDIA SUPPORT & URGENCY]
    # If approved, sends the translated text with urgency tag.
    # If an image was attached to the original post, it includes
    # it automatically.
    #
    # Urgency tags:
    #   🔴 FLASH    — urgency 3  (kinetic events, mass casualty)
    #   🟡 NOTABLE  — urgency 2  (geopolitical, sanctions, arrests)
    #   (no prefix) — urgency 1  (routine newsworthy updates)
    # ==========================================
    if decision:
        if decision.important:
            stats["messages_forwarded"] += 1
            logger.info(f"✅ IMPORTANT (urgency={decision.urgency}). Reason: {decision.reason}")
            
            source_name = CHANNEL_NAMES.get(event.chat_id, f"Unknown ({event.chat_id})")
            
            # Urgency prefix
            if decision.urgency >= 3:
                urgency_tag = "🔴 FLASH"
            elif decision.urgency == 2:
                urgency_tag = "🟡 NOTABLE"
            else:
                urgency_tag = ""
            
            if urgency_tag:
                final_payload = f"{urgency_tag} — [{source_name}]\n{english_text}"
            else:
                final_payload = f"[{source_name}]\n{english_text}"
            
            # If the original message had an image/video, send it along with the text
            if has_media:
                await client.send_message(
                    DESTINATION_CHANNEL, 
                    final_payload, 
                    file=event.message.media
                )
            else:
                await client.send_message(DESTINATION_CHANNEL, final_payload)
            
            # Anti-Flood mechanism
            await asyncio.sleep(1)
            
        else:
            stats["messages_rejected"] += 1
            logger.info(f"❌ Message ignored. Reason: {decision.reason}")
    else:
        # Fallback: All retries exhausted or Groq unreachable.
        stats["groq_failures"] += 1
        logger.warning("Message evaluation failed (all retries exhausted). Ignoring message.")

# ==========================================
# [TAG: HEARTBEAT]
# Periodic health check — logs uptime and key metrics every 5 min.
# ==========================================
async def heartbeat():
    while True:
        await asyncio.sleep(300)
        uptime_s = int(time.time() - BOT_START_TIME)
        h, remainder = divmod(uptime_s, 3600)
        m, _ = divmod(remainder, 60)
        total = stats["messages_received"]
        pass_pct = (stats["prefilter_passed"] / total * 100) if total else 0
        logger.info(
            f"💓 filterbot | ⏱ {h}h{m:02d}m | "
            f"📥 {total} received | "
            f"⚡ {stats['prefilter_passed']} passed pre-filter ({pass_pct:.0f}%) | "
            f"⏭ {stats['semantic_deduped']} deduped | "
            f"✅ {stats['messages_forwarded']} forwarded | "
            f"❌ {stats['messages_rejected']} rejected | "
            f"🔴 {stats['groq_failures']} Groq fails"
        )

# ==========================================
# [TAG: DAILY DIGEST]
# Sends a 24-hour summary to DESTINATION_CHANNEL and logs.
# Uses delta-based approach: compares current stats to a snapshot
# taken at the previous digest, so heartbeat totals are unaffected.
# ==========================================
async def daily_digest():
    global last_digest_stats
    while True:
        await asyncio.sleep(86400)  # 24 hours
        # Compute deltas since last digest
        daily = {k: stats[k] - last_digest_stats[k] for k in stats}
        last_digest_stats = dict(stats)  # snapshot for next cycle
        
        total = daily["messages_received"]
        pass_pct = (daily["prefilter_passed"] / total * 100) if total else 0
        
        digest = (
            "📊 **filterbot Daily Digest**\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📥 {total} messages received\n"
            f"⚡ {daily['prefilter_passed']} passed pre-filter ({pass_pct:.0f}%)\n"
            f"🧠 {daily['semantic_deduped']} semantic duplicates caught\n"
            f"✅ {daily['messages_forwarded']} forwarded\n"
            f"❌ {daily['messages_rejected']} rejected by Groq\n"
            f"🔴 {daily['groq_failures']} Groq failures"
        )
        
        try:
            await client.send_message(DESTINATION_CHANNEL, digest)
            logger.info("📊 Daily digest sent to destination channel.")
        except Exception as e:
            logger.error(f"Failed to send daily digest: {e}")

# ==========================================
# [TAG: MAIN EXECUTION]
# Starts the event loop with heartbeat, daily digest, and graceful shutdown.
# On first run, it will prompt you for your phone number and
# login code in the terminal.
# ==========================================
async def main():
    await client.start()
    
    # ── Groq startup health check ──────────────────────────────
    try:
        await groq_client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[{"role": "user", "content": "ping"}],
            max_completion_tokens=4,
            stream=False,
        )
        logger.info(f"✅ Groq API connected — model: {GROQ_MODEL}")
    except Exception as e:
        logger.critical(f"🔴 Groq API not reachable: {e}. Bot will start but evaluations will fail until Groq is available.")
    
    logger.info("Bot is running and listening for messages! Press Ctrl+C to stop.")
    try:
        await asyncio.gather(
            client.run_until_disconnected(),
            heartbeat(),
            daily_digest(),
        )
    finally:
        logger.info("Shutting down filterbot...")
        logger.info(
            f"📊 Final stats — "
            f"📥 {stats['messages_received']} received | "
            f"⏭ {stats['semantic_deduped']} deduped | "
            f"✅ {stats['messages_forwarded']} forwarded | "
            f"❌ {stats['messages_rejected']} rejected | "
            f"🔴 {stats['groq_failures']} Groq fails"
        )
        await client.disconnect()

if __name__ == '__main__':
    logger.info("Starting Telegram LLM Filter Bot...")
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("filterbot stopped by user.")