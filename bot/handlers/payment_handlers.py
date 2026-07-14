import re
import os
import uuid
import logging
from datetime import datetime
from aiogram import Router, F
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from sqlalchemy import select

from db.database import AsyncSessionLocal
from db.models import Plan, Settings, Payment, User
from bot.services.payment_service import create_payment_record, create_heleket_invoice, create_cryptopay_invoice
from bot.services.sbp_service import create_sbp_invoice
from bot.services.vpn_service import activate_subscription
from bot.keyboards.keyboards import plans_kb, payment_methods_kb, payment_waiting_kb, support_contact_kb, subscription_kb
from utils.i18n import t

router = Router()
logger = logging.getLogger(__name__)


def _admin_ids() -> list[int]:
    return [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]


def _bot_domain() -> str:
    base = re.sub(r":\d+$", "", os.getenv("BOT_DOMAIN", "").rstrip("/"))
    return base + ":8433"


async def _get_settings(session) -> Settings:
    result = await session.execute(select(Settings).where(Settings.id == 1))
    return result.scalar_one()


async def _notify_admins_payment(bot, user: User, amount: float, method: str, plan_name: str):
    uname = f"@{user.username}" if user.username else f"ID {user.id}"
    text = (
        f"✅ <b>Новая оплата!</b>\n\n"
        f"👤 Пользователь: <b>{uname}</b>\n"
        f"📦 Тариф: <b>{plan_name}</b>\n"
        f"💰 Сумма: <b>{amount:.0f} ₽</b>\n"
        f"💳 Способ: <b>{method}</b>\n"
        f"🕐 {datetime.now().strftime('%d.%m.%Y %H:%M')}"
    )
    for admin_id in _admin_ids():
        try:
            await bot.send_message(admin_id, text, parse_mode="HTML")
        except Exception as e:
            logger.warning(f"Не удалось уведомить админа {admin_id}: {e}")


async def _notify_admins_topup(bot, user: User, amount: float, method: str):
    uname = f"@{user.username}" if user.username else f"ID {user.id}"
    text = (
        f"💰 <b>Пополнение баланса!</b>\n\n"
        f"👤 Пользователь: <b>{uname}</b>\n"
        f"💵 Сумма: <b>+{amount:.0f} ₽</b>\n"
        f"💳 Способ: <b>{method}</b>\n"
        f"🕐 {datetime.now().strftime('%d.%m.%Y %H:%M')}"
    )
    for admin_id in _admin_ids():
        try:
            await bot.send_message(admin_id, text, parse_mode="HTML")
        except Exception as e:
            logger.warning(f"Не удалось уведомить админа {admin_id}: {e}")


async def show_payment_methods(message_or_cb, plan_id: int, plan, price: float,
                               lang: str = "ru", edit: bool = False,
                               discount_label: str = ""):
    async with AsyncSessionLocal() as session:
        settings = await _get_settings(session)
        uid = getattr(message_or_cb, "from_user", None)
        uid = uid.id if uid else None
        balance = 0
        if uid:
            from sqlalchemy import select as sa_select
            result = await session.execute(sa_select(User.balance).where(User.id == uid))
            row = result.first()
            balance = float(row[0] or 0) if row else 0

    traffic = f"{plan.traffic_gb} GB" if plan.traffic_gb else t("unlimited", lang)
    devices = getattr(plan, "max_devices", 1)
    price_label = (
        f"<s>{plan.price_rub}</s>₽ → <b>{price:.0f}₽</b>" if discount_label
        else f"<b>{price:.0f}₽</b>"
    )
    text = (
        f"📦 <b>{plan.name}</b>\n\n"
        f"💰 Стоимость: {price_label}\n"
        f"⏳ Срок: <b>{plan.duration_days} дн.</b>\n"
        f"📊 Трафик: <b>{traffic}</b>\n"
        f"📱 Устройств: <b>{devices}</b>\n\n"
        f"{t('choose_payment', lang)}"
    )
    kb = payment_methods_kb(plan_id, settings, lang, balance, price, discount_label)
    if edit:
        msg = getattr(message_or_cb, "message", message_or_cb)
        await msg.edit_text(text, reply_markup=kb, parse_mode="HTML")
    else:
        target = getattr(message_or_cb, "message", message_or_cb)
        await target.answer(text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data == "buy")
async def show_plans(callback: CallbackQuery):
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Plan).where(Plan.is_active == True).order_by(Plan.sort_order)
        )
        plans = result.scalars().all()
        settings = await _get_settings(session)
        user = await session.get(User, callback.from_user.id)
        lang = (user.language or "ru")

    if not plans:
        await callback.answer("Тарифы не настроены", show_alert=True)
        return
    await callback.message.edit_text(
        t("choose_plan", lang),
        reply_markup=plans_kb(plans, settings, lang),
        parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data.startswith("plan:"))
async def select_plan(callback: CallbackQuery):
    plan_id = int(callback.data.split(":")[1])
    async with AsyncSessionLocal() as session:
        plan = await session.get(Plan, plan_id)
        user = await session.get(User, callback.from_user.id)
        lang = (user.language or "ru")
    if not plan:
        await callback.answer("Тариф не найден", show_alert=True)
        return
    await show_payment_methods(callback, plan_id, plan, plan.price_rub, lang, edit=True)
    await callback.answer()


