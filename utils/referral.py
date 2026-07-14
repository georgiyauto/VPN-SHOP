"""
utils/referral.py

Центральная логика реферальных начислений.
Вызывается из любого места где происходит пополнение баланса или оплата тарифа.

Правило:
  — при любой оплате (тариф или пополнение баланса) тому, кто пригласил
    рефераля, начисляется referral_percent% от суммы
  — referral_percent настраивается в Settings (по умолчанию 10%)
  — уведомление рефереру в Telegram с именем партнёра и суммой
  — если referral_enabled = False — ничего не начисляется
"""
import logging
import os
import re
from datetime import datetime

logger = logging.getLogger(__name__)

# Процент по умолчанию если в Settings не задан
DEFAULT_PERCENT = 10


def _bot_domain() -> str:
    base = re.sub(r":\d+$", "", os.getenv("BOT_DOMAIN", "").rstrip("/"))
    return base + ":8433"


async def accrue_referral_reward(
    *,
    payer_user_id: int,
    amount_rub: float,
    source_label: str,       # «тариф Базовый» / «пополнение баланса» и т.п.
    bot=None,                # aiogram Bot — для уведомления; None = без уведомления
    session=None,            # можно передать открытую сессию; иначе откроем сами
) -> float:
    """
    Начисляет реферальное вознаграждение пригласившему пользователю.

    Возвращает сумму начисления (0.0 если не начислено).
    """
    from db.database import AsyncSessionLocal
    from db.models import User, Settings, ReferralLog
    from sqlalchemy import select

    async def _run(sess):
        # Настройки реф. системы
        result = await sess.execute(select(Settings).where(Settings.id == 1))
        settings = result.scalar_one_or_none()
        if not settings:
            return 0.0

        if not getattr(settings, "referral_enabled", True):
            return 0.0

        # Процент из настроек или дефолт
        percent = float(getattr(settings, "referral_percent", DEFAULT_PERCENT) or DEFAULT_PERCENT)
        if percent <= 0:
            return 0.0

        # Кто платил
        payer = await sess.get(User, payer_user_id)
        if not payer or not payer.referred_by:
            return 0.0

        # Кто пригласил
        referrer = await sess.get(User, payer.referred_by)
        if not referrer:
            return 0.0

        reward = round(amount_rub * percent / 100, 2)
        if reward <= 0:
            return 0.0

        # Начисляем
        referrer.partner_balance = (referrer.partner_balance or 0.0) + reward
        referrer.partner_earned  = (referrer.partner_earned  or 0.0) + reward

        # Лог начисления
        log = ReferralLog(
            referrer_id=referrer.id,
            payer_id=payer_user_id,
            amount=reward,
            source=source_label,
        )
        sess.add(log)
        await sess.commit()
        await sess.refresh(referrer)

        logger.info(
            f"Referral reward +{reward}₽ ({percent}%) → "
            f"referrer={referrer.id} (@{referrer.username}) "
            f"from payer={payer_user_id} source='{source_label}'"
        )

        # Уведомление рефереру
        if bot:
            payer_name = f"@{payer.username}" if payer.username else f"ID {payer.id}"
            try:
                from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
                kb = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(
                        text="💰 Мой партнёрский баланс",
                        callback_data="referral"
                    )],
                ])
                await bot.send_message(
                    referrer.id,
                    f"🎉 <b>Партнёрское вознаграждение!</b>\n\n"
                    f"👤 Ваш партнёр <b>{payer_name}</b> оплатил:\n"
                    f"📌 <b>{source_label}</b>\n"
                    f"💵 Сумма платежа: <b>{amount_rub:.0f} ₽</b>\n\n"
                    f"✅ Вам начислено: <b>+{reward:.0f} ₽</b> ({percent:.0f}%)\n"
                    f"💼 Партнёрский баланс: <b>{referrer.partner_balance:.0f} ₽</b>",
                    reply_markup=kb,
                    parse_mode="HTML"
                )
            except Exception as e:
                logger.warning(f"Не удалось отправить уведомление рефереру {referrer.id}: {e}")

        return reward

    if session:
        return await _run(session)
    else:
        from db.database import AsyncSessionLocal
        async with AsyncSessionLocal() as sess:
            return await _run(sess)
