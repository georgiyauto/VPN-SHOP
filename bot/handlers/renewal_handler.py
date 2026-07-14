"""Продление подписки со скидкой — обработчик кнопки из уведомления."""
import logging
from aiogram import Router, F
from aiogram.types import CallbackQuery

from db.database import AsyncSessionLocal
from db.models import User, Plan, Settings
from sqlalchemy import select
from utils.i18n import t

router = Router()
logger = logging.getLogger(__name__)


@router.callback_query(F.data.startswith("renew_discount:"))
async def renew_with_discount(callback: CallbackQuery):
    parts = callback.data.split(":")
    plan_id = int(parts[1])
    discount = int(parts[2])

    async with AsyncSessionLocal() as session:
        plan = await session.get(Plan, plan_id)
        settings_r = await session.execute(select(Settings).where(Settings.id == 1))
        settings = settings_r.scalar_one()
        user = await session.get(User, callback.from_user.id)
        lang = (user.language or "ru")

    if not plan:
        await callback.answer("Тариф не найден", show_alert=True)
        return

    discounted_price = round(plan.price_rub * (1 - discount / 100))

    # Показываем методы оплаты с новой ценой
    from bot.handlers.payment_handlers import show_payment_methods
    await show_payment_methods(callback.message, plan_id, plan, discounted_price, lang,
                               edit=True, discount_label=f"-{discount}%")
