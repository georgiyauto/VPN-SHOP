"""
bot/handlers/main_handlers.py  (v7)
"""
import os
import re
import logging
from datetime import datetime, timedelta

from aiogram import Router, F
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardRemove,
)
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.filters import StateFilter
from sqlalchemy import select, func

from db.database import AsyncSessionLocal
from db.models import User, Plan, Settings, Subscription, Payment
from bot.services.vpn_service import (
    get_or_create_user, get_active_subscription,
    get_all_active_servers, _add_client_to_server
)
from bot.keyboards.keyboards import main_menu_kb, subscription_kb, referral_kb, withdraw_kb
from utils.i18n import t, detect_lang

router = Router()
logger = logging.getLogger(__name__)

REFERRAL_PERCENT = 10  # % от каждой покупки реферала


def _is_admin(user_id: int) -> bool:
    ids = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
    return user_id in ids


async def _get_settings(session) -> Settings:
    result = await session.execute(select(Settings).where(Settings.id == 1))
    return result.scalar_one()


def _sub_status(sub, lang: str = "ru") -> str:
    if not sub:
        return "❌ Нет подписки" if lang == "ru" else "❌ No subscription"
    days = (sub.expires_at - datetime.now()).days if sub.expires_at else 9999
    try:
        plan_name = sub.plan.name if sub.plan else ("Активна" if lang == "ru" else "Active")
    except Exception:
        plan_name = "Активна" if lang == "ru" else "Active"
    return f"✅ {plan_name} · {days} " + ("дн." if lang == "ru" else "d.")


async def _build_menu_text(settings: Settings, user, sub, lang: str) -> str:
    base = getattr(settings, "welcome_text", None) or t("welcome", lang, name=user.full_name or "друг")
    try:
        return base.format(
            name=user.full_name or user.username or "друг",
            sub_status=_sub_status(sub, lang),
        )
    except (KeyError, ValueError):
        return base


async def _send_main_menu(target, user, settings, sub, lang: str, is_edit: bool = False):
    trial_ok = settings.trial_enabled and not user.trial_used and not sub
    text = await _build_menu_text(settings, user, sub, lang)
    kb = main_menu_kb(
        has_sub=bool(sub),
        trial_available=trial_ok,
        lang=lang,
        is_admin=_is_admin(user.id),
        balance=user.balance or 0,
    )
    if is_edit:
        try:
            await target.edit_text(text, reply_markup=kb, parse_mode="HTML")
        except Exception:
            pass
    else:
        from aiogram.types import Message as TgMessage
        if isinstance(target, TgMessage):
            try:
                rm_msg = await target.answer("​", reply_markup=ReplyKeyboardRemove())
                await rm_msg.delete()
            except Exception:
                pass
        await target.answer(text, reply_markup=kb, parse_mode="HTML")


# ── /start ────────────────────────────────────────────────────────────────────

@router.message(CommandStart())
async def cmd_start(message: Message):
    async with AsyncSessionLocal() as session:
        user = await get_or_create_user(
            session, message.from_user.id,
            message.from_user.username,
            message.from_user.full_name
        )
        if not user.language:
            user.language = detect_lang(message.from_user.language_code)
        await session.commit()

        args = message.text.split()
        if len(args) > 1:
            token = args[1]
            if token.startswith("fam_"):
                from bot.handlers.family_handler import handle_family_join
                await session.commit()
                result_text = await handle_family_join(user.id, token[4:], message.bot)
                await message.answer(result_text, parse_mode="HTML")
                return
            if not user.referred_by:
                result = await session.execute(
                    select(User).where(User.referral_code == token).where(User.id != user.id)
                )
                referrer = result.scalar_one_or_none()
                if referrer:
                    user.referred_by = referrer.id
                    await session.commit()

        settings = await _get_settings(session)
        sub = await get_active_subscription(session, user.id)
        lang = user.language or "ru"

    await _send_main_menu(message, user, settings, sub, lang)


@router.message(F.text & ~F.text.startswith("/"), StateFilter(None))
async def catch_text(message: Message):
    async with AsyncSessionLocal() as session:
        user = await session.get(User, message.from_user.id)
        if not user:
            await message.answer("Напиши /start")
            return
        settings = await _get_settings(session)
        sub = await get_active_subscription(session, user.id)
        lang = user.language or "ru"
    await _send_main_menu(message, user, settings, sub, lang)


# ── Назад в главное ───────────────────────────────────────────────────────────

