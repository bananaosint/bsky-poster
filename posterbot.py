import asyncio
import time
import sys
from telethon import TelegramClient, events
from groq import AsyncGroq
from atproto import AsyncClient, models
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


# Load the model once on startup
print("Loading semantic duplicate detector (CPU)...")
embedding_model = SentenceTransformer('all-MiniLM-L6-v2', device='cpu')

# Settings
RECENT_POSTS_CACHE = deque(maxlen=13)
SEMANTIC_THRESHOLD = 0.65  # Tuned to capture paraphrased news

# ==========================================
# [TAG: BLUESKY STORY THREADING]
# Tracks recently posted Bluesky messages so incoming updates
# to the same story reply to the original post, creating threads.
#
# Similarity zones (uses same embedding model as dedup):
#   >= 0.65  →  duplicate (already dropped by dedup above)
#   0.40–0.64  →  same story, new info → reply as update
#   < 0.40  →  different story → new standalone post
# ==========================================
POSTED_CACHE: deque = deque(maxlen=15)  # (text, embedding, post_strong_ref, formatted_text)
UPDATE_THRESHOLD = 0.55  # Raised from 0.40 — domain-specific content needs a tighter match

# ==========================================
# 1. CONFIGURATION & CREDENTIALS
# ==========================================

# Telegram Config
TELEGRAM_API_ID = int(os.getenv('TELEGRAM_API_ID'))
TELEGRAM_API_HASH = os.getenv('TELEGRAM_API_HASH')

# Load destination channel from channels.json (shared with filterbot)
_config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "channels.json")
if not os.path.isfile(_config_path):
    print("❌ channels.json not found! Run: python \"(TOOL)export_telegram_channels.py\"")
    sys.exit(1)
with open(_config_path, "r", encoding="utf-8") as _f:
    import json as _json
    _channel_config = _json.load(_f)
TARGET_CHANNEL = _channel_config["destination_channel"]

# Groq Config
GROQ_API_KEY = os.getenv('GROQ_API_KEY')
TEXT_MODEL = 'llama-3.3-70b-versatile'
VISION_MODEL = 'meta-llama/llama-4-scout-17b-16e-instruct'
groq_client = AsyncGroq(api_key=GROQ_API_KEY)

# ==========================================
# [TAG: BATCH QUEUE CONFIG]
# Mirrors the same 2-minute cycle as claudebot.py.
# claudebot flushes → ImagePoster queues → 2 min later ImagePoster
# drains. The two bots naturally interleave and never hit Ollama
# at the same time.
# ==========================================
QUEUE_MAX_SIZE  = 10
BATCH_INTERVAL  = 60  # seconds (1 minutes)

# ==========================================
# [TAG: BOUNDED QUEUE]
# Drops the oldest item (with a log warning) when full,
# so the event handler never stalls during a batch flood.
# ==========================================
class BoundedQueue:
    def __init__(self, maxsize: int):
        self._queue: deque = deque()
        self._maxsize = maxsize

    def put_nowait(self, item) -> None:
        if len(self._queue) >= self._maxsize:
            dropped = self._queue.popleft()
            print(f"⚠️  Queue full ({self._maxsize}). Dropped oldest item.")
        self._queue.append(item)

    def drain(self) -> list:
        items = list(self._queue)
        self._queue.clear()
        return items

    def __len__(self):
        return len(self._queue)

message_queue = BoundedQueue(QUEUE_MAX_SIZE)

# ==========================================
# [TAG: BOT STATS]
# Runtime counters for the heartbeat monitor.
# ==========================================
BOT_START_TIME = time.time()
stats = {
    "messages_received": 0,
    "messages_deduped": 0,
    "messages_queued": 0,
    "messages_posted": 0,
    "messages_threaded": 0,
    "post_failures": 0,
    "logos_dropped": 0,
}

# ==========================================
# 2. INITIALIZE CLIENTS
# ==========================================

blsky_client = AsyncClient()
tg_client = TelegramClient('session_name', TELEGRAM_API_ID, TELEGRAM_API_HASH)

# ==========================================
# 3. ASYNC AI PIPELINE FUNCTIONS
# ==========================================

