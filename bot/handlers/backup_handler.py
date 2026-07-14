"""Обработчик команды /backup для ручного запуска бэкапа из Telegram."""
import os
import logging
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command

router = Router()
logger = logging.getLogger(__name__)


def is_admin(user_id: int) -> bool:
    admin_ids = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
    return user_id in admin_ids


@router.message(Command("backup"))
async def cmd_backup(message: Message):
    if not is_admin(message.from_user.id):
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, создать бэкап", callback_data="do_backup")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_backup")],
    ])
    await message.answer(
        "💾 <b>Резервная копия БД</b>\n\n"
        "Создать бэкап прямо сейчас и отправить сюда?",
        reply_markup=kb,
        parse_mode="HTML"
    )


@router.callback_query(F.data == "do_backup")
async def do_backup(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return

    await callback.message.edit_text("⏳ Создаю резервную копию...")

    from utils.backup import send_backup_to_admins
    ok = await send_backup_to_admins(bot=callback.bot)

    if ok:
        await callback.message.edit_text("✅ Бэкап создан и отправлен!")
    else:
        await callback.message.edit_text(
            "❌ Ошибка создания бэкапа.\n"
            "Проверьте что pg_dump доступен в контейнере."
        )


@router.callback_query(F.data == "cancel_backup")
async def cancel_backup(callback: CallbackQuery):
    await callback.message.edit_text("❌ Отменено")
