"""One-time login to the Telegram account that sends messages.

Usage: set TELEGRAM_API_ID and TELEGRAM_API_HASH (from my.telegram.org), then run
    python -m app.tg_login
and enter the phone number, the code from Telegram and the 2FA password if asked.
"""

import asyncio

from telethon import TelegramClient

from app.config import Settings


async def main() -> None:
    settings = Settings.from_env()
    if not (settings.telegram_api_id and settings.telegram_api_hash):
        raise SystemExit("Set TELEGRAM_API_ID and TELEGRAM_API_HASH first")
    client = TelegramClient(
        str(settings.telegram_session),
        settings.telegram_api_id,
        settings.telegram_api_hash,
        proxy=settings.telegram_proxy_settings,
    )
    await client.start()
    me = await client.get_me()
    print(f"Logged in as {me.first_name} (@{me.username}); session saved to {settings.telegram_session}")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
