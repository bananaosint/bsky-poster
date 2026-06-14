import asyncio
from telethon import TelegramClient, events
import ollama
from atproto import AsyncClient
from collections import deque
from sentence_transformers import SentenceTransformer, util
import torch

# Load the model once on startup
print("Loading semantic duplicate detector (CPU)...")
embedding_model = SentenceTransformer('all-MiniLM-L6-v2', device='cpu')

# Settings
RECENT_POSTS_CACHE = deque(maxlen=5)
SEMANTIC_THRESHOLD = 0.65  # Tuned to capture paraphrased news

# ==========================================
# 1. CONFIGURATION & CREDENTIALS
# ==========================================

# Telegram Config
TELEGRAM_API_ID = '28055333'
TELEGRAM_API_HASH = 'ff22dd26d5ab3b17592cd850f9f00154'
TARGET_CHANNEL = -1003770951822 

# Ollama Config
OLLAMA_MODEL = 'gemma4:e2b'
VISION_MODEL = 'moondream'  # Lightweight vision model (~2GB RAM, fast on CPU)
                              # Pull with: ollama pull moondream2

# ==========================================
# [TAG: BATCH QUEUE CONFIG]
# Mirrors the same 2-minute cycle as claudebot.py.
# claudebot flushes → ImagePoster queues → 2 min later ImagePoster
# drains. The two bots naturally interleave and never hit Ollama
# at the same time.
# ==========================================
QUEUE_MAX_SIZE  = 25
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
# 2. INITIALIZE CLIENTS
# ==========================================

blsky_client = AsyncClient()
tg_client = TelegramClient('session_name', TELEGRAM_API_ID, TELEGRAM_API_HASH)

# ==========================================
# 3. ASYNC AI PIPELINE FUNCTIONS
# ==========================================

async def format_for_bsky(message_text):
    """Uses Async Ollama to summarize and format the message."""
    prompt = f"""
    Report this news factually in 25-50 words. Keep exact terminology. No opinions or editorializing. Add flag emojis of countries involved. Use urgent emojis only if original message indicates urgency. Output 1-3 lines, nothing else.
Output the rewritten message ONLY. Do not include internal thoughts, reasoning, or wrapper text.

    Message to rewrite: "{message_text}"
    """
    response = await ollama.AsyncClient().generate(
        model=OLLAMA_MODEL, 
        prompt=prompt,
        options={'temperature': 0.4}
    )
    return response['response'].strip()


