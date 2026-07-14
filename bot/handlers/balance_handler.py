"""
Баланс пользователей — просмотр, пополнение через поддержку, оплата балансом.
"""
import os
import logging
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.filters import Command
from sqlalchemy import select

from db.database import AsyncSessionLocal
from db.models import User, BalanceLog

router = Router()
logger = logging.getLogger(__name__)


def is_admin(user_id: int) -> bool:
    admin_ids = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
    return user_id in admin_ids


class BalanceTopupStates(StatesGroup):
    waiting_amount = State()


# ── Просмотр баланса ──────────────────────────────────────────────────────────

@router.callback_query(F.data == "my_balance")
async def show_balance(callback: CallbackQuery):
    async with AsyncSessionLocal() as session:
        user = await session.get(User, callback.from_user.id)
        logs_result = await session.execute(
            select(BalanceLog)
            .where(BalanceLog.user_id == user.id)
            .order_by(BalanceLog.created_at.desc())
            .limit(5)
        )
        logs = logs_result.scalars().all()

    history = ""
    if logs:
        history = "\n\n📋 <b>Последние операции:</b>\n"
        for log in logs:
            sign = "+" if log.amount > 0 else ""
            dt = log.created_at.strftime("%d.%m") if log.created_at else "—"
            history += f"• {sign}{log.amount:.0f}₽ — {log.comment or '—'} ({dt})\n"

    await callback.message.edit_text(
        f"💰 <b>Ваш баланс: {user.balance:.0f} ₽</b>{history}\n"
        f"<i>Баланс можно использовать для оплаты тарифов</i>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ Пополнить через поддержку", callback_data="balance_topup_request")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="back_main")],
        ]),
        parse_mode="HTML"
    )
    await callback.answer()


# ── Пополнение через поддержку ────────────────────────────────────────────────

@router.callback_query(F.data == "balance_topup_request")
async def balance_topup_request(callback: CallbackQuery, state: FSMContext):
    async with AsyncSessionLocal() as session:
        user = await session.get(User, callback.from_user.id)
        from db.models import Settings
        result = await session.execute(select(Settings).where(Settings.id == 1))
        settings = result.scalar_one()
        support = settings.support_username or "support"

    await state.set_state(BalanceTopupStates.waiting_amount)
    await callback.message.edit_text(
        f"💰 <b>Пополнение баланса</b>\n\n"
        f"Введите сумму пополнения (в рублях):\n\n"
        f"<i>После этого вы получите реквизиты и свяжетесь с @{support}</i>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Отмена", callback_data="my_balance")]
        ])
    )
    await callback.answer()


@router.message(BalanceTopupStates.waiting_amount)
async def balance_topup_amount(message: Message, state: FSMContext):
    try:
        amount = float(message.text.strip().replace(",", ".").replace(" ", ""))
        if amount <= 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ Введите корректную сумму числом, например: <b>500</b>", parse_mode="HTML")
        return

    await state.clear()

    async with AsyncSessionLocal() as session:
        user = await session.get(User, message.from_user.id)
        from db.models import Settings
        result = await session.execute(select(Settings).where(Settings.id == 1))
        settings = result.scalar_one()
        support = settings.support_username or "support"
        support_chat_id = settings.support_chat_id

    user_info = f"@{user.username}" if user.username else f"ID {user.id}"

    await message.answer(
        f"✅ <b>Заявка создана!</b>\n\n"
        f"💵 Сумма: <b>{amount:.0f} ₽</b>\n\n"
        f"Для завершения пополнения:\n"
        f"1. Напишите в поддержку @{support}\n"
        f"2. Укажите ваш ID: <code>{user.id}</code> и сумму <b>{amount:.0f}₽</b>\n"
        f"3. После перевода администратор начислит баланс",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"📩 Написать @{support}", url=f"https://t.me/{support}")],
            [InlineKeyboardButton(text="◀️ В меню", callback_data="back_main")],
        ])
    )

    # Уведомляем чат поддержки
    if support_chat_id:
        try:
            await message.bot.send_message(
                support_chat_id,
                f"💰 <b>Заявка на пополнение баланса</b>\n\n"
                f"👤 {user_info} (ID: <code>{user.id}</code>)\n"
                f"💵 Сумма: <b>{amount:.0f} ₽</b>\n\n"
                f"Команда для начисления:\n"
                f"<code>/addbalance {user.id} {amount:.0f}</code>",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(
                        text=f"✅ Начислить {amount:.0f}₽",
                        callback_data=f"admin_topup:{user.id}:{int(amount)}"
                    )],
                ])
            )
        except Exception as e:
            logger.warning(f"Failed to notify support chat: {e}")


