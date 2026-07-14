"""Промокоды — ввод кода при оплате, применение скидки."""
import logging
from datetime import datetime
from aiogram import Router, F
from aiogram.types import CallbackQuery, Message, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from sqlalchemy import select

from db.database import AsyncSessionLocal
from db.models import PromoCode, User
from utils.i18n import t

router = Router()
logger = logging.getLogger(__name__)


class PromoStates(StatesGroup):
    waiting_code = State()


@router.callback_query(F.data.startswith("promo:"))
async def ask_promo(callback: CallbackQuery, state: FSMContext):
    plan_id = int(callback.data.split(":")[1])
    await state.set_state(PromoStates.waiting_code)
    await state.update_data(plan_id=plan_id)

    async with AsyncSessionLocal() as session:
        user = await session.get(User, callback.from_user.id)
        lang = (user.language if user else "ru") or "ru"

    await callback.message.edit_text(
        t("promo_enter", lang),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=t("btn_back", lang), callback_data=f"plan:{plan_id}")]
        ])
    )


@router.message(PromoStates.waiting_code)
async def receive_promo(message: Message, state: FSMContext):
    code = message.text.strip().upper()
    data = await state.get_data()
    plan_id = data.get("plan_id")

    async with AsyncSessionLocal() as session:
        user = await session.get(User, message.from_user.id)
        lang = (user.language if user else "ru") or "ru"

        result = await session.execute(
            select(PromoCode)
            .where(PromoCode.code == code)
            .where(PromoCode.is_active == True)
        )
        promo = result.scalar_one_or_none()

        # Проверяем срок
        if promo and promo.valid_until and promo.valid_until < datetime.now():
            promo = None
        # Проверяем лимит
        if promo and promo.max_uses and promo.uses_count >= promo.max_uses:
            promo = None

    if not promo:
        # Оставляем state активным — можно попробовать ещё раз
        await message.answer(
            t("promo_invalid", lang),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔄 " + ("Попробовать ещё раз" if lang == "ru" else "Try again"),
                                     callback_data=f"promo:{plan_id}")],
                [InlineKeyboardButton(text=t("btn_back", lang), callback_data=f"plan:{plan_id}")]
            ]),
            parse_mode="HTML"
        )
        pass  # state stays active
        return

    # Считаем скидку
    from db.database import AsyncSessionLocal
    from db.models import Plan
    async with AsyncSessionLocal() as session:
        plan = await session.get(Plan, plan_id)

    if promo.discount_type == "percent":
        discount_amount = plan.price_rub * promo.discount_value / 100
        discount_str = f"{promo.discount_value:.0f}%"
    else:
        discount_amount = promo.discount_value
        discount_str = f"{promo.discount_value:.0f}₽"

    new_price = max(1.0, plan.price_rub - discount_amount)

    # Сохраняем в state
    await state.update_data(promo_code=code, promo_price=new_price)
    await state.set_state(None)

    from bot.handlers.payment_handlers import show_payment_methods
    from db.models import Settings
    from sqlalchemy import select as sa_select

    async with AsyncSessionLocal() as session:
        result = await session.execute(sa_select(Settings).where(Settings.id == 1))
        settings = result.scalar_one()

    # Красивое уведомление о применении промокода
    promo_msg = (
        f"🎟 <b>Промокод применён!</b>\n\n"
        f"📦 Тариф: <b>{plan.name}</b>\n"
        f"❌ Цена без скидки: <s>{plan.price_rub:.0f}₽</s>\n"
        f"✅ Скидка: <b>{discount_str}</b>\n"
        f"💰 Итоговая цена: <b>{new_price:.0f}₽</b>\n\n"
        f"Выберите способ оплаты:"
    )

    from bot.keyboards.keyboards import payment_methods_kb
    kb = payment_methods_kb(plan_id, settings, lang, 0, new_price, f"-{discount_str}")

    await message.answer(promo_msg, parse_mode="HTML", reply_markup=kb)


async def get_promo_from_state(state: FSMContext) -> float | None:
    """Возвращает цену со скидкой если промокод был применён."""
    data = await state.get_data()
    return data.get("promo_price")


async def use_promo(code: str):
    """Инкрементировать счётчик использований промокода."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(PromoCode).where(PromoCode.code == code))
        promo = result.scalar_one_or_none()
        if promo:
            promo.uses_count += 1
            await session.commit()