# ==========================================
# [TAG: VISION PIPELINE]
# Runs on downloaded image bytes using moondream2.
# Two sequential passes — logo detection first (cheap gate),
# then captioning only if the image passes.
#
# Returns a dict:
#   is_logo: bool
#   caption: str | None  (None if logo, or if captioning failed)
#
# Videos are skipped — moondream2 is image-only, and per-frame
# video captioning on CPU would be impractically slow.
# ==========================================
async def analyze_image(img_bytes: bytes) -> dict:
    import base64
    image_b64 = base64.b64encode(img_bytes).decode("utf-8")

    # ── Pass 1: Logo detection ─────────────────────────────────
    try:
        logo_resp = await ollama.AsyncClient().generate(
            model=VISION_MODEL,
            prompt=(
                "Look at this image carefully. "
                "Is it a logo, channel icon, watermark, avatar, or social media branding graphic? "
                "Reply with only the single word YES or NO."
            ),
            images=[image_b64],
            options={"temperature": 0.0},  # Fully deterministic — this is a binary gate
        )
        is_logo = logo_resp["response"].strip().upper().startswith("YES")
        print(f"🔍 Vision — logo check: {'LOGO detected, dropping image' if is_logo else 'not a logo'}")
    except Exception as e:
        print(f"⚠️ Vision logo check failed: {e}. Treating as non-logo and continuing.")
        is_logo = False

    if is_logo:
        return {"is_logo": True, "caption": None}

    # ── Pass 2: Auto-caption ───────────────────────────────────
    # Only reached if the image is not a logo.
    try:
        caption_resp = await ollama.AsyncClient().generate(
            model=VISION_MODEL,
            prompt=(
                "Describe what is happening in this image in one short factual sentence. "
                "Focus on visible people, missiles, vehicles, damage, or events. "
                "No opinions or speculation. Output the sentence only, nothing else."
            ),
            images=[image_b64],
            options={"temperature": 0.2},
        )
        caption = caption_resp["response"].strip()
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
                    img_bytes = None  # Drop the image, post text-only
                elif not raw_message and vision["caption"]:
                    # No original caption — use what the vision model described
                    raw_message = vision["caption"]
                    print(f"📝 Using vision-generated caption: {raw_message[:80]}")

            # ── Format via Ollama ──────────────────────────────
            # Guard: if the logo was dropped AND there was no caption,
            # there's nothing left to post — skip entirely.
            if not raw_message and not img_bytes and not video_bytes:
                print("⏭️ Nothing to post after vision filter (logo-only message). Skipping.")
                continue
            print("✅ Formatting message...")
            try:
                final_post = await format_for_bsky(raw_message) if raw_message else ""
            except Exception as e:
                print(f"❌ Ollama formatting failed: {e}")
                continue

            if len(final_post) > 300:
                print(f"⚠️ Text too long ({len(final_post)} chars). Truncating...")
                final_post = final_post[:297] + "..."

            if not final_post.strip():
                print("⏭️ Post text is empty after formatting. Skipping.")
                continue

            print(f"📝 Drafted Post:\n---\n{final_post}\n---")

            # ── Post to Bluesky ────────────────────────────────
            try:
                if video_bytes:
                    await blsky_client.send_video(
                        text=final_post,
                        video=video_bytes,
                        video_alt="OSINT video attachment"
                    )
                    print("🚀 Posted text + video to Bluesky!")
                elif img_bytes:
                    await blsky_client.send_image(
                        text=final_post,
                        image=img_bytes,
                        image_alt="OSINT media attachment"
                    )
                    print("🚀 Posted text + image to Bluesky!")
                else:
                    await blsky_client.send_post(text=final_post)
                    print("🚀 Posted text-only to Bluesky!")
            except Exception as e:
                print(f"❌ Failed to post to Bluesky: {e}")

            await asyncio.sleep(1)  # Brief pause between posts — anti-flood


# ==========================================
# 4. THE EVENT LISTENER
# ==========================================

@tg_client.on(events.NewMessage(chats=TARGET_CHANNEL))
async def new_message_handler(event):
    raw_message = event.message.message

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
    # are deduplicated before any of them touch Ollama.
    if raw_message:
        new_embedding = embedding_model.encode(raw_message, convert_to_tensor=True)

        for old_text, old_embedding in RECENT_POSTS_CACHE:
            cosine_score = util.cos_sim(new_embedding, old_embedding).item()
            if cosine_score >= SEMANTIC_THRESHOLD:
                print(f"⏭️ Semantic duplicate ({cosine_score*100:.1f}% match). Skipping.")
                return

        # Unique — add to cache now so subsequent messages in this
        # same batch are checked against it immediately.
        RECENT_POSTS_CACHE.append((raw_message, new_embedding))

    # ── Enqueue for batch processing ──────────────────────────
    message_queue.put_nowait({
        "event":       event,
        "raw_message": raw_message,
        "has_photo":   has_photo,
        "is_video":    is_video,
        "duration":    duration,
    })
    print(f"📥 Queued. ({len(message_queue)}/{QUEUE_MAX_SIZE} pending)")

# ==========================================
# 5. ASYNC STARTUP RUNNER
# ==========================================

async def main():
    print("Connecting to Bluesky...")
    await blsky_client.login('bananaosint.bsky.social', 'ywro-f5pz-wltf-ybda')
    print("Authenticated with Bluesky!")

    # Pre-load Ollama models into memory before any messages arrive
    print("Pre-loading Ollama models...")
    for model_name in [OLLAMA_MODEL, VISION_MODEL]:
        try:
            await ollama.AsyncClient().generate(model=model_name, prompt="test", stream=False)
            print(f"✅ {model_name} ready!")
        except Exception as e:
            print(f"⚠️ Could not pre-load {model_name}: {e}")

    await tg_client.start()
    print("Bot is running. Listening for messages, processing every 2 minutes.")
    await asyncio.gather(
        tg_client.run_until_disconnected(),
        batch_worker(),
    )

if __name__ == '__main__':
    asyncio.run(main())