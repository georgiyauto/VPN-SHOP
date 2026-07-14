"""
bot/handlers/support_handler.py

💬 Тикет-система поддержки.

Пользователь:
  /support Текст проблемы   — создать тикет
  callback: support_ticket_close:{id}  — закрыть свой тикет

Чат поддержки (SUPPORT_CHAT_ID):
  Бот пересылает сообщение с кнопкой "Ответить"
  callback: ticket_reply:{ticket_id}  — ввести ответ (FSM)
  callback: ticket_close:{ticket_id}  — закрыть тикет
"""
import os
import logging
from datetime import datetime

from aiogram import Router, F, Bot
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from sqlalchemy import select

from db.database import AsyncSessionLocal
from db.models import User, SupportTicket, Settings
from utils.i18n import t

router = Router()


async def _get_allowed_ids() -> list[int]:
    """Возвращает список ID, которые могут отвечать на тикеты (admins + operators из БД)."""
    ids = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(select(Settings).where(Settings.id == 1))
            s = result.scalar_one_or_none()
            if s and s.support_operator_ids:
                for x in s.support_operator_ids.split(","):
                    x = x.strip()
                    if x.isdigit():
                        ids.append(int(x))
    except Exception:
        pass
    return list(set(ids))
logger = logging.getLogger(__name__)


class SupportReplyStates(StatesGroup):
    waiting_reply = State()   # admin вводит текст ответа, ticket_id в data


class NewTicketStates(StatesGroup):
    waiting_text = State()    # user вводит текст нового тикета


async def _get_support_chat_id(session) -> str | None:
    from sqlalchemy import select
    result = await session.execute(select(Settings).where(Settings.id == 1))
    settings = result.scalar_one_or_none()
    if settings:
        return settings.support_chat_id
    return os.getenv("SUPPORT_CHAT_ID")




# ── Пользователь нажал "Написать тикет" → ждём следующее сообщение ───────────

@router.callback_query(F.data == "support_new_ticket_fsm")
async def support_new_ticket_fsm(callback: CallbackQuery, state: FSMContext):
    await state.set_state(NewTicketStates.waiting_text)
    await callback.answer()


@router.message(NewTicketStates.waiting_text)
async def receive_new_ticket(message: Message, bot: Bot, state: FSMContext):
    text = message.text.strip() if message.text else ""
    if not text:
        await message.answer("❌ Пожалуйста, отправьте текстовое сообщение.")
        return

    await state.clear()

    async with AsyncSessionLocal() as session:
        user = await session.get(User, message.from_user.id)
        if not user:
            await message.answer("❌ Сначала напиши /start")
            return

        ticket = SupportTicket(user_id=user.id, text=text, status="open")
        session.add(ticket)
        await session.flush()
        support_chat_id = await _get_support_chat_id(session)
        await session.commit()
        await session.refresh(ticket)

    await message.answer(
        f"✅ <b>Тикет #{ticket.id} создан</b>\n\n"
        f"Ваш вопрос:\n<i>{text[:300]}</i>\n\n"
        f"Мы ответим вам в ближайшее время.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Закрыть тикет", callback_data=f"support_ticket_close:{ticket.id}")]
        ])
    )

    # Пересылаем в чат поддержки или админам
    if support_chat_id:
        try:
            username_str = f"@{user.username}" if user.username else f"id:{user.id}"
            await bot.send_message(
                support_chat_id,
                f"🎫 <b>Новый тикет #{ticket.id}</b>\n\n"
                f"👤 {user.full_name} ({username_str})\n"
                f"🆔 <code>{user.id}</code>\n\n"
                f"📝 <b>Проблема:</b>\n{text}",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="💬 Ответить", callback_data=f"ticket_reply:{ticket.id}"),
                    InlineKeyboardButton(text="✅ Закрыть",  callback_data=f"ticket_close:{ticket.id}"),
                ]])
            )
        except Exception as e:
            logger.error(f"Failed to forward ticket {ticket.id}: {e}")
    else:
        admin_ids = await _get_allowed_ids()
        for admin_id in admin_ids:
            try:
                await bot.send_message(
                    admin_id,
                    f"🎫 <b>Тикет #{ticket.id}</b> от {user.full_name} (id:{user.id})\n\n{text}",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                        InlineKeyboardButton(text="💬 Ответить", callback_data=f"ticket_reply:{ticket.id}")
                    ]])
                )
            except Exception:
                pass

