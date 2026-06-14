"""
export_telegram_channels.py

Pulls all followed/joined channels from your Telegram account using Telethon
and prints them formatted as a Python dict (CHANNEL_NAMES).

Setup:
    pip install telethon

    Get your API credentials at https://my.telegram.org/apps
    Then set these env vars (or fill in the constants below):
        TELEGRAM_API_ID
        TELEGRAM_API_HASH
"""

import asyncio
import os

from telethon import TelegramClient
from telethon.tl.types import Channel

# ── Credentials ────────────────────────────────────────────────────────────────
API_ID   = int(os.environ.get("TELEGRAM_API_ID",   28055333))    # or hard-code as int
API_HASH =     os.environ.get("TELEGRAM_API_HASH", "ff22dd26d5ab3b17592cd850f9f00154")    # or hard-code as str
SESSION  = "my_account"   # local session file name (no extension needed)
# ───────────────────────────────────────────────────────────────────────────────


async def main() -> None:
    async with TelegramClient(SESSION, API_ID, API_HASH) as client:
        dialogs = await client.get_dialogs()

        channels: list[tuple[int, str]] = []
        for dialog in dialogs:
            entity = dialog.entity
            # Keep only broadcast channels (not groups/supergroups)
            if isinstance(entity, Channel) and entity.broadcast:
                # Telethon gives the "bare" id; prefix with -100 for the full id
                full_id = int(f"-100{entity.id}")
                name    = entity.title or f"channel_{entity.id}"
                channels.append((full_id, name))

        # Sort by channel name for readability
        channels.sort(key=lambda x: x[1].lower())

        # ── Print formatted output ─────────────────────────────────────────────
        print("CHANNEL_NAMES = {")
        for cid, cname in channels:
            # Escape any double-quotes inside the channel name
            safe_name = cname.replace('"', '\\"')
            print(f'    {cid}: "{safe_name}",')
        print("}")


if __name__ == "__main__":
    if not API_ID or not API_HASH:
        raise SystemExit(
            "Set TELEGRAM_API_ID and TELEGRAM_API_HASH env vars "
            "(or hard-code them in the script).\n"
            "Get your credentials at https://my.telegram.org/apps"
        )
    asyncio.run(main())