@router.callback_query(F.data.startswith("admin_topup:"))
async def admin_topup_confirm(callback: CallbackQuery):
    """Кнопка подтверждения пополнения в чате поддержки."""
    parts = callback.data.split(":")
    user_id = int(parts[1])
    amount = float(parts[2])

    new_balance = await change_balance_direct(
        user_id, amount,
        f"Пополнение подтверждено (@{callback.from_user.username or callback.from_user.id})"
    )

    await callback.answer(f"✅ Начислено {amount:.0f}₽ → баланс {new_balance:.0f}₽", show_alert=True)
    try:
        await callback.message.edit_text(
            callback.message.text + f"\n\n✅ <b>Начислено!</b> Новый баланс: <b>{new_balance:.0f}₽</b>",
            parse_mode="HTML"
        )
    except Exception:
        pass

    try:
        await callback.bot.send_message(
            user_id,
            f"✅ <b>Баланс пополнен!</b>\n\n"
            f"Зачислено: <b>+{amount:.0f} ₽</b>\n"
            f"Текущий баланс: <b>{new_balance:.0f} ₽</b>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="💎 Купить подписку", callback_data="buy")]
            ])
        )
    except Exception:
        pass


# ── Оплата балансом ───────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("pay_balance:"))
async def pay_with_balance(callback: CallbackQuery):
    plan_id = int(callback.data.split(":")[1])
    async with AsyncSessionLocal() as session:
        user = await session.get(User, callback.from_user.id)
        from db.models import Plan, Settings
        plan = await session.get(Plan, plan_id)
        result = await session.execute(select(Settings).where(Settings.id == 1))
        settings = result.scalar_one()
        lang = user.language or "ru"

    if not plan:
        await callback.answer("Тариф не найден", show_alert=True)
        return

    balance = user.balance or 0
    need = plan.price_rub

    # ── Недостаточно средств → страница пополнения ────────────────────────────
    if balance < need:
        shortage = need - balance
        support = settings.support_username or "support"

        topup_rows = []
        if settings.manual_payment_enabled:
            topup_rows.append([InlineKeyboardButton(
                text="📩 Пополнить через поддержку",
                callback_data=f"topup_exact:{plan_id}:{need:.0f}"
            )])
        if settings.heleket_enabled:
            topup_rows.append([InlineKeyboardButton(
                text="💰 Пополнить криптой (Heleket)",
                callback_data=f"topup_heleket:{plan_id}"
            )])
        if settings.cryptopay_enabled:
            topup_rows.append([InlineKeyboardButton(
                text="💎 Пополнить через CryptoPay",
                callback_data=f"topup_cryptopay:{plan_id}"
            )])
        if settings.card_link_enabled and settings.card_link_url:
            topup_rows.append([InlineKeyboardButton(
                text=settings.card_link_text or "💳 Пополнить картой",
                url=settings.card_link_url
            )])
        topup_rows.append([InlineKeyboardButton(
            text="◀️ Назад к тарифу", callback_data=f"plan:{plan_id}"
        )])

        await callback.message.edit_text(
            f"💰 <b>Недостаточно средств</b>\n\n"
            f"📦 Тариф: <b>{plan.name}</b>\n"
            f"💵 Стоимость: <b>{need:.0f} ₽</b>\n"
            f"💳 Ваш баланс: <b>{balance:.0f} ₽</b>\n"
            f"❗ Не хватает: <b>{shortage:.0f} ₽</b>\n\n"
            f"Выберите способ пополнения:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=topup_rows),
            parse_mode="HTML"
        )
        await callback.answer()
        return

    # ── Достаточно → списываем и активируем ──────────────────────────────────
    async with AsyncSessionLocal() as session:
        user = await session.get(User, callback.from_user.id)
        user.balance -= plan.price_rub
        session.add(BalanceLog(user_id=user.id, amount=-plan.price_rub, comment=f"Оплата тарифа {plan.name}"))
        await session.commit()
        new_balance = user.balance

    from bot.services.vpn_service import activate_subscription
    ok = await activate_subscription(callback.from_user.id, plan_id)

    if ok:
        import re
        from bot.keyboards.keyboards import subscription_kb
        from bot.handlers.payment_handlers import _notify_admins_payment
        async with AsyncSessionLocal() as session:
            user = await session.get(User, callback.from_user.id)
            from db.models import Settings
            result = await session.execute(select(Settings).where(Settings.id == 1))
            settings = result.scalar_one()

        bot_domain = re.sub(r":\d+$", "", os.getenv("BOT_DOMAIN", "").rstrip("/")) + ":8433"
        sub_url = f"{bot_domain}/sub/{user.sub_token}"

        await callback.message.edit_text(
            f"✅ <b>Оплата прошла успешно!</b>\n\n"
            f"📦 Тариф: <b>{plan.name}</b>\n"
            f"💰 Списано: <b>{plan.price_rub:.0f} ₽</b>\n"
            f"💳 Остаток: <b>{new_balance:.0f} ₽</b>\n\n"
            f"{settings.sub_issued_text or ''}",
            reply_markup=subscription_kb(sub_url, lang),
            parse_mode="HTML"
        )
        # Реферальное вознаграждение за оплату с баланса
        try:
            from utils.referral import accrue_referral_reward
            await accrue_referral_reward(
                payer_user_id=callback.from_user.id,
                amount_rub=plan.price_rub,
                source_label=f"тариф {plan.name} (с баланса)",
                bot=callback.bot,
            )
        except Exception as _ref_err:
            logger.warning(f"Referral accrual error (balance pay): {_ref_err}")

        # Уведомляем всех админов
        await _notify_admins_payment(callback.bot, user, plan.price_rub, "С баланса", plan.name)
    else:
        async with AsyncSessionLocal() as session:
            user = await session.get(User, callback.from_user.id)
            user.balance += plan.price_rub
            session.add(BalanceLog(user_id=user.id, amount=plan.price_rub, comment="Возврат — ошибка активации"))
            await session.commit()
        await callback.answer("❌ Ошибка активации, баланс возвращён", show_alert=True)
    await callback.answer()


