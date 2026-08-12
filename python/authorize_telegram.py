"""Interactive one-time Telegram session authorization for installed builds."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from telethon.sync import TelegramClient


PROJECT_DIR = Path(__file__).resolve().parent
load_dotenv(PROJECT_DIR / ".env")


def main() -> int:
    api_id_text = os.getenv("TELEGRAM_API_ID", "").strip()
    api_hash = os.getenv("TELEGRAM_API_HASH", "").strip()
    if not api_id_text or not api_hash:
        print("Telegram API credentials are missing from .env.")
        return 1

    print("Telegram Strike Monitor - secure one-time authorization")
    print("Telegram will ask for your phone number and login code.")
    print()

    client = TelegramClient(
        str(PROJECT_DIR / "autotrend_session"),
        int(api_id_text),
        api_hash,
    )
    try:
        client.start()
        identity = client.get_me()
        display_name = " ".join(
            value for value in (identity.first_name, identity.last_name) if value
        )
        print()
        print(f"Authorization completed successfully for {display_name or identity.id}.")
        return 0
    except Exception as error:
        print()
        print(f"Authorization failed: {error}")
        return 1
    finally:
        client.disconnect()


if __name__ == "__main__":
    raise SystemExit(main())