async def format_for_bsky(message_text):
    """Uses Groq (Llama 3.3) to summarize and format the message."""
    prompt = (
        "Report this news factually in 25-50 words. Keep exact terminology. "
        "No opinions or editorializing. Add flag emojis of countries involved. "
        "Use urgent emojis only if original message indicates urgency. "
        "Output 1-3 lines, nothing else.\n"
        "Output the rewritten message ONLY. Do not include internal thoughts, "
        "reasoning, or wrapper text.\n\n"
        f'Message to rewrite: "{message_text}"'
    )
    response = await groq_client.chat.completions.create(
        model=TEXT_MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.4,
        max_completion_tokens=512,
        top_p=1,
        stream=False,
    )
    return response.choices[0].message.content.strip()


async def format_update_for_bsky(message_text, original_post_text):
    """Formats a message as a thread update, given the original post for context."""
    prompt = (
        "You are writing a SHORT follow-up reply to a Bluesky post. "
        "The original post and a new update are provided below.\n\n"
        "Rules:\n"
        "- Write 15-40 words. This is a REPLY, not a standalone post.\n"
        "- Start with 'UPDATE:' or '⚠️ UPDATE:' if urgent.\n"
        "- Only include NEW information not in the original post.\n"
        "- Do NOT repeat what the original post already says.\n"
        "- Keep exact terminology. No opinions.\n"
        "- Add flag emojis of countries involved.\n"
        "- Output the reply text ONLY. No reasoning or wrapper text.\n\n"
        f'Original post: "{original_post_text}"\n\n'
        f'New update to incorporate: "{message_text}"'
    )
    response = await groq_client.chat.completions.create(
        model=TEXT_MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.4,
        max_completion_tokens=256,
        top_p=1,
        stream=False,
    )
    return response.choices[0].message.content.strip()


# ==========================================
# [TAG: GROQ RETRY WRAPPER]
# Retries format_for_bsky with exponential backoff.
# Prevents a single Groq API hiccup from dropping messages.
# ==========================================
async def format_for_bsky_with_retry(message_text, max_retries=3):
    for attempt in range(max_retries):
        try:
            return await format_for_bsky(message_text)
        except Exception as e:
            wait = 2 ** attempt
            print(f"⚠️ Groq format attempt {attempt+1}/{max_retries} failed: {e}. Retrying in {wait}s...")
            await asyncio.sleep(wait)
    print("❌ All Groq format retries exhausted.")
    return None


async def format_update_for_bsky_with_retry(message_text, original_post_text, max_retries=3):
    for attempt in range(max_retries):
        try:
            return await format_update_for_bsky(message_text, original_post_text)
        except Exception as e:
            wait = 2 ** attempt
            print(f"⚠️ Groq update-format attempt {attempt+1}/{max_retries} failed: {e}. Retrying in {wait}s...")
            await asyncio.sleep(wait)
    print("❌ All Groq update-format retries exhausted.")
    return None


# ==========================================
# [TAG: VISION PIPELINE]
# Runs on downloaded image bytes using Groq + Llama 4 Scout.
# Two sequential passes — logo detection first (cheap gate),
# then captioning only if the image passes.
#
# Returns a dict:
#   is_logo: bool
#   caption: str | None  (None if logo, or if captioning failed)
# ==========================================
async def analyze_image(img_bytes: bytes) -> dict:
    import base64
    image_b64 = base64.b64encode(img_bytes).decode("utf-8")
    image_url = f"data:image/jpeg;base64,{image_b64}"

    # ── Pass 1: Logo detection ─────────────────────────────────
    try:
        logo_resp = await groq_client.chat.completions.create(
            model=VISION_MODEL,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": (
                        "Look at this image carefully. "
                        "Is it a logo, channel icon, watermark, avatar, or social media branding graphic? "
                        "Reply with only the single word YES or NO."
                    )},
                    {"type": "image_url", "image_url": {"url": image_url}},
                ],
            }],
            temperature=0.0,
            max_completion_tokens=8,
            stream=False,
        )
        answer = logo_resp.choices[0].message.content.strip().upper()
        is_logo = answer.startswith("YES")
        print(f"🔍 Vision — logo check: {'LOGO detected, dropping image' if is_logo else 'not a logo'}")
    except Exception as e:
        print(f"⚠️ Vision logo check failed: {e}. Treating as non-logo and continuing.")
        is_logo = False

    if is_logo:
        return {"is_logo": True, "caption": None}

    # ── Pass 2: Auto-caption ───────────────────────────────────
    # Only reached if the image is not a logo.
    try:
        caption_resp = await groq_client.chat.completions.create(
            model=VISION_MODEL,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": (
                        "Describe what is happening in this image in one short factual sentence. "
                        "Focus on visible people, missiles, vehicles, damage, or events. "
                        "No opinions or speculation. Output the sentence only, nothing else."
                    )},
                    {"type": "image_url", "image_url": {"url": image_url}},
                ],
            }],
            temperature=0.2,
            max_completion_tokens=256,
            stream=False,
        )
        caption = caption_resp.choices[0].message.content.strip()
        print(f"🔍 Vision — caption: {caption[:80]}{'...' if len(caption) > 80 else ''}")
    except Exception as e:
        print(f"⚠️ Vision captioning failed: {e}. Proceeding without caption.")
        caption = None

    return {"is_logo": False, "caption": caption}


