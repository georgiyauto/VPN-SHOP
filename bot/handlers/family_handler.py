"""
bot/handlers/family_handler.py

💎 Групповые/семейные тарифы.

Команды пользователя:
  /family          — управление семейной группой
  callback_data:
    family_create:{plan_id}   — создать группу
    family_invite             — получить ссылку-приглашение
    family_members            — список участников
    family_join:{token}       — принять приглашение (из deep-link ?start=fam_{token})
    family_kick:{member_id}   — исключить участника (только владелец)
    family_leave              — покинуть группу (не владелец)
"""
import os
import uuid
import logging
from datetime import datetime, timedelta

from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command
from sqlalchemy import select

from db.database import AsyncSessionLocal
from db.models import User, FamilyGroup, FamilyMember, Plan, Settings, Subscription
from bot.services.vpn_service import get_active_subscription, _add_client_to_server, get_all_active_servers
from utils.i18n import t

router = Router()
logger = logging.getLogger(__name__)

# Временное хранилище invite-токенов: {token: group_id}  (можно заменить на Redis)
_invite_tokens: dict[str, int] = {}

FAMILY_MAX_MEMBERS = 5


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _get_user_family(session, user_id: int) -> tuple[FamilyGroup | None, FamilyMember | None]:
    """Возвращает (group, member) если пользователь состоит в группе."""
    # Владелец?
    result = await session.execute(
        select(FamilyGroup)
        .where(FamilyGroup.owner_id == user_id)
        .where(FamilyGroup.status == "active")
    )
    grp = result.scalar_one_or_none()
    if grp:
        return grp, None

    # Участник?
    result = await session.execute(
        select(FamilyMember).where(FamilyMember.user_id == user_id)
    )
    member = result.scalar_one_or_none()
    if member:
        result2 = await session.execute(
            select(FamilyGroup)
            .where(FamilyGroup.id == member.group_id)
            .where(FamilyGroup.status == "active")
        )
        grp = result2.scalar_one_or_none()
        return grp, member

    return None, None


def _family_kb(is_owner: bool, group_id: int, lang: str) -> InlineKeyboardMarkup:
    rows = []
    if is_owner:
        rows.append([InlineKeyboardButton(text="👥 Участники", callback_data=f"family_members:{group_id}")])
        rows.append([InlineKeyboardButton(text="🔗 Пригласить", callback_data=f"family_invite:{group_id}")])
    else:
        rows.append([InlineKeyboardButton(text="🚪 Покинуть группу", callback_data=f"family_leave:{group_id}")])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="back_main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ── /family ───────────────────────────────────────────────────────────────────

@router.message(Command("family"))
async def cmd_family(message: Message):
    async with AsyncSessionLocal() as session:
        user = await session.get(User, message.from_user.id)
        if not user:
            await message.answer("❌ Сначала напиши /start")
            return
        lang = user.language or "ru"
        grp, member = await _get_user_family(session, user.id)

        if grp:
            is_owner = (grp.owner_id == user.id)
            result = await session.execute(
                select(FamilyMember).where(FamilyMember.group_id == grp.id)
            )
            members = result.scalars().all()
            expires = grp.expires_at.strftime("%d.%m.%Y") if grp.expires_at else "∞"
            slots_used = len([m for m in members if m.user_id is not None])
            text = (
                f"👨‍👩‍👧‍👦 <b>Семейная группа</b>\n\n"
                f"{'👑 Вы владелец' if is_owner else '👤 Вы участник'}\n"
                f"👥 Участников: {slots_used}/{grp.max_members}\n"
                f"⏳ Активна до: {expires}\n"
            )
            await message.answer(text, reply_markup=_family_kb(is_owner, grp.id, lang), parse_mode="HTML")
        else:
            # Нет группы — предложить купить семейный тариф
            result = await session.execute(
                select(Plan)
                .where(Plan.is_active == True)
                .where(Plan.max_devices >= 3)
                .order_by(Plan.sort_order)
            )
            family_plans = result.scalars().all()

            if not family_plans:
                await message.answer(
                    "💎 <b>Семейные тарифы</b>\n\nПока нет доступных семейных тарифов.",
                    parse_mode="HTML"
                )
                return

            rows = []
            for plan in family_plans:
                rows.append([InlineKeyboardButton(
                    text=f"{plan.name} — {plan.price_rub}₽ · до {plan.max_devices} чел.",
                    callback_data=f"family_create:{plan.id}"
                )])
            rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="back_main")])
            await message.answer(
                "💎 <b>Семейные тарифы</b>\n\n"
                "Одна подписка — несколько UUID. Каждый участник получает свою ссылку "
                "и может выбрать протокол (VLESS, VMess, Trojan, Shadowsocks).\n\n"
                "Выберите тариф:",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
                parse_mode="HTML"
            )