# ── /support Текст ────────────────────────────────────────────────────────────

@router.message(Command("support"))
async def cmd_support(message: Message, bot: Bot):
    args = message.text.strip().split(maxsplit=1)
    if len(args) < 2 or not args[1].strip():
        await message.answer(
            "💬 <b>Поддержка</b>\n\n"
            "Опишите вашу проблему одним сообщением:\n\n"
            "<code>/support Текст вашей проблемы</code>",
            parse_mode="HTML"
        )
        return

    text = args[1].strip()

    async with AsyncSessionLocal() as session:
        user = await session.get(User, message.from_user.id)
        if not user:
            await message.answer("❌ Сначала напиши /start")
            return
        lang = user.language or "ru"

        # Создаём тикет
        ticket = SupportTicket(
            user_id=user.id,
            text=text,
            status="open"
        )
        session.add(ticket)
        await session.flush()

        support_chat_id = await _get_support_chat_id(session)
        await session.commit()
        await session.refresh(ticket)

    # Подтверждение пользователю
    await message.answer(
        f"✅ <b>Тикет #{ticket.id} создан</b>\n\n"
        f"Ваш вопрос:\n<i>{text[:300]}</i>\n\n"
        f"Мы ответим вам в ближайшее время.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Закрыть тикет", callback_data=f"support_ticket_close:{ticket.id}")]
        ])
    )

    # Пересылаем в чат поддержки
    if support_chat_id:
        try:
            username_str = f"@{user.username}" if user.username else f"id:{user.id}"
            admin_text = (
                f"🎫 <b>Новый тикет #{ticket.id}</b>\n\n"
                f"👤 Пользователь: {user.full_name} ({username_str})\n"
                f"🆔 Telegram ID: <code>{user.id}</code>\n\n"
                f"📝 <b>Проблема:</b>\n{text}"
            )
            sent = await bot.send_message(
                support_chat_id,
                admin_text,
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [
                        InlineKeyboardButton(text="💬 Ответить", callback_data=f"ticket_reply:{ticket.id}"),
                        InlineKeyboardButton(text="✅ Закрыть", callback_data=f"ticket_close:{ticket.id}"),
                    ]
                ])
            )
            # Сохраняем msg_id для ссылки на оригинал
            async with AsyncSessionLocal() as session:
                t_obj = await session.get(SupportTicket, ticket.id)
                if t_obj:
                    t_obj.forwarded_msg_id = sent.message_id
                    await session.commit()
        except Exception as e:
            logger.error(f"Failed to forward ticket {ticket.id} to support chat: {e}")
    else:
        # Нет чата поддержки — шлём всем админам
        admin_ids = await _get_allowed_ids()
        for admin_id in admin_ids:
            try:
                await bot.send_message(
                    admin_id,
                    f"🎫 <b>Тикет #{ticket.id}</b> от {user.full_name} (id:{user.id})\n\n{text}",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="💬 Ответить", callback_data=f"ticket_reply:{ticket.id}")]
                    ])
                )
            except Exception:
                pass


# ── Пользователь закрывает свой тикет ────────────────────────────────────────