@router.callback_query(F.data == "back_main")
async def back_main(callback: CallbackQuery):
    async with AsyncSessionLocal() as session:
        user = await session.get(User, callback.from_user.id)
        settings = await _get_settings(session)
        sub = await get_active_subscription(session, user.id)
        lang = user.language or "ru"
    await _send_main_menu(callback.message, user, settings, sub, lang, is_edit=True)
    await callback.answer()


# ── Моя подписка ──────────────────────────────────────────────────────────────

@router.callback_query(F.data == "my_sub")
async def my_subscription(callback: CallbackQuery):
    async with AsyncSessionLocal() as session:
        user = await session.get(User, callback.from_user.id)
        settings = await _get_settings(session)
        sub = await get_active_subscription(session, user.id)
        lang = user.language or "ru"

    if not sub:
        await callback.answer(t("sub_none", lang), show_alert=True)
        return

    bot_domain = re.sub(r":\d+$", "", os.getenv("BOT_DOMAIN", "").rstrip("/")) + ":8433"
    sub_url = f"{bot_domain}/sub/{user.sub_token}"
    days_left = (sub.expires_at - datetime.now()).days if sub.expires_at else 9999
    traffic_used = f"{sub.traffic_used_gb:.1f}" if sub.traffic_used_gb else "0"
    traffic_limit = f"{sub.traffic_limit_gb} GB" if sub.traffic_limit_gb else t("unlimited", lang)
    devices = getattr(sub.plan, "max_devices", 1) if sub.plan else 1

    text = t("sub_active", lang,
             plan=sub.plan.name if sub.plan else "—",
             days=days_left, used=traffic_used,
             limit=traffic_limit, devices=devices,
             sub_text=settings.sub_issued_text or "")

    # Ссылка на подписку в виде цитаты (blockquote)
    text += f"\n\n{t('sub_url_label', lang)}\n<blockquote>{sub_url}</blockquote>"

    await callback.message.edit_text(
        text,
        reply_markup=subscription_kb(sub_url, lang),
        parse_mode="HTML"
    )
    await callback.answer()


# ── Пробный период ────────────────────────────────────────────────────────────

@router.callback_query(F.data == "trial")
async def activate_trial(callback: CallbackQuery):
    async with AsyncSessionLocal() as session:
        user = await session.get(User, callback.from_user.id)
        settings = await _get_settings(session)
        lang = user.language or "ru"

        if user.trial_used:
            await callback.answer(t("trial_used", lang), show_alert=True)
            return
        if not settings.trial_enabled:
            await callback.answer(t("trial_disabled", lang), show_alert=True)
            return

        user.trial_used = True
        expires_at = datetime.now() + timedelta(days=settings.trial_days)
        from sqlalchemy import select as _select
        from db.models import Plan as _Plan
        trial_plan = (await session.execute(
            _select(_Plan).where(_Plan.is_active == True).order_by(_Plan.price_rub).limit(1)
        )).scalar_one_or_none()
        sub = Subscription(
            user_id=user.id, plan_id=trial_plan.id if trial_plan else 1, status="active",
            traffic_limit_gb=settings.trial_traffic_gb, expires_at=expires_at,
        )
        session.add(sub)
        await session.commit()

    async with AsyncSessionLocal() as session:
        user = await session.get(User, callback.from_user.id)
        from bot.services.vpn_service import get_cheapest_plan_servers
        servers = await get_cheapest_plan_servers(session)

    import asyncio
    expire_ms = int(expires_at.timestamp() * 1000)
    await asyncio.gather(*[
        _add_client_to_server(srv, str(user.xray_uuid), expire_ms, settings.trial_traffic_gb)
        for srv in servers
    ], return_exceptions=True)

    bot_domain = re.sub(r":\d+$", "", os.getenv("BOT_DOMAIN", "").rstrip("/")) + ":8433"
    sub_url = f"{bot_domain}/sub/{user.sub_token}"

    text = t("trial_activated", lang, days=settings.trial_days, traffic=settings.trial_traffic_gb)
    text += f"\n\n{t('sub_url_label', lang)}\n<blockquote>{sub_url}</blockquote>"

    await callback.message.edit_text(
        text,
        reply_markup=subscription_kb(sub_url, lang),
        parse_mode="HTML"
    )
    await callback.answer()


# ── Промокод из главного меню ─────────────────────────────────────────────────

