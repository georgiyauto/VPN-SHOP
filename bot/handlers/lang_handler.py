"""Смена языка бота — RU / EN."""
import logging
from aiogram import Router, F
from aiogram.types import CallbackQuery, Message, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command

from db.database import AsyncSessionLocal
from db.models import User, Settings
from sqlalchemy import select
from utils.i18n import t

router = Router()
logger = logging.getLogger(__name__)


@router.callback_query(F.data == "choose_lang")
@router.message(Command("lang"))
async def choose_language(event):
    if isinstance(event, Message):
        send = event.answer
    else:
        send = event.message.edit_text

    async with AsyncSessionLocal() as session:
        user = await session.get(User, event.from_user.id)
        lang = (user.language if user else "ru") or "ru"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🇷🇺 Русский", callback_data="lang_ru"),
            InlineKeyboardButton(text="🇬🇧 English", callback_data="lang_en"),
        ],
        [InlineKeyboardButton(text=t("btn_back", lang), callback_data="back_main")],
    ])
    await send(t("choose_lang", lang), reply_markup=kb)
    if isinstance(event, CallbackQuery):
        await event.answer()


@router.callback_query(F.data.in_({"lang_ru", "lang_en"}))
async def set_language(callback: CallbackQuery):
    new_lang = "ru" if callback.data == "lang_ru" else "en"

    async with AsyncSessionLocal() as session:
        user = await session.get(User, callback.from_user.id)
        if user:
            user.language = new_lang
            await session.commit()

    await callback.answer(t("lang_changed", new_lang), show_alert=False)

    # Обновляем главное меню через общую функцию (с правильным welcome_text)
    from bot.handlers.main_handlers import _send_main_menu
    from bot.services.vpn_service import get_active_subscription

    async with AsyncSessionLocal() as session:
        user = await session.get(User, callback.from_user.id)
        result = await session.execute(select(Settings).where(Settings.id == 1))
        settings = result.scalar_one()
        sub = await get_active_subscription(session, user.id)

    await _send_main_menu(callback.message, user, settings, sub, new_lang, is_edit=True)


@router.callback_query(F.data == "check_channel")
async def check_channel_subscription(callback: CallbackQuery):
    """Пользователь нажал 'Я подписался' — перепроверяем."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Settings).where(Settings.id == 1))
        settings = result.scalar_one_or_none()
        user = await session.get(User, callback.from_user.id)
        lang = (user.language if user else "ru") or "ru"

    if not settings or not settings.channel_id:
        await callback.answer("✅ OK")
        return

    try:
        member = await callback.bot.get_chat_member(
            chat_id=settings.channel_id,
            user_id=callback.from_user.id
        )
        subscribed = member.status in ("member", "administrator", "creator")
    except Exception:
        subscribed = True

    if subscribed:
        await callback.answer("✅ Подписка подтверждена!", show_alert=False)
        from bot.handlers.main_handlers import _send_main_menu
        from bot.services.vpn_service import get_active_subscription

        async with AsyncSessionLocal() as session:
            result = await session.execute(select(Settings).where(Settings.id == 1))
            settings = result.scalar_one()
            user = await session.get(User, callback.from_user.id)
            sub = await get_active_subscription(session, user.id)

        await _send_main_menu(callback.message, user, settings, sub, lang, is_edit=True)
    else:
        await callback.answer(t("not_subscribed", lang), show_alert=True)