# ==========================================
# [TAG: BATCH WORKER]
# Wakes every BATCH_INTERVAL seconds, drains the queue, and
# processes each item through: download media → vision check → format → post.
#
# Media is downloaded HERE (not in the handler) because
# download_media is a network call — keeping it out of the
# hot path means the handler returns instantly.
# ==========================================
async def batch_worker():
    print(f"🕐 Batch worker started. Draining every {BATCH_INTERVAL}s, queue cap: {QUEUE_MAX_SIZE}.")
    while True:
        await asyncio.sleep(BATCH_INTERVAL)

        batch = message_queue.drain()
        if not batch:
            print("🕐 Batch worker woke up — queue empty, nothing to process.")
            continue

        print(f"🕐 Batch worker woke up — processing {len(batch)} queued message(s).")

        for item in batch:
            event       = item["event"]
            raw_message = item["raw_message"]
            has_photo   = item["has_photo"]
            is_video    = item["is_video"]
            duration    = item["duration"]

            # ── Download media (deferred from handler) ─────────
            video_bytes = None
            img_bytes   = None

            if is_video:
                if duration > 60:
                    print(f"⏭️ Video skipped: {duration}s (over 1 min limit).")
                else:
                    print(f"🎬 Downloading video ({duration}s)...")
                    try:
                        video_bytes = await event.message.download_media(file=bytes)
                    except Exception as e:
                        print(f"❌ Video download failed: {e}")

            if has_photo:
                print("📸 Downloading image...")
                try:
                    img_bytes = await event.message.download_media(file=bytes)
                except Exception as e:
                    print(f"❌ Image download failed: {e}")

            # ── Vision analysis (images only) ──────────────────
            # Runs after download, before formatting.
            # Logo → drop img_bytes but keep any existing caption.
            # No caption + not a logo → use vision-generated caption.
            if img_bytes:
                vision = await analyze_image(img_bytes)
                if vision["is_logo"]:
                    stats["logos_dropped"] += 1
                    img_bytes = None  # Drop the image, post text-only
                elif not raw_message and vision["caption"]:
                    # No original caption — use what the vision model described
                    raw_message = vision["caption"]
                    print(f"📝 Using vision-generated caption: {raw_message[:80]}")

            # ── Story threading: detect BEFORE formatting ────
            # Compare the raw message against recently posted Bluesky
            # posts. If similar enough, this is a follow-up to an
            # existing story — we'll format it as an update and reply.
            reply_ref = None
            original_post_text = None
            if raw_message:
                msg_embedding = embedding_model.encode(raw_message, convert_to_tensor=True)
                best_score = 0.0
                best_ref = None
                best_text = None
                for cached_raw, cached_emb, cached_ref, cached_post_text in POSTED_CACHE:
                    score = util.cos_sim(msg_embedding, cached_emb).item()
                    if score >= UPDATE_THRESHOLD and score < SEMANTIC_THRESHOLD and score > best_score:
                        best_score = score
                        best_ref = cached_ref
                        best_text = cached_post_text
                if best_ref:
                    reply_ref = models.AppBskyFeedPost.ReplyRef(
                        root=best_ref, parent=best_ref
                    )
                    original_post_text = best_text
                    stats["messages_threaded"] += 1
                    print(f"🧵 Update detected ({best_score*100:.1f}% match). Will format as thread reply.")

            # ── Format via Groq ────────────────────────────
            # Guard: if the logo was dropped AND there was no caption,
            # there's nothing left to post — skip entirely.
            if not raw_message and not img_bytes and not video_bytes:
                print("⏭️ Nothing to post after vision filter (logo-only message). Skipping.")
                continue

            if raw_message and reply_ref and original_post_text:
                # Thread reply: give Groq the original post for context
                print("✅ Formatting as thread update (with original context)...")
                final_post = await format_update_for_bsky_with_retry(raw_message, original_post_text)
            else:
                # Standalone post
                print("✅ Formatting message...")
                final_post = await format_for_bsky_with_retry(raw_message) if raw_message else ""

            if final_post is None:
                print("❌ Groq formatting failed after all retries. Skipping.")
                continue

            if len(final_post) > 300:
                print(f"⚠️ Text too long ({len(final_post)} chars). Truncating...")
                final_post = final_post[:297] + "..."

            if not final_post.strip() and not img_bytes and not video_bytes:
                print("⏭️ Post text is empty after formatting and no media attached. Skipping.")
                continue

            print(f"📝 Drafted Post:\n---\n{final_post}\n---")

            # ── Post to Bluesky ────────────────────────────
            try:
                sent_post = None
                if video_bytes:
                    sent_post = await blsky_client.send_video(
                        text=final_post,
                        video=video_bytes,
                        video_alt="video attachment"
                    )
                    stats["messages_posted"] += 1
                    print("🚀 Posted text + video to Bluesky!")
                elif img_bytes:
                    if reply_ref:
                        upload = await blsky_client.upload_blob(img_bytes)
                        embed = models.AppBskyEmbedImages.Main(
                            images=[models.AppBskyEmbedImages.Image(
                                alt="media attachment",
                                image=upload.blob,
                            )]
                        )
                        sent_post = await blsky_client.send_post(
                            text=final_post, embed=embed, reply_to=reply_ref
                        )
                    else:
                        sent_post = await blsky_client.send_image(
                            text=final_post,
                            image=img_bytes,
                            image_alt="media attachment"
                        )
                    stats["messages_posted"] += 1
                    print("🚀 Posted text + image to Bluesky!")
                else:
                    sent_post = await blsky_client.send_post(
                        text=final_post, reply_to=reply_ref
                    )
                    stats["messages_posted"] += 1
                    print("🚀 Posted text-only to Bluesky!")

                # Store in posted cache for future threading
                # Cache stores: (raw_message, embedding, strong_ref, formatted_post_text)
                if sent_post and raw_message:
                    strong_ref = models.create_strong_ref(sent_post)
                    raw_emb = embedding_model.encode(raw_message, convert_to_tensor=True)
                    POSTED_CACHE.append((raw_message, raw_emb, strong_ref, final_post))

            except Exception as e:
                stats["post_failures"] += 1
                print(f"❌ Failed to post to Bluesky: {e}")

            await asyncio.sleep(1)  # Brief pause between posts — anti-flood