@router.callback_query(F.data == "promo_standalone")
async def promo_standalone(callback: CallbackQuery):
    async with AsyncSessionLocal() as session:
        user = await session.get(User, callback.from_user.id)
        lang = user.language or "ru"
    lbl = "Если у вас есть промокод — введите его при выборе тарифа." if lang == "ru" else "If you have a promo code — enter it when selecting a plan."
    await callback.message.edit_text(
        f"🎟 <b>{'Промокод' if lang == 'ru' else 'Promo Code'}</b>\n\n{lbl}",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=t("btn_buy", lang), callback_data="buy")],
            [InlineKeyboardButton(text=t("btn_back", lang), callback_data="back_main")],
        ])
    )
    await callback.answer()


# ── Партнёрка (10% с трат рефералов) ─────────────────────────────────────────

@router.callback_query(F.data == "referral")
async def referral_info(callback: CallbackQuery):
    async with AsyncSessionLocal() as session:
        user = await session.get(User, callback.from_user.id)
        result = await session.execute(select(User).where(User.referred_by == user.id))
        referrals = result.scalars().all()
        total_earned = user.partner_earned or 0.0
        partner_balance = user.partner_balance or 0.0
        lang = user.language or "ru"

    bot_username = (await callback.bot.get_me()).username
    ref_link = f"https://t.me/{bot_username}?start={user.referral_code}"

    await callback.message.edit_text(
        t("referral_text", lang,
          partner_balance=f"{partner_balance:.0f}",
          count=len(referrals),
          total_earned=f"{total_earned:.0f}",
          link=ref_link),
        parse_mode="HTML",
        reply_markup=referral_kb(ref_link, lang, partner_balance)
    )
    await callback.answer()


# ── Заявка на вывод ───────────────────────────────────────────────────────────

class WithdrawStates(StatesGroup):
    waiting_details = State()


@router.callback_query(F.data == "referral_withdraw")
async def referral_withdraw(callback: CallbackQuery, state: FSMContext):
    async with AsyncSessionLocal() as session:
        user = await session.get(User, callback.from_user.id)
        lang = user.language or "ru"
        partner_balance = user.partner_balance or 0.0

    if partner_balance < 100:
        await callback.answer(
            t("withdraw_min", lang, balance=f"{partner_balance:.0f}"),
            show_alert=True
        )
        return

    await state.set_state(WithdrawStates.waiting_details)
    await state.update_data(partner_balance=partner_balance, lang=lang)
    await callback.message.edit_text(
        t("withdraw_prompt", lang, balance=f"{partner_balance:.0f}"),
        parse_mode="HTML",
        reply_markup=withdraw_kb(lang)
    )
    await callback.answer()


@router.message(WithdrawStates.waiting_details)
async def receive_withdraw_details(message: Message, state: FSMContext):
    data = await state.get_data()
    lang = data.get("lang", "ru")
    partner_balance = data.get("partner_balance", 0)
    details = message.text.strip()

    await state.clear()

    # Уведомляем администратора
    admin_ids = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
    admin_text = (
        f"💸 <b>Заявка на вывод</b>\n\n"
        f"👤 Пользователь: <a href='tg://user?id={message.from_user.id}'>{message.from_user.full_name}</a> (ID: {message.from_user.id})\n"
        f"💰 Сумма: <b>{partner_balance:.0f}₽</b>\n"
        f"📋 Реквизиты: <code>{details}</code>"
    )
    for admin_id in admin_ids:
        try:
            await message.bot.send_message(admin_id, admin_text, parse_mode="HTML")
        except Exception:
            pass

    # Обнуляем партнёрский баланс
    async with AsyncSessionLocal() as session:
        user = await session.get(User, message.from_user.id)
        if user:
            user.partner_balance = 0.0
            await session.commit()

    await message.answer(
        t("withdraw_sent", lang, balance=f"{partner_balance:.0f}"),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=t("btn_back", lang), callback_data="back_main")]
        ])
    )


