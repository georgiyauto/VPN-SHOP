"""
Рассылка через /broadcast — копирует сообщение с сохранением:
- Премиум эмодзи
- Форматирования (bold, italic, code, spoiler)
- Медиа: фото, видео, документ, аудио, голосовое, стикер, анимация
- Инлайн-кнопка (опционально)

Использует bot.copy_message — отправляет точную копию сообщения.
"""
import os
import asyncio
import logging
from aiogram import Router, F, Bot
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.filters import Command
from sqlalchemy import select

from db.database import AsyncSessionLocal
from db.models import User, Subscription, Broadcast
from datetime import datetime

router = Router()
logger = logging.getLogger(__name__)

ADMIN_IDS_ENV = os.getenv("ADMIN_IDS", "")


def is_admin(user_id: int) -> bool:
    return user_id in [int(x) for x in ADMIN_IDS_ENV.split(",") if x.strip()]


class BroadcastStates(StatesGroup):
    choosing_target  = State()   # выбор аудитории
    waiting_message  = State()   # ждём само сообщение от админа
    waiting_button   = State()   # ждём текст|ссылку кнопки
    confirm          = State()   # подтверждение


# In-memory store: {admin_id: {target, from_chat_id, message_id, button_text, button_url}}
_bc: dict = {}


# ── /broadcast ────────────────────────────────────────────────────────────────

@router.message(Command("broadcast"))
async def cmd_broadcast(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    _bc[message.from_user.id] = {}
    await state.set_state(BroadcastStates.choosing_target)
    await message.answer(
        "📢 <b>Рассылка</b>\n\nКому отправить?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="👥 Всем пользователям",       callback_data="bc_target:all")],
            [InlineKeyboardButton(text="✅ Только активным подписчикам", callback_data="bc_target:active")],
            [InlineKeyboardButton(text="❌ Отмена",                    callback_data="bc_cancel")],
        ]),
        parse_mode="HTML"
    )


@router.callback_query(F.data.startswith("bc_target:"))
async def bc_choose_target(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    target = callback.data.split(":")[1]
    _bc[callback.from_user.id]["target"] = target
    await state.set_state(BroadcastStates.waiting_message)
    await callback.message.edit_text(
        "📢 <b>Рассылка</b>\n\n"
        "Отправьте мне сообщение которое нужно разослать.\n\n"
        "Поддерживается:\n"
        "• Текст с форматированием и премиум эмодзи\n"
        "• Фото / видео / документ / аудио\n"
        "• Голосовое / анимация / стикер\n"
        "• Любые медиагруппы\n\n"
        "<i>Бот скопирует сообщение как есть — все эмодзи и форматирование сохранятся</i>",
        parse_mode="HTML"
    )
    await callback.answer()


# ── Получаем сообщение для рассылки ──────────────────────────────────────────

@router.message(BroadcastStates.waiting_message)
async def bc_receive_message(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return

    uid = message.from_user.id
    _bc[uid]["from_chat_id"] = message.chat.id
    _bc[uid]["message_id"]   = message.message_id

    # Определяем тип для истории
    if message.photo:         _bc[uid]["type"] = "photo"
    elif message.video:       _bc[uid]["type"] = "video"
    elif message.document:    _bc[uid]["type"] = "document"
    elif message.audio:       _bc[uid]["type"] = "audio"
    elif message.voice:       _bc[uid]["type"] = "voice"
    elif message.sticker:     _bc[uid]["type"] = "sticker"
    elif message.animation:   _bc[uid]["type"] = "animation"
    elif message.video_note:  _bc[uid]["type"] = "video_note"
    else:                     _bc[uid]["type"] = "text"

    await state.set_state(BroadcastStates.waiting_button)
    await message.answer(
        "✅ Сообщение получено!\n\nДобавить инлайн-кнопку?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ Добавить кнопку", callback_data="bc_add_button")],
            [InlineKeyboardButton(text="⏩ Без кнопки",      callback_data="bc_no_button")],
            [InlineKeyboardButton(text="❌ Отмена",          callback_data="bc_cancel")],
        ])
    )