@router.callback_query(F.data.startswith("support_ticket_close:"))
async def user_close_ticket(callback: CallbackQuery):
    ticket_id = int(callback.data.split(":")[1])
    async with AsyncSessionLocal() as session:
        ticket = await session.get(SupportTicket, ticket_id)
        if not ticket or ticket.user_id != callback.from_user.id:
            await callback.answer("Тикет не найден", show_alert=True)
            return
        ticket.status = "closed"
        await session.commit()
    await callback.message.edit_text(
        f"✅ Тикет #{ticket_id} закрыт.",
        reply_markup=None
    )


# ── Админ нажимает «Ответить» ─────────────────────────────────────────────────

@router.callback_query(F.data.startswith("ticket_reply:"))
async def admin_start_reply(callback: CallbackQuery, state: FSMContext):
    ticket_id = int(callback.data.split(":")[1])
    allowed_ids = await _get_allowed_ids()
    support_chat = os.getenv("SUPPORT_CHAT_ID", "")

    # Доступ: операторы/admins или из чата поддержки
    is_admin = callback.from_user.id in allowed_ids
    is_support_chat = str(callback.message.chat.id) == str(support_chat)

    if not is_admin and not is_support_chat:
        await callback.answer("Нет доступа", show_alert=True)
        return

    await state.set_state(SupportReplyStates.waiting_reply)
    await state.update_data(ticket_id=ticket_id, admin_id=callback.from_user.id)
    await callback.message.answer(
        f"✍️ Введите ответ на тикет #{ticket_id}:\n"
        f"(или /cancel для отмены)"
    )
    await callback.answer()


@router.message(SupportReplyStates.waiting_reply)
async def admin_send_reply(message: Message, state: FSMContext, bot: Bot):
    if message.text and message.text.strip() == "/cancel":
        await state.clear()
        await message.answer("❌ Отменено")
        return

    data = await state.get_data()
    ticket_id = data.get("ticket_id")
    admin_id = data.get("admin_id")
    reply_text = message.text or ""

    async with AsyncSessionLocal() as session:
        ticket = await session.get(SupportTicket, ticket_id)
        if not ticket:
            await message.answer("❌ Тикет не найден")
            await state.clear()
            return

        user_id = ticket.user_id
        ticket.status = "answered"
        ticket.admin_id = admin_id
        ticket.answer = reply_text
        ticket.answered_at = datetime.now()
        await session.commit()

    # Отправляем ответ пользователю
    try:
        await bot.send_message(
            user_id,
            f"💬 <b>Ответ на ваш тикет #{ticket_id}</b>\n\n"
            f"{reply_text}\n\n"
            f"<i>Если вопрос не решён, используйте /support снова.</i>",
            parse_mode="HTML"
        )
        await message.answer(f"✅ Ответ на тикет #{ticket_id} отправлен пользователю!")
    except Exception as e:
        await message.answer(f"⚠️ Не удалось доставить ответ пользователю: {e}")

    await state.clear()


# ── Админ закрывает тикет ─────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("ticket_close:"))
async def admin_close_ticket(callback: CallbackQuery, bot: Bot):
    ticket_id = int(callback.data.split(":")[1])
    allowed_ids = await _get_allowed_ids()
    support_chat = os.getenv("SUPPORT_CHAT_ID", "")

    is_admin = callback.from_user.id in allowed_ids
    is_support_chat = str(callback.message.chat.id) == str(support_chat)

    if not is_admin and not is_support_chat:
        await callback.answer("Нет доступа", show_alert=True)
        return

    async with AsyncSessionLocal() as session:
        ticket = await session.get(SupportTicket, ticket_id)
        if not ticket:
            await callback.answer("Тикет не найден", show_alert=True)
            return
        ticket.status = "closed"
        await session.commit()
        user_id = ticket.user_id

    try:
        await bot.send_message(
            user_id,
            f"✅ Ваш тикет #{ticket_id} закрыт администратором.\n"
            f"Если остались вопросы — используйте /support"
        )
    except Exception:
        pass

    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.answer(f"✅ Тикет #{ticket_id} закрыт", show_alert=True)
