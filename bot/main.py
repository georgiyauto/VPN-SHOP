from dotenv import load_dotenv
load_dotenv("/app/.env", override=True)
import asyncio
import logging
import os
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.redis import RedisStorage

from db.database import init_db
from bot.handlers.main_handlers      import router as main_router
from bot.handlers.payment_handlers   import router as payment_router
from bot.handlers.broadcast_handler  import router as broadcast_router
from bot.handlers.backup_handler     import router as backup_router
from bot.handlers.balance_handler    import router as balance_router
from bot.handlers.promo_handler      import router as promo_router
from bot.handlers.stars_handler      import router as stars_router
from bot.handlers.qr_handler         import router as qr_router
from bot.handlers.lang_handler       import router as lang_router
from bot.handlers.renewal_handler    import router as renewal_router
from bot.handlers.admin_panel_handler import router as admin_router
# ── v4 ───────────────────────────────────────────────────────────────────────
from bot.handlers.family_handler     import router as family_router
from bot.handlers.support_handler    import router as support_router
from bot.handlers.protocol_handler   import router as protocol_router
from bot.middlewares.middlewares      import (
    AntiFloodMiddleware, BanMiddleware, ChannelSubscriptionMiddleware
)

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)


async def main():
    await init_db()
    logger.info("Database initialized")

    bot = Bot(token=os.getenv("BOT_TOKEN"),
              default=DefaultBotProperties(parse_mode=ParseMode.HTML))

    redis_url = os.getenv("REDIS_URL", "redis://redis:6379/0")
    storage = RedisStorage.from_url(redis_url)
    dp = Dispatcher(storage=storage)

    # Middlewares
    for update_type in (dp.message, dp.callback_query):
        update_type.middleware(AntiFloodMiddleware())
        update_type.middleware(BanMiddleware())
        update_type.middleware(ChannelSubscriptionMiddleware())

    # Routers — порядок важен
    dp.include_router(lang_router)       # check_channel — первым
    dp.include_router(admin_router)      # Reply-кнопки + мини-панель
    dp.include_router(promo_router)
    dp.include_router(main_router)
    dp.include_router(payment_router)
    dp.include_router(promo_router)
    dp.include_router(stars_router)
    dp.include_router(qr_router)
    dp.include_router(renewal_router)
    dp.include_router(broadcast_router)
    dp.include_router(backup_router)
    dp.include_router(balance_router)
    dp.include_router(family_router)
    dp.include_router(support_router)
    dp.include_router(protocol_router)

    logger.info("Bot starting...")

    # Устанавливаем кнопку Mini App в меню бота
    try:
        import os as _os
        bot_domain = _os.getenv("BOT_DOMAIN", "").rstrip("/")
        sub_port = _os.getenv("SUB_PORT", "8433")
        if bot_domain:
            base = bot_domain if ":" in bot_domain.split("//")[-1] else f"{bot_domain}:{sub_port}"
            setup_url = f"{base}/setup"
            from aiogram.types import MenuButtonWebApp, WebAppInfo
            await bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(
                    text="🛡 Подключить VPN",
                    web_app=WebAppInfo(url=setup_url)
                )
            )
            logger.info(f"Mini App menu button set: {setup_url}")
    except Exception as e:
        logger.warning(f"Could not set menu button: {e}")

    from utils.monitoring import start_monitoring
    from utils.expiry_notifier import start_expiry_notifier
    from bot.services.vpn_service import set_bot_instance
    set_bot_instance(bot)
    start_monitoring()
    start_expiry_notifier(bot)
    await dp.start_polling(
        bot,
        allowed_updates=["message", "callback_query", "pre_checkout_query"]
    )


if __name__ == "__main__":
    asyncio.run(main())
