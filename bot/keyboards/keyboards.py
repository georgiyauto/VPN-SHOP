"""
bot/keyboards/keyboards.py  (v7)
"""
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from db.models import Plan, Settings
from utils.i18n import t


def main_menu_kb(
    has_sub: bool = False,
    trial_available: bool = True,
    lang: str = "ru",
    is_admin: bool = False,
    balance: float = 0.0,
) -> InlineKeyboardMarkup:
    rows = []

    if has_sub:
        rows.append([InlineKeyboardButton(text=t("btn_my_sub", lang), callback_data="my_sub")])
    rows.append([InlineKeyboardButton(text="🔄 Продлить подписку" if lang == "ru" else "🔄 Renew subscription", callback_data="buy")])

    rows.append([InlineKeyboardButton(
        text=f"💰 {t('balance_label', lang)}: {balance:.0f} ₽",
        callback_data="my_balance"
    )])

    buy_row = [InlineKeyboardButton(text=t("btn_buy", lang), callback_data="buy")]
    buy_row.append(InlineKeyboardButton(text=t("btn_promo", lang), callback_data="promo_standalone"))
    rows.append(buy_row)

    if trial_available and not has_sub:
        rows.append([InlineKeyboardButton(text=t("btn_trial", lang), callback_data="trial")])

    rows.append([
        InlineKeyboardButton(text=t("btn_referral", lang), callback_data="referral"),
        InlineKeyboardButton(text=t("btn_support", lang),  callback_data="support"),
    ])

    rows.append([
        InlineKeyboardButton(text=t("btn_info", lang),     callback_data="info"),
        InlineKeyboardButton(text=t("btn_language", lang), callback_data="choose_lang"),
    ])

    if is_admin:
        rows.append([InlineKeyboardButton(text="🛠 " + t("btn_admin", lang), callback_data="admin_quick_stats")])

    return InlineKeyboardMarkup(inline_keyboard=rows)


def plans_kb(plans: list, settings: Settings, lang: str = "ru") -> InlineKeyboardMarkup:
    rows = []
    for plan in plans:
        traffic = f"{plan.traffic_gb} GB" if plan.traffic_gb else t("unlimited", lang)
        devices = t("devices", lang, n=plan.max_devices) if hasattr(plan, "max_devices") else ""
        family_badge = " 👨‍👩‍👧" if getattr(plan, "max_devices", 1) >= 3 else ""
        rows.append([InlineKeyboardButton(
            text=f"{plan.name} — {plan.price_rub}₽ · {traffic} · {devices}{family_badge}",
            callback_data=f"plan:{plan.id}"
        )])
    rows.append([InlineKeyboardButton(text=t("btn_back", lang), callback_data="back_main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def payment_methods_kb(
    plan_id: int,
    settings: Settings,
    lang: str = "ru",
    user_balance: float = 0,
    discounted_price: float = None,
    discount_label: str = "",
) -> InlineKeyboardMarkup:
    rows = []
    suffix = f" {discount_label}" if discount_label else ""

    if settings.manual_payment_enabled:
        rows.append([InlineKeyboardButton(
            text=t("btn_pay_manual", lang) + suffix,
            callback_data=f"pay_manual:{plan_id}"
        )])
    if settings.heleket_enabled:
        rows.append([InlineKeyboardButton(
            text=t("btn_pay_heleket", lang) + suffix,
            callback_data=f"pay_heleket:{plan_id}"
        )])
    if settings.cryptopay_enabled:
        rows.append([InlineKeyboardButton(
            text=t("btn_pay_crypto", lang) + suffix,
            callback_data=f"pay_cryptopay:{plan_id}"
        )])
    if getattr(settings, "sbp_enabled", False):
        rows.append([InlineKeyboardButton(
            text="🏦 СБП (11%)" + suffix,
            callback_data=f"pay_sbp:{plan_id}"
        )])
    if settings.card_link_enabled and settings.card_link_url:
        rows.append([InlineKeyboardButton(
            text=(settings.card_link_text or t("btn_pay_card", lang)) + suffix,
            url=settings.card_link_url
        )])
    if getattr(settings, "stars_enabled", False):
        from math import ceil
        price = discounted_price or 0
        stars = max(1, ceil(price / 5))
        rows.append([InlineKeyboardButton(
            text=t("btn_pay_stars", lang, stars=stars),
            callback_data=f"pay_stars:{plan_id}"
        )])
    if getattr(settings, "balance_payment_enabled", True):
        balance_label = f"💰 {t('btn_pay_balance_label', lang)} ({user_balance:.0f}₽)"
        rows.append([InlineKeyboardButton(
            text=balance_label,
            callback_data=f"pay_balance:{plan_id}"
        )])
    if getattr(settings, "promo_enabled", True):
        rows.append([InlineKeyboardButton(
            text=t("btn_promo", lang),
            callback_data=f"promo:{plan_id}"
        )])
    rows.append([InlineKeyboardButton(text=t("btn_back", lang), callback_data="buy")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def subscription_kb(sub_url: str, lang: str = "ru") -> InlineKeyboardMarkup:
    from urllib.parse import quote as _q
    from aiogram.types import WebAppInfo
    import re as _re
    enc = _q(sub_url, safe="")
    domain_match = _re.match(r"(https?://[^/]+)", sub_url)
    base_domain = domain_match.group(1) if domain_match else ""
    token_match = _re.search(r"/sub/([^/?]+)", sub_url)
    token = token_match.group(1) if token_match else ""
    setup_url = f"{base_domain}/setup?token={token}" if token else sub_url
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="📲 " + ("Подключить VPN (мини-приложение)" if lang == "ru" else "Connect VPN (mini app)"),
            web_app=WebAppInfo(url=setup_url)
        )],
        [InlineKeyboardButton(text="📷 " + ("QR-код" if lang == "ru" else "QR Code"), callback_data="show_qr")],
        [InlineKeyboardButton(text="📖 " + t("btn_instruction", lang), callback_data="instruction")],
        [InlineKeyboardButton(text=t("btn_back", lang), callback_data="back_main")],
    ])


def payment_waiting_kb(pay_url: str, plan_id: int, lang: str = "ru") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t("btn_pay_go", lang), url=pay_url)],
        [InlineKeyboardButton(text=t("btn_paid", lang), callback_data=f"check_payment:{plan_id}")],
        [InlineKeyboardButton(text=t("btn_back", lang), callback_data="buy")],
    ])


def support_contact_kb(username: str, plan_id: int, amount: float) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📩 Написать в поддержку", url=f"https://t.me/{username}")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data=f"plan:{plan_id}")],
    ])


def admin_payment_kb(payment_id: int, user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Активировать", callback_data=f"admin_approve:{payment_id}"),
            InlineKeyboardButton(text="❌ Отклонить",    callback_data=f"admin_reject:{payment_id}"),
        ],
        [InlineKeyboardButton(text="👤 Профиль", url=f"tg://user?id={user_id}")],
    ])


def referral_kb(ref_link: str, lang: str = "ru", partner_balance: float = 0.0) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t("btn_ref_share", lang), switch_inline_query=ref_link)],
        [InlineKeyboardButton(
            text=f"💸 {t('btn_withdraw', lang)} ({partner_balance:.0f}₽)",
            callback_data="referral_withdraw"
        )],
        [InlineKeyboardButton(text=t("btn_back", lang), callback_data="back_main")],
    ])


def withdraw_kb(lang: str = "ru") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t("btn_back", lang), callback_data="referral")],
    ])