# ── Поддержка ─────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "support")
async def support_menu(callback: CallbackQuery):
    async with AsyncSessionLocal() as session:
        user = await session.get(User, callback.from_user.id)
        lang = (user.language if user else "ru") or "ru"
        from sqlalchemy import select as _sa_sel
        from db.models import Settings as _Settings
        _res = await session.execute(_sa_sel(_Settings).where(_Settings.id == 1))
        _settings = _res.scalar_one_or_none()

    support_bot_username = ""
    support_username = ""
    if _settings:
        support_bot_username = getattr(_settings, "support_bot_username", "") or ""
        support_username = getattr(_settings, "support_username", "") or ""
    support_bot_username = support_bot_username.strip().lstrip("@") or os.getenv("SUPPORT_BOT_USERNAME", "").strip().lstrip("@")
    support_username = support_username.strip().lstrip("@") or os.getenv("SUPPORT_USERNAME", "").strip().lstrip("@")

    rows = []
    if support_bot_username:
        rows.append([InlineKeyboardButton(
            text="🎫 " + ("Открыть поддержку" if lang == "ru" else "Open support"),
            url=f"https://t.me/{support_bot_username}"
        )])
    elif support_username:
        rows.append([InlineKeyboardButton(
            text="💬 " + ("Написать в поддержку" if lang == "ru" else "Contact support"),
            url=f"https://t.me/{support_username}"
        )])
    rows.append([InlineKeyboardButton(text=t("btn_back", lang), callback_data="back_main")])

    text = (
        "💬 <b>" + ("Техническая поддержка" if lang == "ru" else "Support") + "</b>\n\n" +
        ("Нажмите кнопку ниже чтобы перейти в бот поддержки.\n\n"
         "Там вы можете:\n• Создать тикет\n• Отслеживать статус\n• Получить ответ оператора"
         if lang == "ru" else "Tap below to open the support bot.")
    )
    await callback.message.edit_text(text, parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await callback.answer()


@router.callback_query(F.data == "support_new_ticket")
async def support_new_ticket_prompt(callback: CallbackQuery, state: FSMContext):
    from bot.handlers.support_handler import NewTicketStates
    await state.set_state(NewTicketStates.waiting_text)
    async with AsyncSessionLocal() as session:
        user = await session.get(User, callback.from_user.id)
        lang = user.language or "ru"
    await callback.message.edit_text(
        "✍️ <b>" + ("Новый тикет" if lang == "ru" else "New ticket") + "</b>\n\n" +
        ("Напишите следующим сообщением описание проблемы." if lang == "ru" else "Send your issue in the next message."),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=t("btn_back", lang), callback_data="support")]
        ])
    )
    await callback.answer()


@router.callback_query(F.data == "support_my_tickets")
async def support_my_tickets(callback: CallbackQuery):
    from db.models import SupportTicket
    async with AsyncSessionLocal() as session:
        user = await session.get(User, callback.from_user.id)
        lang = user.language or "ru"
        result = await session.execute(
            select(SupportTicket)
            .where(SupportTicket.user_id == callback.from_user.id)
            .order_by(SupportTicket.created_at.desc())
            .limit(5)
        )
        tickets = result.scalars().all()

    if not tickets:
        await callback.answer("У вас нет тикетов" if lang == "ru" else "No tickets yet", show_alert=True)
        return

    status_icons = {"open": "🟡", "answered": "✅", "closed": "⚫"}
    lines = [("📋 <b>Ваши тикеты:</b>" if lang == "ru" else "📋 <b>Your tickets:</b>") + "\n"]
    for tk in tickets:
        icon = status_icons.get(tk.status, "❓")
        date = tk.created_at.strftime("%d.%m") if tk.created_at else "—"
        lines.append(f"{icon} #{tk.id} · {date} — {tk.text[:50]}")
    await callback.message.edit_text(
        "\n".join(lines), parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=t("btn_back", lang), callback_data="support")]
        ])
    )
    await callback.answer()


# ── Инструкция ────────────────────────────────────────────────────────────────