@router.callback_query(F.data == "bc_add_button")
async def bc_ask_button(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    await callback.message.edit_text(
        "Введите текст кнопки и ссылку через <code>|</code>\n\n"
        "Пример:\n<code>🔥 Купить VPN | https://t.me/your_bot</code>",
        parse_mode="HTML"
    )
    await callback.answer()


@router.message(BroadcastStates.waiting_button, F.text)
async def bc_receive_button(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    uid = message.from_user.id
    if "|" not in message.text:
        await message.answer(
            "❌ Формат: <code>Текст кнопки | https://ссылка</code>",
            parse_mode="HTML"
        )
        return
    btn_text, btn_url = [p.strip() for p in message.text.split("|", 1)]
    if not btn_url.startswith("http"):
        await message.answer("❌ Ссылка должна начинаться с http:// или https://")
        return
    _bc[uid]["button_text"] = btn_text
    _bc[uid]["button_url"]  = btn_url
    await _show_preview(message, uid, state)


@router.callback_query(F.data == "bc_no_button")
async def bc_no_button(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    uid = callback.from_user.id
    _bc[uid].pop("button_text", None)
    _bc[uid].pop("button_url", None)
    await _show_preview(callback.message, uid, state, edit=True)
    await callback.answer()


# ── Предпросмотр ──────────────────────────────────────────────────────────────

async def _show_preview(message, uid: int, state: FSMContext, edit=False):
    data = _bc.get(uid, {})
    await state.set_state(BroadcastStates.confirm)

    async with AsyncSessionLocal() as session:
        if data.get("target") == "active":
            result = await session.execute(
                select(User.id)
                .join(Subscription, Subscription.user_id == User.id)
                .where(Subscription.status == "active")
                .where(Subscription.expires_at > datetime.now())
                .where(User.is_banned == False)
                .distinct()
            )
        else:
            result = await session.execute(
                select(User.id).where(User.is_banned == False)
            )
        user_ids = result.scalars().all()

    target_label = "всем пользователям" if data.get("target") == "all" else "активным подписчикам"
    btn_info = f"\n🔘 Кнопка: <b>{data['button_text']}</b>" if data.get("button_text") else ""

    text = (
        f"📢 <b>Предпросмотр рассылки</b>\n\n"
        f"Кому: <b>{target_label}</b>\n"
        f"Получателей: <b>{len(user_ids)}</b>\n"
        f"Тип: <b>{data.get('type', 'text')}</b>{btn_info}\n\n"
        f"Подтвердить отправку?"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"📤 Отправить ({len(user_ids)} чел.)", callback_data="bc_send")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="bc_cancel")],
    ])
    if edit:
        await message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    else:
        await message.answer(text, reply_markup=kb, parse_mode="HTML")


# ── Отправка ──────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "bc_send")
async def bc_send(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return

    uid  = callback.from_user.id
    data = _bc.get(uid, {})
    await state.clear()

    from_chat_id = data.get("from_chat_id")
    message_id   = data.get("message_id")

    if not from_chat_id or not message_id:
        await callback.message.edit_text("❌ Сообщение для рассылки не найдено. Начните заново.")
        await callback.answer()
        return

    # Получаем пользователей
    async with AsyncSessionLocal() as session:
        if data.get("target") == "active":
            result = await session.execute(
                select(User.id)
                .join(Subscription, Subscription.user_id == User.id)
                .where(Subscription.status == "active")
                .where(Subscription.expires_at > datetime.now())
                .where(User.is_banned == False)
                .distinct()
            )
        else:
            result = await session.execute(
                select(User.id).where(User.is_banned == False)
            )
        user_ids = list(result.scalars().all())

    # Кнопка
    reply_markup = None
    if data.get("button_text") and data.get("button_url"):
        reply_markup = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=data["button_text"], url=data["button_url"])]
        ])

    await callback.message.edit_text(
        f"⏳ Отправляю рассылку {len(user_ids)} пользователям...",
    )
    await callback.answer()

    sent = 0
    failed = 0
    bot: Bot = callback.bot

    for user_id in user_ids:
        try:
            # copy_message сохраняет премиум эмодзи, форматирование, медиа — всё как есть
            await bot.copy_message(
                chat_id=user_id,
                from_chat_id=from_chat_id,
                message_id=message_id,
                reply_markup=reply_markup,
            )
            sent += 1
        except Exception as e:
            failed += 1
            logger.debug(f"Broadcast failed for {user_id}: {e}")

        # Антифлуд: ~25 сообщений/сек
        if (sent + failed) % 25 == 0:
            await asyncio.sleep(1)

    _bc.pop(uid, None)

    # Сохраняем в историю
    async with AsyncSessionLocal() as session:
        session.add(Broadcast(
            admin_id=uid,
            target=data.get("target", "all"),
            msg_type=data.get("type", "text"),
            sent_count=sent,
            failed_count=failed,
        ))
        await session.commit()

    await callback.message.edit_text(
        f"✅ <b>Рассылка завершена!</b>\n\n"
        f"📤 Отправлено: <b>{sent}</b>\n"
        f"❌ Не доставлено: <b>{failed}</b>",
        parse_mode="HTML"
    )


@router.callback_query(F.data == "bc_cancel")
async def bc_cancel(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    _bc.pop(callback.from_user.id, None)
    await state.clear()
    await callback.message.edit_text("❌ Рассылка отменена")
    await callback.answer()
