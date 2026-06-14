"""
export_telegram_channels.py

Pulls all followed/joined channels from your Telegram account using Telethon
and generates a channels.json config file.

Setup:
    pip install telethon python-dotenv

    Get your API credentials at https://my.telegram.org/apps
    Then add them to private.env:
        TELEGRAM_API_ID=your_id
        TELEGRAM_API_HASH=your_hash
"""

import asyncio
import json
import os

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.tl.types import Channel

# ── Load credentials from private.env ─────────────────────────────────────────
load_dotenv('private.env')
API_ID   = int(os.getenv("TELEGRAM_API_ID", "0"))
API_HASH =     os.getenv("TELEGRAM_API_HASH", "")
SESSION  = "my_account"
# ──────────────────────────────────────────────────────────────────────────────


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

        # ── Build channels.json structure ──────────────────────────────────────
        config = {
            "destination_channel": -100000000000,
            "channels": {},
            "channel_tiers": {},
            "rapid_update_channels": [],
        }

        for cid, cname in channels:
            config["channels"][str(cid)] = cname
            config["channel_tiers"][str(cid)] = ""  # default: unknown tier

        # ── Write to channels.json ─────────────────────────────────────────────
        output_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "channels.json")
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=4, ensure_ascii=False)

        print(f"✅ Exported {len(channels)} channels to channels.json")
        print(f"   Path: {output_path}")
        print()
        print("Next steps:")
        print("  1. Open channels.json")
        print('  2. Set "destination_channel" to your destination channel ID')
        print('  3. Remove any channels you don\'t want to monitor')
        print('  4. Set channel tiers: "high", "medium", "low", or "" (unknown)')
        print('  5. Add rapid-update channel IDs to "rapid_update_channels"')


if __name__ == "__main__":
    if not API_ID or not API_HASH:
        raise SystemExit(
            "Set TELEGRAM_API_ID and TELEGRAM_API_HASH in private.env\n"
            "Get your credentials at https://my.telegram.org/apps"
        )
    asyncio.run(main())