# ── Создание группы (после оплаты тарифа) ────────────────────────────────────

async def create_family_group(user_id: int, plan_id: int, payment_id: int = None) -> FamilyGroup | None:
    """Вызывается из payment_handler после успешной оплаты семейного тарифа."""
    async with AsyncSessionLocal() as session:
        plan = await session.get(Plan, plan_id)
        if not plan:
            return None

        expires_at = datetime.now() + timedelta(days=plan.duration_days)
        grp = FamilyGroup(
            owner_id=user_id,
            plan_id=plan_id,
            max_members=plan.max_devices,
            status="active",
            expires_at=expires_at,
        )
        session.add(grp)
        await session.flush()

        # Владелец автоматически становится первым участником
        owner_member = FamilyMember(
            group_id=grp.id,
            user_id=user_id,
            xray_uuid=uuid.uuid4(),
            nickname="Владелец",
            protocol="vless",
        )
        session.add(owner_member)
        await session.commit()
        await session.refresh(grp)

        # Добавляем UUID владельца на все серверы
        servers = await get_all_active_servers(session)
        expire_ms = int(expires_at.timestamp() * 1000)
        for srv in servers:
            await _add_client_to_server(srv, str(owner_member.xray_uuid), expire_ms, plan.traffic_gb)

        logger.info(f"Family group {grp.id} created for user {user_id}, plan {plan_id}")
        return grp


# ── Приглашение ───────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("family_invite:"))
async def family_invite(callback: CallbackQuery):
    group_id = int(callback.data.split(":")[1])
    async with AsyncSessionLocal() as session:
        grp = await session.get(FamilyGroup, group_id)
        if not grp or grp.owner_id != callback.from_user.id:
            await callback.answer("Нет доступа", show_alert=True)
            return

        result = await session.execute(
            select(FamilyMember).where(FamilyMember.group_id == group_id)
        )
        members = result.scalars().all()
        occupied = len([m for m in members if m.user_id is not None])

        if occupied >= grp.max_members:
            await callback.answer("Все слоты заняты!", show_alert=True)
            return

    token = uuid.uuid4().hex[:12]
    _invite_tokens[token] = group_id
    bot_username = os.getenv("BOT_USERNAME", "vpnbot")
    link = f"https://t.me/{bot_username}?start=fam_{token}"

    await callback.message.edit_text(
        f"🔗 <b>Ссылка-приглашение</b>\n\n"
        f"Отправьте эту ссылку члену семьи:\n\n"
        f"<code>{link}</code>\n\n"
        f"⚠️ Ссылка одноразовая и действует 24 часа.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Назад", callback_data=f"family_back:{group_id}")]
        ])
    )


# ── Принятие приглашения (через /start fam_TOKEN) ─────────────────────────────

async def handle_family_join(user_id: int, token: str, bot) -> str:
    """Вызывается из cmd_start при deep-link ?start=fam_TOKEN."""
    group_id = _invite_tokens.pop(token, None)
    if not group_id:
        return "❌ Ссылка-приглашение недействительна или уже использована."

    async with AsyncSessionLocal() as session:
        grp = await session.get(FamilyGroup, group_id)
        if not grp or grp.status != "active":
            return "❌ Группа не найдена или неактивна."
        if grp.owner_id == user_id:
            return "ℹ️ Вы уже являетесь владельцем этой группы."

        result = await session.execute(
            select(FamilyMember).where(FamilyMember.group_id == group_id)
        )
        members = result.scalars().all()

        # Проверим — не состоит ли пользователь уже
        for m in members:
            if m.user_id == user_id:
                return "ℹ️ Вы уже состоите в этой группе."

        occupied = len([m for m in members if m.user_id is not None])
        if occupied >= grp.max_members:
            return "❌ В группе уже нет свободных мест."

        plan = await session.get(Plan, grp.plan_id)
        new_member = FamilyMember(
            group_id=group_id,
            user_id=user_id,
            xray_uuid=uuid.uuid4(),
            nickname="Участник",
            protocol="vless",
        )
        session.add(new_member)
        await session.commit()
        await session.refresh(new_member)

        # Добавляем UUID участника на серверы
        servers = await get_all_active_servers(session)
        expire_ms = int(grp.expires_at.timestamp() * 1000) if grp.expires_at else 0
        for srv in servers:
            await _add_client_to_server(srv, str(new_member.xray_uuid), expire_ms, plan.traffic_gb if plan else 0)

    return (
        f"✅ <b>Вы вступили в семейную группу!</b>\n\n"
        f"Теперь у вас есть персональный UUID и доступ ко всем серверам.\n"
        f"Используй /myconfig чтобы получить ссылку подписки.\n\n"
        f"Для смены протокола: /protocol"
    )