# ==========================================
# 4. THE EVENT LISTENER
# ==========================================

@tg_client.on(events.NewMessage(chats=TARGET_CHANNEL))
async def new_message_handler(event):
    raw_message = event.message.message or ""
    stats["messages_received"] += 1

    # ── Media type detection (metadata only, no download yet) ─
    is_video  = (
        event.message.file and
        event.message.file.mime_type and
        event.message.file.mime_type.startswith('video/')
    )
    duration  = (event.message.file.duration or 0) if is_video else 0
    has_photo = event.message.photo is not None

    # Skip if nothing to work with
    if not raw_message and not has_photo and not is_video:
        return

    snippet = raw_message.replace('\n', ' ')[:60] if raw_message else "[No text, media only]"
    print(f"\n📥 [New Telegram Message]: {snippet}...")

    # ── Semantic duplicate check (stays in hot path) ──────────
    # Must run here so messages within the same incoming batch
    # are deduplicated before any of them touch Groq.
    # Exception: if a "duplicate" matches a previously POSTED story
    # in the update range (0.40-0.64), let it through — it's a
    # follow-up that the batch worker will thread as a reply.
    if raw_message:
        new_embedding = embedding_model.encode(raw_message, convert_to_tensor=True)

        is_dup = False
        for old_text, old_embedding in RECENT_POSTS_CACHE:
            cosine_score = util.cos_sim(new_embedding, old_embedding).item()
            if cosine_score >= SEMANTIC_THRESHOLD:
                is_dup = True
                break

        if is_dup:
            # Before dropping, check if this could be a story update
            is_update = False
            for cached_raw, cached_emb, cached_ref, cached_post in POSTED_CACHE:
                score = util.cos_sim(new_embedding, cached_emb).item()
                if score >= UPDATE_THRESHOLD and score < SEMANTIC_THRESHOLD:
                    is_update = True
                    print(f"🧵 Dedup override: message is a story update ({score*100:.1f}% match to posted). Allowing through.")
                    break

            if not is_update:
                stats["messages_deduped"] += 1
                print(f"⏭️ Semantic duplicate ({cosine_score*100:.1f}% match). Skipping.")
                return

        # Unique (or update) — add to cache
        RECENT_POSTS_CACHE.append((raw_message, new_embedding))

    # ── Enqueue for batch processing ──────────────────────────
    message_queue.put_nowait({
        "event":       event,
        "raw_message": raw_message,
        "has_photo":   has_photo,
        "is_video":    is_video,
        "duration":    duration,
    })
    stats["messages_queued"] += 1
    print(f"📥 Queued. ({len(message_queue)}/{QUEUE_MAX_SIZE} pending)")

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
        print(
            f"💓 posterbot | ⏱ {h}h{m:02d}m | "
            f"📥 {stats['messages_received']} received | "
            f"⏭ {stats['messages_deduped']} deduped | "
            f"🚀 {stats['messages_posted']} posted | "
            f"🧵 {stats['messages_threaded']} threaded | "
            f"❌ {stats['post_failures']} failed | "
            f"🔍 {stats['logos_dropped']} logos dropped | "
            f"📋 Queue: {len(message_queue)}/{QUEUE_MAX_SIZE}"
        )

