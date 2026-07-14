"""
bot/handlers/protocol_handler.py

📡 Выбор протокола подключения — Hysteria2 редакция.

Hysteria2 является единственным и основным протоколом.
Файл оставлен для совместимости, но теперь показывает
только Hysteria2 и не позволяет переключиться на VLESS.
"""
import logging
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command
from sqlalchemy import select

from db.database import AsyncSessionLocal
from db.models import User, UserProtocolChoice, ServerProtocol, Settings
from utils.i18n import t

router = Router()
logger = logging.getLogger(__name__)

PROTOCOL_ICONS = {
    "hysteria2":   "⚡️ Hysteria2",
    "vmess":       "🔷 VMess",
    "trojan":      "🛡️ Trojan",
    "shadowsocks": "🌑 Shadowsocks",
}

PROTOCOL_DESCRIPTIONS = {
    "hysteria2":   "Быстрый UDP-протокол. Обходит блокировки. Рекомендуется.",
    "vmess":       "Классический V2Ray. Хорошая совместимость.",
    "trojan":      "Маскируется под HTTPS. Сложнее блокировать.",
    "shadowsocks": "Простой и надёжный. Поддерживается везде.",
}


async def get_user_protocol(session, user_id: int) -> str:
    """Возвращает текущий выбранный протокол пользователя."""
    result = await session.execute(
        select(UserProtocolChoice).where(UserProtocolChoice.user_id == user_id)
    )
    choice = result.scalar_one_or_none()
    proto = choice.protocol if choice else "hysteria2"
    # Миграция: если у пользователя был vless — автоматически переключаем
    if proto == "vless":
        return "hysteria2"
    return proto


async def get_available_protocols(session) -> list[str]:
    """Возвращает протоколы, настроенные хотя бы на одном активном сервере."""
    from db.models import Server
    result = await session.execute(
        select(ServerProtocol.protocol)
        .join(Server, Server.id == ServerProtocol.server_id)
        .where(Server.is_active == True)
        .where(ServerProtocol.enabled == True)
        .distinct()
    )
    protos = [row[0] for row in result.all()]
    # Hysteria2 всегда доступен (основной протокол)
    if "hysteria2" not in protos:
        protos.insert(0, "hysteria2")
    # Убираем устаревший vless из списка
    protos = [p for p in protos if p != "vless"]
    return protos


def _protocol_kb(available: list[str], current: str) -> InlineKeyboardMarkup:
    rows = []
    for proto in available:
        icon = PROTOCOL_ICONS.get(proto, proto.upper())
        check = " ✅" if proto == current else ""
        rows.append([InlineKeyboardButton(
            text=f"{icon}{check}",
            callback_data=f"set_protocol:{proto}"
        )])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="back_main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(Command("protocol"))
async def cmd_protocol(message: Message):
    async with AsyncSessionLocal() as session:
        user = await session.get(User, message.from_user.id)
        if not user:
            await message.answer("❌ Сначала напиши /start")
            return
        current = await get_user_protocol(session, user.id)
        available = await get_available_protocols(session)

    desc_lines = "\n".join(
        f"• {PROTOCOL_ICONS.get(p, p)}: {PROTOCOL_DESCRIPTIONS.get(p, '')}"
        for p in available
    )
    await message.answer(
        f"📡 <b>Выбор протокола</b>\n\n"
        f"Текущий: <b>{PROTOCOL_ICONS.get(current, current)}</b>\n\n"
        f"{desc_lines}\n\n"
        f"После смены — скопируй новую ссылку подписки в /myconfig",
        parse_mode="HTML",
        reply_markup=_protocol_kb(available, current)
    )


@router.callback_query(F.data == "choose_protocol")
async def cb_choose_protocol(callback: CallbackQuery):
    async with AsyncSessionLocal() as session:
        user = await session.get(User, callback.from_user.id)
        if not user:
            await callback.answer("Нет доступа", show_alert=True)
            return
        current = await get_user_protocol(session, user.id)
        available = await get_available_protocols(session)

    desc_lines = "\n".join(
        f"• {PROTOCOL_ICONS.get(p, p)}: {PROTOCOL_DESCRIPTIONS.get(p, '')}"
        for p in available
    )
    await callback.message.edit_text(
        f"📡 <b>Выбор протокола</b>\n\n"
        f"Текущий: <b>{PROTOCOL_ICONS.get(current, current)}</b>\n\n"
        f"{desc_lines}",
        parse_mode="HTML",
        reply_markup=_protocol_kb(available, current)
    )


@router.callback_query(F.data.startswith("set_protocol:"))
async def set_protocol(callback: CallbackQuery):
    proto = callback.data.split(":")[1]
    # Блокируем попытку выбрать vless напрямую (на случай старых кнопок)
    if proto == "vless":
        proto = "hysteria2"
    if proto not in PROTOCOL_ICONS:
        await callback.answer("Неизвестный протокол", show_alert=True)
        return

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(UserProtocolChoice).where(UserProtocolChoice.user_id == callback.from_user.id)
        )
        choice = result.scalar_one_or_none()
        if choice:
            choice.protocol = proto
        else:
            choice = UserProtocolChoice(user_id=callback.from_user.id, protocol=proto)
            session.add(choice)
        await session.commit()
        available = await get_available_protocols(session)

    await callback.answer(f"✅ Протокол изменён на {PROTOCOL_ICONS.get(proto, proto)}", show_alert=True)
    await callback.message.edit_reply_markup(
        reply_markup=_protocol_kb(available, proto)
    )