@router.callback_query(F.data.startswith("pay_manual:"))
async def pay_manual(callback: CallbackQuery):
    plan_id = int(callback.data.split(":")[1])
    async with AsyncSessionLocal() as session:
        plan = await session.get(Plan, plan_id)
        settings = await _get_settings(session)
        user = await session.get(User, callback.from_user.id)
        lang = (user.language or "ru")
    if not plan or not settings.manual_payment_enabled:
        await callback.answer("Способ оплаты недоступен", show_alert=True)
        return
    text = settings.manual_payment_text.format(
        amount=plan.price_rub, support=settings.support_username,
    )
    await callback.message.edit_text(
        text,
        reply_markup=support_contact_kb(settings.support_username, plan_id, plan.price_rub),
        parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data.startswith("pay_heleket:"))
async def pay_heleket(callback: CallbackQuery):
    plan_id = int(callback.data.split(":")[1])
    async with AsyncSessionLocal() as session:
        plan = await session.get(Plan, plan_id)
        settings = await _get_settings(session)
        user = await session.get(User, callback.from_user.id)
        lang = (user.language or "ru")
        if not settings.heleket_enabled:
            await callback.answer("Недоступно", show_alert=True)
            return
        payment = await create_payment_record(session, callback.from_user.id, plan_id, "heleket", plan.price_rub)
        await session.commit()

    invoice = await create_heleket_invoice(plan.price_rub, str(payment.id), f"VPN {plan.name}")
    if not invoice:
        await callback.answer("Ошибка создания платежа", show_alert=True)
        return
    await callback.message.edit_text(
        f"💰 <b>Оплата через Heleket</b>\n\nТариф: <b>{plan.name}</b>\nСумма: <b>{plan.price_rub}₽</b>",
        reply_markup=payment_waiting_kb(invoice["url"], plan_id, lang),
        parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data.startswith("pay_cryptopay:"))
async def pay_cryptopay(callback: CallbackQuery):
    plan_id = int(callback.data.split(":")[1])
    async with AsyncSessionLocal() as session:
        plan = await session.get(Plan, plan_id)
        settings = await _get_settings(session)
        user = await session.get(User, callback.from_user.id)
        lang = (user.language or "ru")
        if not settings.cryptopay_enabled:
            await callback.answer("Недоступно", show_alert=True)
            return
        payment = await create_payment_record(session, callback.from_user.id, plan_id, "cryptopay", plan.price_rub)
        await session.commit()

    invoice = await create_cryptopay_invoice(plan.price_rub, str(payment.id), f"VPN {plan.name}")
    if not invoice:
        await callback.answer("Ошибка создания платежа", show_alert=True)
        return
    await callback.message.edit_text(
        f"💎 <b>CryptoPay</b>\n\nТариф: <b>{plan.name}</b>\nСумма: <b>~{invoice['amount_usdt']} USDT</b>",
        reply_markup=payment_waiting_kb(invoice["url"], plan_id, lang),
        parse_mode="HTML"
    )
    await callback.answer()


# ── СБП (Platega.io) ──────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("pay_sbp:"))
async def pay_sbp(callback: CallbackQuery):
    plan_id = int(callback.data.split(":")[1])
    async with AsyncSessionLocal() as session:
        plan = await session.get(Plan, plan_id)
        settings = await _get_settings(session)
        user = await session.get(User, callback.from_user.id)
        lang = (user.language or "ru")

        if not getattr(settings, "sbp_enabled", False):
            await callback.answer("СБП временно недоступен", show_alert=True)
            return

        order_id = str(uuid.uuid4())
        payment = Payment(
            user_id=callback.from_user.id,
            plan_id=plan_id,
            amount=plan.price_rub,
            method="sbp",
            status="pending",
            external_id=order_id,
        )
        session.add(payment)
        await session.commit()
        await session.refresh(payment)
        payment_db_id = payment.id
        plan_name = plan.name
        plan_price = plan.price_rub

    invoice = await create_sbp_invoice(
        amount_rub=plan_price,
        order_id=order_id,
        description=f"VPN {plan_name}",
        settings=settings,
        bot_domain=_bot_domain(),
    )

    if not invoice:
        await callback.answer("Ошибка создания счёта СБП. Попробуйте позже.", show_alert=True)
        return

    # Обновляем external_id на реальный invoice_id от Platega
    async with AsyncSessionLocal() as session:
        payment = await session.get(Payment, payment_db_id)
        if payment:
            payment.external_id = invoice["invoice_id"]
            if hasattr(payment, "pay_url"):
                payment.pay_url = invoice["url"]
            await session.commit()

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Оплатить через СБП", url=invoice["url"])],
        [InlineKeyboardButton(text="✅ Я оплатил", callback_data=f"check_payment:{plan_id}")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data=f"plan:{plan_id}")],
    ])

    await callback.message.edit_text(
        f"🏦 <b>Оплата через СБП</b>\n\n"
        f"📦 Тариф: <b>{plan_name}</b>\n"
        f"💰 К оплате: <b>{plan_price:.0f} ₽</b>\n"
        f"📌 Комиссия: <b>11%</b> (включена в сумму)\n"
        f"⏳ Счёт действует: <b>30 минут</b>\n\n"
        f"👇 Нажмите кнопку ниже для оплаты через банковское приложение.\n\n"
        f"После оплаты нажмите <b>«Я оплатил»</b> — подписка активируется автоматически.",
        reply_markup=kb,
        parse_mode="HTML"
    )
    await callback.answer()