# ==========================================
# 5. ASYNC STARTUP RUNNER
# ==========================================

async def main():
    print("Connecting to Bluesky...")
    await blsky_client.login(
        os.getenv("BLUESKY_HANDLE"),
        os.getenv("BLUESKY_APP_PASSWORD")
    )

    print("Authenticated with Bluesky!")


    # Verify Groq API connectivity before any messages arrive
    print("Verifying Groq API connection...")
    try:
        test_resp = await groq_client.chat.completions.create(
            model=TEXT_MODEL,
            messages=[{"role": "user", "content": "ping"}],
            max_completion_tokens=4,
            stream=False,
        )
        print(f"✅ Groq API connected — text model: {TEXT_MODEL}, vision model: {VISION_MODEL}")
    except Exception as e:
        print(f"⚠️ Groq API connectivity check failed: {e}")

    await tg_client.start()
    print("Bot is running. Listening for messages, processing every 1 minute.")
    try:
        await asyncio.gather(
            tg_client.run_until_disconnected(),
            batch_worker(),
            heartbeat(),
        )
    finally:
        print("Shutting down posterbot...")
        
        # ── Graceful shutdown: drain remaining queued messages ──
        remaining = message_queue.drain()
        if remaining:
            print(f"🔄 Draining {len(remaining)} remaining message(s) before exit...")
            for item in remaining:
                try:
                    raw_message = item["raw_message"]
                    if raw_message:
                        final_post = await format_for_bsky_with_retry(raw_message)
                        if final_post and final_post.strip():
                            if len(final_post) > 300:
                                final_post = final_post[:297] + "..."
                            await blsky_client.send_post(text=final_post)
                            stats["messages_posted"] += 1
                            print(f"🚀 Shutdown drain: posted text-only.")
                except Exception as e:
                    stats["post_failures"] += 1
                    print(f"⚠️ Shutdown drain failed for item: {e}")
            print("✅ Shutdown drain complete.")
        
        print(
            f"📊 Final stats — "
            f"📥 {stats['messages_received']} received | "
            f"🚀 {stats['messages_posted']} posted | "
            f"❌ {stats['post_failures']} failed | "
            f"⏭ {stats['messages_deduped']} deduped"
        )
        await tg_client.disconnect()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("posterbot stopped by user.")