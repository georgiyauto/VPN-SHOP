"""Отдельный бот технической поддержки.
Токен берётся из Settings.support_bot_token (настраивается в админ панели).
"""
from dotenv import load_dotenv
load_dotenv("/app/.env", override=True)

import asyncio
import logging
import os

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.redis import RedisStorage

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)


async def get_token() -> str | None:
    """Получаем токен из БД (Settings.support_bot_token)."""
    try:
        from db.database import AsyncSessionLocal
        from db.models import Settings
        from sqlalchemy import select
        async with AsyncSessionLocal() as session:
            result = await session.execute(select(Settings).where(Settings.id == 1))
            s = result.scalar_one_or_none()
            if s and s.support_bot_token:
                return s.support_bot_token.strip()
    except Exception as e:
        logger.error(f"Failed to get support bot token from DB: {e}")
    return os.getenv("SUPPORT_BOT_TOKEN", "").strip() or None


async def main():
    from db.database import init_db
    await init_db()

    token = await get_token()
    if not token:
        logger.warning("SUPPORT_BOT_TOKEN not set — support bot disabled. Set it in admin panel.")
        # Wait and retry in case it gets set later
        while True:
            await asyncio.sleep(60)
            token = await get_token()
            if token:
                logger.info("Support bot token found, starting...")
                break

    bot = Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    redis_url = os.getenv("REDIS_URL", "redis://redis:6379/1")
    storage = RedisStorage.from_url(redis_url)
    dp = Dispatcher(storage=storage)

    from support_bot.handlers import get_support_router
    dp.include_router(get_support_router())

    logger.info("Support bot started")
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