INSTRUCTION_RU = """📖 <b>Инструкция по подключению VPN</b>

━━━━━━━━━━━━━━━━━━━━
📱 <b>Android / iPhone — Happ (рекомендуем)</b>
━━━━━━━━━━━━━━━━━━━━
1. Скачайте <b>Happ</b>:
   • Android: <a href="https://play.google.com/store/apps/details?id=com.happ.vpn">Google Play</a>
   • iOS: <a href="https://apps.apple.com/app/happ-proxy-utility/id6504287215">App Store</a>
2. Вернитесь в бот → нажмите <b>«📱 Happ»</b>
3. Откроется страница → нажмите <b>«Открыть Happ»</b>
4. Нажмите <b>«Открыть»</b> в запросе
5. Подписка добавится — все серверы внутри 🎉
6. Нажмите <b>Подключить</b> и выберите сервер с флагом

━━━━━━━━━━━━━━━━━━━━
📱 <b>Android / iPhone — Hiddify</b>
━━━━━━━━━━━━━━━━━━━━
1. Скачайте <b>Hiddify</b>:
   • Android: <a href="https://play.google.com/store/apps/details?id=app.hiddify.com">Google Play</a>
   • iOS: <a href="https://apps.apple.com/app/hiddify-proxy-vpn/id6596777532">App Store</a>
2. Вернитесь в бот → нажмите <b>«📲 Hiddify»</b>
3. Подтвердите открытие → подписка добавится автоматически
4. Нажмите <b>Подключить</b>

━━━━━━━━━━━━━━━━━━━━
🖥 <b>Windows / Linux / macOS</b>
━━━━━━━━━━━━━━━━━━━━
1. Скачайте <a href="https://github.com/hiddify/hiddify-app/releases/latest">Hiddify</a>
2. Установите и запустите
3. Скопируйте ссылку подписки (блок выше)
4. В Hiddify: <b>+</b> → <b>«Добавить из буфера обмена»</b>

━━━━━━━━━━━━━━━━━━━━
📺 <b>Android TV</b>
━━━━━━━━━━━━━━━━━━━━
1. Установите <b>Hiddify</b> через ADB или файловый менеджер
2. Скопируйте ссылку подписки
3. В Hiddify: <b>+</b> → <b>«Добавить из буфера»</b>

━━━━━━━━━━━━━━━━━━━━
❓ <b>Не получается?</b> Нажмите <b>Техподдержка</b> в меню!
"""

INSTRUCTION_EN = """📖 <b>VPN Setup Guide</b>

━━━━━━━━━━━━━━━━━━━━
📱 <b>Android / iPhone — Happ (recommended)</b>
━━━━━━━━━━━━━━━━━━━━
1. Download <b>Happ</b>:
   • Android: <a href="https://play.google.com/store/apps/details?id=com.happ.vpn">Google Play</a>
   • iOS: <a href="https://apps.apple.com/app/happ-proxy-utility/id6504287215">App Store</a>
2. Return to the bot → tap <b>«📱 Happ»</b>
3. A page opens → tap <b>«Open Happ»</b>
4. Tap <b>«Open»</b> in the prompt
5. Subscription added — all servers inside 🎉
6. Tap <b>Connect</b> and choose a server by flag

━━━━━━━━━━━━━━━━━━━━
📱 <b>Android / iPhone — Hiddify</b>
━━━━━━━━━━━━━━━━━━━━
1. Download <b>Hiddify</b>:
   • Android: <a href="https://play.google.com/store/apps/details?id=app.hiddify.com">Google Play</a>
   • iOS: <a href="https://apps.apple.com/app/hiddify-proxy-vpn/id6596777532">App Store</a>
2. Return to the bot → tap <b>«📲 Hiddify»</b>
3. Confirm the prompt → subscription added automatically
4. Tap <b>Connect</b>

━━━━━━━━━━━━━━━━━━━━
🖥 <b>Windows / Linux / macOS</b>
━━━━━━━━━━━━━━━━━━━━
1. Download <a href="https://github.com/hiddify/hiddify-app/releases/latest">Hiddify</a>
2. Install and launch
3. Copy your subscription link (blockquote above)
4. In Hiddify: <b>+</b> → <b>«Add from clipboard»</b>

━━━━━━━━━━━━━━━━━━━━
📺 <b>Android TV</b>
━━━━━━━━━━━━━━━━━━━━
1. Install <b>Hiddify</b> via ADB or file manager
2. Copy your subscription link
3. In Hiddify: <b>+</b> → <b>«Add from clipboard»</b>

━━━━━━━━━━━━━━━━━━━━
❓ <b>Need help?</b> Tap <b>Support</b> in the menu!
"""


@router.callback_query(F.data == "instruction")
async def show_instruction(callback: CallbackQuery):
    async with AsyncSessionLocal() as session:
        user = await session.get(User, callback.from_user.id)
        lang = user.language or "ru" if user else "ru"
    text = INSTRUCTION_RU if lang == "ru" else INSTRUCTION_EN
    await callback.message.edit_text(
        text,
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="◀️ " + ("Назад к подписке" if lang == "ru" else "Back to subscription"),
                callback_data="my_sub"
            )],
        ])
    )
    await callback.answer()