@router.callback_query(F.data.startswith("topup_exact:"))
async def topup_exact_amount(callback: CallbackQuery):
    """Заявка на пополнение точной суммой через поддержку."""
    parts = callback.data.split(":")
    plan_id = int(parts[1])
    amount = float(parts[2])

    async with AsyncSessionLocal() as session:
        user = await session.get(User, callback.from_user.id)
        from db.models import Plan, Settings
        plan = await session.get(Plan, plan_id)
        result = await session.execute(select(Settings).where(Settings.id == 1))
        settings = result.scalar_one()
        support = settings.support_username or "support"
        support_chat_id = settings.support_chat_id

    user_info = f"@{user.username}" if user.username else f"ID {user.id}"

    await callback.message.edit_text(
        f"✅ <b>Заявка создана!</b>\n\n"
        f"💵 Сумма пополнения: <b>{amount:.0f} ₽</b>\n\n"
        f"1. Напишите в поддержку @{support}\n"
        f"2. Укажите ваш ID: <code>{user.id}</code>\n"
        f"3. После перевода администратор зачислит баланс\n"
        f"4. После пополнения нажмите «💰 Оплатить с баланса»",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"📩 Написать @{support}", url=f"https://t.me/{support}")],
            [InlineKeyboardButton(text="◀️ К тарифу", callback_data=f"plan:{plan_id}")],
        ])
    )

    if support_chat_id:
        try:
            await callback.bot.send_message(
                support_chat_id,
                f"💰 <b>Заявка на пополнение баланса</b>\n\n"
                f"👤 {user_info} (ID: <code>{user.id}</code>)\n"
                f"💵 Сумма: <b>{amount:.0f} ₽</b>\n"
                f"🎯 Цель: оплата тарифа «{plan.name}»\n\n"
                f"<code>/addbalance {user.id} {amount:.0f}</code>",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(
                        text=f"✅ Начислить {amount:.0f}₽",
                        callback_data=f"admin_topup:{user.id}:{int(amount)}"
                    )],
                ])
            )
        except Exception as e:
            logger.warning(f"Failed to notify support: {e}")
    await callback.answer()


