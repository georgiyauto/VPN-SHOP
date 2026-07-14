import re
"""
Оплата через Telegram Stars (XTR).
Stars — нативный метод без комиссий, без ИП.
bot.send_invoice(currency="XTR", prices=[LabeledPrice("VPN", stars_amount)])
"""
import logging
from aiogram import Router, F
from aiogram.types import (
    CallbackQuery, Message, LabeledPrice,
    PreCheckoutQuery, InlineKeyboardMarkup, InlineKeyboardButton
)
from sqlalchemy import select

from db.database import AsyncSessionLocal
from db.models import Plan, Settings, User, Payment
from bot.services.vpn_service import activate_subscription
from utils.i18n import t

router = Router()
logger = logging.getLogger(__name__)


def calc_stars(price_rub: float, stars_rate: int) -> int:
    """
    Конвертируем рубли в Stars.
    stars_rate = кол-во Stars за базовый тариф 30 дней.
    Пропорционально масштабируем.
    """
    # Минимум 1 звезда
    return max(1, round(price_rub / 5))  # ~5 руб за 1 Star (настраивается)


@router.callback_query(F.data.startswith("pay_stars:"))
async def pay_stars(callback: CallbackQuery):
    plan_id = int(callback.data.split(":")[1])

    async with AsyncSessionLocal() as session:
        plan = await session.get(Plan, plan_id)
        settings_result = await session.execute(select(Settings).where(Settings.id == 1))
        settings = settings_result.scalar_one()
        user = await session.get(User, callback.from_user.id)
        lang = (user.language if user else "ru") or "ru"

    if not settings.stars_enabled:
        await callback.answer("⭐ Stars оплата недоступна", show_alert=True)
        return

    stars_count = calc_stars(plan.price_rub, settings.stars_rate)

    # Создаём запись платежа
    async with AsyncSessionLocal() as session:
        payment = Payment(
            user_id=callback.from_user.id,
            plan_id=plan_id,
            method="stars",
            amount=stars_count,
            status="pending"
        )
        session.add(payment)
        await session.commit()
        payment_id = payment.id

    traffic = f"{plan.traffic_gb} GB" if plan.traffic_gb else t("unlimited", lang)

    await callback.message.delete()
    await callback.bot.send_invoice(
        chat_id=callback.from_user.id,
        title=f"VPN — {plan.name}",
        description=(
            f"⏳ {plan.duration_days} дн. · "
            f"📊 {traffic} · "
            f"📱 {plan.max_devices} уст."
        ),
        payload=f"stars:{payment_id}:{plan_id}",
        currency="XTR",
        prices=[LabeledPrice(label=plan.name, amount=stars_count)],
    )


@router.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery):
    """Telegram вызывает это перед списанием Stars — надо ответить OK."""
    await query.answer(ok=True)


@router.message(F.successful_payment)
async def successful_stars_payment(message: Message):
    """Stars успешно списаны — активируем подписку."""
    payload = message.successful_payment.invoice_payload
    if not payload.startswith("stars:"):
        return

    parts = payload.split(":")
    payment_id = int(parts[1])
    plan_id = int(parts[2])

    async with AsyncSessionLocal() as session:
        payment = await session.get(Payment, payment_id)
        if payment:
            from datetime import datetime
            payment.status = "paid"
            payment.paid_at = datetime.now()
            await session.commit()

    ok = await activate_subscription(message.from_user.id, plan_id)

    async with AsyncSessionLocal() as session:
        user = await session.get(User, message.from_user.id)
        lang = (user.language if user else "ru") or "ru"

    if ok:
        import os
        from bot.keyboards.keyboards import subscription_kb
        async with AsyncSessionLocal() as session:
            user = await session.get(User, message.from_user.id)
        bot_domain = re.sub(r":(\d+)$", "", os.getenv("BOT_DOMAIN", "").rstrip("/")) + ":8433"
        sub_url = f"{bot_domain}/sub/{user.sub_token}"

        await message.answer(
            f"⭐ <b>Оплачено Stars!</b>\n\n{t('sub_issued', lang)}",
            reply_markup=subscription_kb(sub_url),
            parse_mode="HTML"
        )
    else:
        await message.answer(f"❌ {t('payment_failed', lang)}")