# ── Список участников ─────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("family_members:"))
async def family_members(callback: CallbackQuery):
    group_id = int(callback.data.split(":")[1])
    async with AsyncSessionLocal() as session:
        grp = await session.get(FamilyGroup, group_id)
        if not grp or grp.owner_id != callback.from_user.id:
            await callback.answer("Нет доступа", show_alert=True)
            return

        result = await session.execute(
            select(FamilyMember).where(FamilyMember.group_id == group_id)
        )
        members = result.scalars().all()

    lines = [f"👨‍👩‍👧‍👦 <b>Участники группы</b> ({len(members)}/{grp.max_members})\n"]
    rows = []
    for m in members:
        icon = "👑" if m.user_id == grp.owner_id else "👤"
        name = m.nickname or f"user_{m.user_id}"
        proto = m.protocol.upper()
        lines.append(f"{icon} {name} · {proto}")
        if m.user_id != grp.owner_id and m.user_id:
            rows.append([InlineKeyboardButton(
                text=f"❌ Исключить {name}",
                callback_data=f"family_kick:{m.id}"
            )])

    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data=f"family_back:{group_id}")])
    await callback.message.edit_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        parse_mode="HTML"
    )


@router.callback_query(F.data.startswith("family_kick:"))
async def family_kick(callback: CallbackQuery):
    member_id = int(callback.data.split(":")[1])
    async with AsyncSessionLocal() as session:
        member = await session.get(FamilyMember, member_id)
        if not member:
            await callback.answer("Участник не найден", show_alert=True)
            return
        grp = await session.get(FamilyGroup, member.group_id)
        if not grp or grp.owner_id != callback.from_user.id:
            await callback.answer("Нет доступа", show_alert=True)
            return

        member.user_id = None
        member.nickname = "[свободный слот]"
        await session.commit()

    await callback.answer("✅ Участник исключён", show_alert=True)
    await family_members(callback)


@router.callback_query(F.data.startswith("family_leave:"))
async def family_leave(callback: CallbackQuery):
    group_id = int(callback.data.split(":")[1])
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(FamilyMember)
            .where(FamilyMember.group_id == group_id)
            .where(FamilyMember.user_id == callback.from_user.id)
        )
        member = result.scalar_one_or_none()
        if not member:
            await callback.answer("Вы не состоите в этой группе", show_alert=True)
            return
        member.user_id = None
        await session.commit()

    await callback.message.edit_text(
        "✅ Вы покинули семейную группу.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Главное меню", callback_data="back_main")]
        ])
    )


@router.callback_query(F.data.startswith("family_back:"))
async def family_back(callback: CallbackQuery):
    """Редирект на /family через callback."""
    await callback.message.delete()
    await cmd_family.__wrapped__(callback.message) if hasattr(cmd_family, "__wrapped__") else None
    # Простой fallback
    await callback.answer()


@router.callback_query(F.data == "open_family")
async def open_family_callback(callback: CallbackQuery):
    """Кнопка 'Семья' из главного меню."""
    async with AsyncSessionLocal() as session:
        user = await session.get(User, callback.from_user.id)
        if not user:
            await callback.answer("Сначала напиши /start", show_alert=True)
            return
        lang = user.language or "ru"
        grp, member = await _get_user_family(session, user.id)

        if grp:
            is_owner = (grp.owner_id == user.id)
            result = await session.execute(
                select(FamilyMember).where(FamilyMember.group_id == grp.id)
            )
            members = result.scalars().all()
            expires = grp.expires_at.strftime("%d.%m.%Y") if grp.expires_at else "∞"
            slots_used = len([m for m in members if m.user_id is not None])
            text = (
                f"👨‍👩‍👧‍👦 <b>Семейная группа</b>\n\n"
                f"{'👑 Вы владелец' if is_owner else '👤 Вы участник'}\n"
                f"👥 Участников: {slots_used}/{grp.max_members}\n"
                f"⏳ Активна до: {expires}\n"
            )
            await callback.message.edit_text(
                text, reply_markup=_family_kb(is_owner, grp.id, lang), parse_mode="HTML"
            )
        else:
            from utils.i18n import t
            await callback.message.edit_text(
                "👨‍👩‍👧‍👦 <b>Семейный тариф</b>\n\n"
                "У вас нет семейной группы.\n"
                "Купите тариф с пометкой 👨‍👩‍👧 для создания группы.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="💎 Купить семейный тариф", callback_data="buy")],
                    [InlineKeyboardButton(text="◀️ Назад", callback_data="back_main")],
                ])
            )
    await callback.answer()