@router.callback_query(F.data.startswith("topup_heleket:"))
async def topup_via_heleket(callback: CallbackQuery):
    plan_id = int(callback.data.split(":")[1])
    callback.data = f"pay_heleket:{plan_id}"
    from bot.handlers.payment_handlers import pay_heleket
    await pay_heleket(callback)


@router.callback_query(F.data.startswith("topup_cryptopay:"))
async def topup_via_cryptopay(callback: CallbackQuery):
    plan_id = int(callback.data.split(":")[1])
    callback.data = f"pay_cryptopay:{plan_id}"
    from bot.handlers.payment_handlers import pay_cryptopay
    await pay_cryptopay(callback)


# ── Админ команды ─────────────────────────────────────────────────────────────

@router.message(Command("addbalance"))
async def admin_add_balance(message: Message):
    if not is_admin(message.from_user.id):
        return
    try:
        parts = message.text.split()
        user_id = int(parts[1])
        amount = float(parts[2])
        comment = " ".join(parts[3:]) if len(parts) > 3 else "Пополнение администратором"
        new_balance = await change_balance_direct(user_id, amount, comment)
        await message.answer(f"✅ Начислено {amount:.0f}₽ → баланс: {new_balance:.0f}₽")
        try:
            await message.bot.send_message(
                user_id,
                f"💰 <b>Баланс пополнен</b>\n\n+{amount:.0f} ₽\nИтого: <b>{new_balance:.0f} ₽</b>",
                parse_mode="HTML"
            )
        except Exception:
            pass
    except (IndexError, ValueError):
        await message.answer("Использование: /addbalance <user_id> <сумма> [комментарий]")


@router.message(Command("subbalance"))
async def admin_sub_balance(message: Message):
    if not is_admin(message.from_user.id):
        return
    try:
        parts = message.text.split()
        user_id = int(parts[1])
        amount = float(parts[2])
        comment = " ".join(parts[3:]) if len(parts) > 3 else "Списание администратором"
        new_balance = await change_balance_direct(user_id, -amount, comment)
        await message.answer(f"✅ Списано {amount:.0f}₽ → баланс: {new_balance:.0f}₽")
    except (IndexError, ValueError):
        await message.answer("Использование: /subbalance <user_id> <сумма>")


async def change_balance_direct(user_id: int, amount: float, comment: str = "") -> float:
    async with AsyncSessionLocal() as session:
        user = await session.get(User, user_id)
        if not user:
            return 0.0
        user.balance = (user.balance or 0) + amount
        session.add(BalanceLog(user_id=user_id, amount=amount, comment=comment))
        await session.commit()
        return user.balance
