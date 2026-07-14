import time
import logging
from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

logger = logging.getLogger(__name__)
_last_seen: dict[int, float] = {}
RATE_LIMIT = 0.5


class AntiFloodMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user_id = None
        if isinstance(event, (Message, CallbackQuery)):
            user_id = event.from_user.id
        if user_id:
            now = time.time()
            if now - _last_seen.get(user_id, 0) < RATE_LIMIT:
                if isinstance(event, CallbackQuery):
                    await event.answer("⏳ Не так быстро!")
                return
            _last_seen[user_id] = now
        return await handler(event, data)


class BanMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user_id = None
        if isinstance(event, (Message, CallbackQuery)):
            user_id = event.from_user.id
        if user_id:
            from db.database import AsyncSessionLocal
            from db.models import User
            from sqlalchemy import select
            async with AsyncSessionLocal() as session:
                result = await session.execute(select(User.is_banned).where(User.id == user_id))
                row = result.first()
                if row and row[0]:
                    if isinstance(event, Message):
                        await event.answer("🚫 Вы заблокированы.")
                    elif isinstance(event, CallbackQuery):
                        await event.answer("🚫 Вы заблокированы.", show_alert=True)
                    return
        return await handler(event, data)


class ChannelSubscriptionMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user_id = None
        skip = False
        if isinstance(event, Message):
            user_id = event.from_user.id
            if event.text and event.text.startswith("/start"):
                skip = True
        elif isinstance(event, CallbackQuery):
            user_id = event.from_user.id
            if event.data in ("check_channel", "lang_ru", "lang_en"):
                skip = True

        if skip or not user_id:
            return await handler(event, data)

        from db.database import AsyncSessionLocal
        from db.models import Settings
        from sqlalchemy import select

        async with AsyncSessionLocal() as session:
            result = await session.execute(select(Settings).where(Settings.id == 1))
            settings = result.scalar_one_or_none()

        if not settings or not settings.channel_required or not settings.channel_id:
            return await handler(event, data)

        bot = getattr(event, "bot", None) or data.get("bot")
        if bot is None:
            return await handler(event, data)

        try:
            member = await bot.get_chat_member(chat_id=settings.channel_id, user_id=user_id)
            if member.status in ("member", "administrator", "creator"):
                return await handler(event, data)
        except Exception as e:
            logger.warning(f"Channel check failed: {e}")
            return await handler(event, data)

        from db.database import AsyncSessionLocal
        from db.models import User
        async with AsyncSessionLocal() as session:
            user = await session.get(User, user_id)
            lang = (user.language if user else "ru") or "ru"

        from utils.i18n import t
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text=t("btn_subscribe_channel", lang),
                url=settings.channel_url or f"https://t.me/{settings.channel_id.lstrip('@')}"
            )],
            [InlineKeyboardButton(text=t("btn_check_sub", lang), callback_data="check_channel")],
        ])
        if isinstance(event, Message):
            await event.answer(t("channel_required", lang), reply_markup=kb, parse_mode="HTML")
        elif isinstance(event, CallbackQuery):
            # ФИКС: редактируем сообщение + отвечаем на callback (иначе кнопка "залипает")
            try:
                await event.message.edit_text(
                    t("channel_required", lang), reply_markup=kb, parse_mode="HTML"
                )
            except Exception:
                pass
            await event.answer(t("not_subscribed", lang), show_alert=True)
        return