# ── Инфо ─────────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "info")
async def show_info(callback: CallbackQuery):
    async with AsyncSessionLocal() as session:
        user = await session.get(User, callback.from_user.id)
        settings = await _get_settings(session)
        lang = user.language or "ru"
    info_text = getattr(settings, "info_text", None) or (
        "ℹ️ <b>Информация о сервисе</b>\n\nНастройте этот текст в панели администратора."
        if lang == "ru" else
        "ℹ️ <b>Service Info</b>\n\nConfigure this text in the admin panel."
    )
    # Кнопки: назад + юридические ссылки если настроены
    buttons = []
    privacy_url = getattr(settings, "privacy_policy_url", None)
    terms_url   = getattr(settings, "terms_of_service_url", None)
    if privacy_url:
        buttons.append([InlineKeyboardButton(
            text="🔒 " + ("Политика конфиденциальности" if lang == "ru" else "Privacy Policy"),
            url=privacy_url
        )])
    if terms_url:
        buttons.append([InlineKeyboardButton(
            text="📄 " + ("Пользовательское соглашение" if lang == "ru" else "Terms of Service"),
            url=terms_url
        )])
    buttons.append([InlineKeyboardButton(text=t("btn_back", lang), callback_data="back_main")])
    await callback.message.edit_text(
        info_text, parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )
    await callback.answer()


# ── Статистика ────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "my_stats")
async def my_stats(callback: CallbackQuery):
    async with AsyncSessionLocal() as session:
        user = await session.get(User, callback.from_user.id)
        sub = await get_active_subscription(session, user.id)
        result = await session.execute(select(User).where(User.referred_by == user.id))
        ref_count = len(result.scalars().all())
        result2 = await session.execute(
            select(func.sum(Payment.amount))
            .where(Payment.user_id == user.id)
            .where(Payment.status == "paid")
        )
        total_paid = result2.scalar() or 0
        lang = user.language or "ru"

    lines = [
        f"📊 <b>{'Ваша статистика' if lang == 'ru' else 'Your Stats'}</b>\n",
        f"🆔 ID: <code>{user.id}</code>",
        f"📅 {'Регистрация' if lang == 'ru' else 'Registered'}: {user.created_at.strftime('%d.%m.%Y') if user.created_at else '—'}",
        f"👥 {'Рефералов' if lang == 'ru' else 'Referrals'}: {ref_count}",
        f"💰 {'Потрачено' if lang == 'ru' else 'Spent'}: {total_paid:.0f} ₽",
        f"💸 {'Партнёрский баланс' if lang == 'ru' else 'Partner balance'}: {user.partner_balance or 0:.0f} ₽",
    ]
    if sub:
        days_left = (sub.expires_at - datetime.now()).days if sub.expires_at else "∞"
        try:
            plan_name = sub.plan.name if sub.plan else "—"
        except Exception:
            plan_name = "—"
        lines.append(f"✅ {'Подписка' if lang == 'ru' else 'Subscription'}: {plan_name} · {days_left} {'дн.' if lang == 'ru' else 'd.'}")
    await callback.message.edit_text(
        "\n".join(lines), parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=t("btn_back", lang), callback_data="back_main")]
        ])
    )
    await callback.answer()


# ── Admin ─────────────────────────────────────────────────────────────────────

@router.message(Command("admin"))
async def admin_cmd(message: Message):
    if not _is_admin(message.from_user.id):
        return
    bot_domain = os.getenv("BOT_DOMAIN", "")
    lang = "ru"
    await message.answer(
        "🛠 <b>Панель администратора</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🛠 Открыть панель", url=f"{bot_domain}/admin")],
            [InlineKeyboardButton(text="📊 Быстрая статистика", callback_data="admin_quick_stats")],
        ])
    )


@router.callback_query(F.data == "admin_quick_stats")
async def admin_quick_stats(callback: CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    from db.models import Server
    async with AsyncSessionLocal() as session:
        total_users    = (await session.execute(select(func.count(User.id)))).scalar()
        active_subs    = (await session.execute(
            select(func.count(Subscription.id)).where(Subscription.status == "active")
        )).scalar()
        revenue_today  = (await session.execute(
            select(func.sum(Payment.amount))
            .where(Payment.status == "paid")
            .where(Payment.paid_at > datetime.now() - timedelta(days=1))
        )).scalar() or 0
        servers_online = (await session.execute(
            select(func.count(Server.id)).where(Server.is_online == True)
        )).scalar()

    await callback.message.edit_text(
        f"📊 <b>Статистика</b>\n\n"
        f"👤 Пользователей: <b>{total_users}</b>\n"
        f"✅ Активных подписок: <b>{active_subs}</b>\n"
        f"🖥️ Серверов онлайн: <b>{servers_online}</b>\n"
        f"💰 Выручка за 24ч: <b>{revenue_today:.0f} ₽</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Назад", callback_data="back_main")]
        ])
    )
    await callback.answer()