# ── Проверка статуса ──────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("check_payment:"))
async def check_payment_status(callback: CallbackQuery):
    plan_id = int(callback.data.split(":")[1])
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Payment)
            .where(Payment.user_id == callback.from_user.id)
            .where(Payment.plan_id == plan_id)
            .order_by(Payment.created_at.desc())
        )
        payment = result.scalar_one_or_none()
        user = await session.get(User, callback.from_user.id)
        lang = (user.language or "ru")

    if payment and payment.status == "paid":
        await callback.answer(f"✅ {t('payment_success', lang)}", show_alert=True)
    else:
        await callback.answer(
            "⏳ Платёж ещё не подтверждён.\nПодождите — уведомление придёт автоматически.",
            show_alert=True
        )


# ── Обработчики webhook (вызываются из subscription_server) ──────────────────

async def handle_successful_payment(payment_id: int, bot):
    """Вызывается после подтверждения оплаты за тариф (webhook Heleket/CryptoPay/СБП)."""
    async with AsyncSessionLocal() as session:
        payment = await session.get(Payment, payment_id)
        if not payment or payment.status == "paid":
            return
        payment.status = "paid"
        payment.paid_at = datetime.now()
        await session.commit()

    ok = await activate_subscription(payment.user_id, payment.plan_id)
    if ok:
        async with AsyncSessionLocal() as session:
            user = await session.get(User, payment.user_id)
            plan = await session.get(Plan, payment.plan_id)
            result = await session.execute(select(Settings).where(Settings.id == 1))
            settings = result.scalar_one()
            lang = (user.language or "ru")

        bot_domain = _bot_domain()
        sub_url = f"{bot_domain}/sub/{user.sub_token}"
        plan_name = plan.name if plan else "VPN"
        duration = f"{plan.duration_days} дн." if plan else ""
        traffic = f"{plan.traffic_gb} GB" if plan and plan.traffic_gb else "Безлимит"

        # Уведомление пользователю
        try:
            await bot.send_message(
                payment.user_id,
                f"✅ <b>Оплата прошла успешно!</b>\n\n"
                f"📦 Тариф: <b>{plan_name}</b>\n"
                f"⏳ Срок: <b>{duration}</b>\n"
                f"📊 Трафик: <b>{traffic}</b>\n"
                f"💰 Оплачено: <b>{payment.amount:.0f} ₽</b>\n\n"
                f"{settings.sub_issued_text or ''}",
                reply_markup=subscription_kb(sub_url, lang),
                parse_mode="HTML"
            )
        except Exception as e:
            logger.warning(f"Не удалось уведомить пользователя {payment.user_id}: {e}")

        # Уведомление всем админам
        method_label = {
            "sbp": "СБП (Platega)",
            "heleket": "Heleket",
            "cryptopay": "CryptoPay",
            "manual": "Ручная оплата",
            "balance": "С баланса",
            "stars": "Telegram Stars",
        }.get(payment.method, payment.method or "—")
        await _notify_admins_payment(bot, user, payment.amount, method_label, plan_name)
    else:
        logger.error(f"handle_successful_payment: activate_subscription failed for payment {payment_id}")


async def handle_successful_topup(user_id: int, amount: float, method: str, bot):
    """Вызывается после подтверждения пополнения баланса через СБП."""
    from db.models import BalanceLog
    async with AsyncSessionLocal() as session:
        user = await session.get(User, user_id)
        if not user:
            return
        user.balance = (user.balance or 0) + amount
        session.add(BalanceLog(
            user_id=user_id,
            amount=amount,
            comment=f"Пополнение через {method}"
        ))
        await session.commit()
        await session.refresh(user)
        new_balance = user.balance

    # Уведомление пользователю
    try:
        await bot.send_message(
            user_id,
            f"✅ <b>Баланс пополнен!</b>\n\n"
            f"💰 Зачислено: <b>+{amount:.0f} ₽</b>\n"
            f"💳 Текущий баланс: <b>{new_balance:.0f} ₽</b>",
            parse_mode="HTML"
        )
    except Exception as e:
        logger.warning(f"Не удалось уведомить пользователя {user_id}: {e}")

    # Реферальное вознаграждение за пополнение баланса
    try:
        from utils.referral import accrue_referral_reward
        await accrue_referral_reward(
            payer_user_id=user_id,
            amount_rub=amount,
            source_label=f"пополнение баланса ({method})",
            bot=bot,
        )
    except Exception as _ref_err:
        logger.warning(f"Referral accrual error on topup: {_ref_err}")

    # Уведомление всем админам
    await _notify_admins_topup(bot, user, amount, method)
