"""
bot/handlers/admin_panel_handler.py

Встроенная мини-панель администратора прямо в Telegram.
Доступна через кнопку "🛠 Админка" в главном меню.

Разделы:
  📊 Статистика  — быстрые цифры
  👤 Пользователи — поиск, управление
  🖥️ Серверы — список + добавление с авто-установкой
  💰 Платежи — последние транзакции
  ⚙️ Настройки — быстрые переключатели
"""
import os
import logging
from datetime import datetime, timedelta
from aiogram import Router, F
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from sqlalchemy import select, func

from db.database import AsyncSessionLocal
from db.models import User, Subscription, Plan, Server, Payment, Settings

router = Router()
logger = logging.getLogger(__name__)


def is_admin(user_id: int) -> bool:
    admin_ids = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
    return user_id in admin_ids


class ServerAddStates(StatesGroup):
    type_choice = State()
    label = State()
    ssh_host = State()
    ssh_port = State()
    ssh_user = State()
    ssh_auth = State()        # пароль или ключ
    confirm = State()


class UserSearchStates(StatesGroup):
    query = State()


# ── Reply keyboard для пользователей и админов ────────────────────────────────

def user_reply_kb(*args, **kwargs):
    """v5: Reply-клавиатура удалена. Всё через Inline. Оставлена для совместимости."""
    return None


def admin_reply_kb(*args, **kwargs):
    """v5: Reply-клавиатура удалена. Всё через Inline. Оставлена для совместимости."""
    return None


# ── Главная мини-панель в Telegram ────────────────────────────────────────────

async def show_admin_main(message_or_cb):
    async with AsyncSessionLocal() as session:
        total_users = (await session.execute(select(func.count(User.id)))).scalar()
        active_subs = (await session.execute(
            select(func.count(Subscription.id))
            .where(Subscription.status == "active")
            .where(Subscription.expires_at > datetime.now())
        )).scalar()
        servers_online = (await session.execute(
            select(func.count(Server.id))
            .where(Server.is_active == True)
            .where(Server.is_online == True)
        )).scalar()
        revenue_today = (await session.execute(
            select(func.sum(Payment.amount))
            .where(Payment.status == "paid")
            .where(Payment.paid_at > datetime.now() - timedelta(days=1))
        )).scalar() or 0

    text = (
        "🛠 <b>Панель администратора</b>\n\n"
        f"👤 Пользователей: <b>{total_users}</b>\n"
        f"✅ Активных подписок: <b>{active_subs}</b>\n"
        f"🖥️ Серверов онлайн: <b>{servers_online}</b>\n"
        f"💰 Выручка за 24ч: <b>{revenue_today:.0f} ₽</b>"
    )

    bot_domain = os.getenv("BOT_DOMAIN", "")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="👤 Пользователи", callback_data="adm_users:1"),
            InlineKeyboardButton(text="🖥️ Серверы",      callback_data="adm_servers"),
        ],
        [
            InlineKeyboardButton(text="📦 Тарифы",       callback_data="adm_plans"),
            InlineKeyboardButton(text="💰 Платежи",      callback_data="adm_payments:1"),
        ],
        [
            InlineKeyboardButton(text="🎟 Промокоды",    callback_data="adm_promos"),
            InlineKeyboardButton(text="⚙️ Настройки",    callback_data="adm_settings"),
        ],
        [
            InlineKeyboardButton(text="🌐 Веб-админка",
                                 url=f"{bot_domain}/admin/"),
        ],
    ])

    if isinstance(message_or_cb, CallbackQuery):
        await message_or_cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    else:
        await message_or_cb.answer(text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data == "adm_main")
async def adm_main(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    await show_admin_main(callback)


# ── Пользователи ──────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("adm_users:"))
async def adm_users(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    page = int(callback.data.split(":")[1])
    per_page = 8

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(User).order_by(User.created_at.desc())
            .offset((page - 1) * per_page).limit(per_page)
        )
        users = result.scalars().all()
        total = (await session.execute(select(func.count(User.id)))).scalar()

    rows = []
    for u in users:
        name = u.full_name or u.username or str(u.id)
        rows.append([InlineKeyboardButton(
            text=f"{'🚫' if u.is_banned else '👤'} {name[:25]}",
            callback_data=f"adm_user:{u.id}"
        )])

    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"adm_users:{page-1}"))
    nav.append(InlineKeyboardButton(text=f"{page}/{(total-1)//per_page+1}", callback_data="noop"))
    if page * per_page < total:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"adm_users:{page+1}"))
    if nav:
        rows.append(nav)

    rows.append([
        InlineKeyboardButton(text="🔍 Поиск", callback_data="adm_user_search"),
        InlineKeyboardButton(text="◀️ Назад", callback_data="adm_main"),
    ])

    await callback.message.edit_text(
        f"👤 <b>Пользователи</b> (всего: {total})",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        parse_mode="HTML"
    )


@router.callback_query(F.data.startswith("adm_user:"))
async def adm_user_detail(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    user_id = int(callback.data.split(":")[1])

    async with AsyncSessionLocal() as session:
        user = await session.get(User, user_id)
        sub_result = await session.execute(
            select(Subscription, Plan)
            .outerjoin(Plan, Plan.id == Subscription.plan_id)
            .where(Subscription.user_id == user_id)
            .where(Subscription.status == "active")
            .order_by(Subscription.expires_at.desc())
        )
        sub_row = sub_result.first()

    if not user:
        await callback.answer("Пользователь не найден", show_alert=True)
        return

    sub_info = "нет"
    if sub_row:
        sub, plan = sub_row
        days = (sub.expires_at - datetime.now()).days if sub.expires_at else "∞"
        sub_info = f"{plan.name if plan else '?'} · {days} дн."

    text = (
        f"👤 <b>{user.full_name or 'Без имени'}</b>\n"
        f"🆔 <code>{user.id}</code>\n"
        f"📱 @{user.username or '—'}\n"
        f"💰 Баланс: <b>{user.balance or 0:.0f} ₽</b>\n"
        f"📦 Подписка: <b>{sub_info}</b>\n"
        f"🌍 Язык: {user.language or 'ru'}\n"
        f"📅 Зарегистрирован: {user.created_at.strftime('%d.%m.%Y') if user.created_at else '—'}\n"
        f"🚫 Бан: {'Да' if user.is_banned else 'Нет'}"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Активировать", callback_data=f"adm_activate:{user_id}"),
            InlineKeyboardButton(text="💰 Баланс",       callback_data=f"adm_balance:{user_id}"),
        ],
        [
            InlineKeyboardButton(
                text="🔓 Разбанить" if user.is_banned else "🚫 Забанить",
                callback_data=f"adm_ban:{user_id}:{int(not user.is_banned)}"
            ),
            InlineKeyboardButton(text="📩 Написать", url=f"tg://user?id={user_id}"),
        ],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="adm_users:1")],
    ])
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data.startswith("adm_ban:"))
async def adm_toggle_ban(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    parts = callback.data.split(":")
    user_id, ban_val = int(parts[1]), bool(int(parts[2]))
    async with AsyncSessionLocal() as session:
        user = await session.get(User, user_id)
        user.is_banned = ban_val
        await session.commit()
    from bot.services.vpn_service import revoke_user_access, restore_user_access
    if ban_val:
        changed, total = await revoke_user_access(user_id)
        status = f"Доступ удалён с {changed}/{total} Xray нод"
    else:
        changed, total = await restore_user_access(user_id)
        status = f"Доступ восстановлен на {changed}/{total} Xray нод"
    await callback.answer(status)
    await adm_user_detail(callback)


@router.callback_query(F.data.startswith("adm_activate:"))
async def adm_quick_activate(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    user_id = int(callback.data.split(":")[1])

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Plan).where(Plan.is_active == True).order_by(Plan.sort_order)
        )
        plans = result.scalars().all()

    rows = [[InlineKeyboardButton(
        text=f"{p.name} — {p.price_rub}₽",
        callback_data=f"adm_do_activate:{user_id}:{p.id}"
    )] for p in plans]
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data=f"adm_user:{user_id}")])

    await callback.message.edit_text(
        "📦 Выберите тариф для активации:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)
    )


@router.callback_query(F.data.startswith("adm_do_activate:"))
async def adm_do_activate(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    parts = callback.data.split(":")
    user_id, plan_id = int(parts[1]), int(parts[2])
    from bot.services.vpn_service import activate_subscription
    ok = await activate_subscription(user_id, plan_id)
    if ok:
        await callback.answer("✅ Подписка активирована")
        try:
            async with AsyncSessionLocal() as session:
                user = await session.get(User, user_id)
                result = await session.execute(select(Settings).where(Settings.id == 1))
                settings = result.scalar_one()
                lang = (user.language or "ru")
            import os
            from bot.keyboards.keyboards import subscription_kb
            bot_domain = os.getenv("BOT_DOMAIN", "")
            sub_url = f"{bot_domain}/sub/{user.sub_token}"
            await callback.bot.send_message(
                user_id,
                f"✅ <b>Подписка активирована!</b>\n\n{settings.sub_issued_text}",
                reply_markup=subscription_kb(sub_url, lang),
                parse_mode="HTML"
            )
        except Exception:
            pass
    else:
        await callback.answer("❌ Ошибка активации", show_alert=True)


@router.callback_query(F.data.startswith("adm_balance:"))
async def adm_quick_balance(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return
    user_id = int(callback.data.split(":")[1])
    await state.update_data(balance_target_user=user_id)

    await callback.message.edit_text(
        f"💰 Введите сумму для изменения баланса пользователя {user_id}\n"
        f"(+ для пополнения, - для списания, например: +500 или -100)",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="❌ Отмена", callback_data=f"adm_user:{user_id}")
        ]])
    )
    await state.set_state("adm_balance_input")


@router.message(F.state == "adm_balance_input")
async def adm_balance_input(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    data = await state.get_data()
    user_id = data.get("balance_target_user")
    try:
        amount = float(message.text.replace(",", ".").replace(" ", ""))
    except ValueError:
        await message.answer("❌ Неверный формат. Введите число, например: 500 или -100")
        return

    from bot.handlers.balance_handler import change_balance_direct
    new_balance = await change_balance_direct(user_id, amount, "Изменение администратором в боте")
    await state.clear()
    await message.answer(
        f"✅ Баланс изменён\n"
        f"Пользователь: {user_id}\n"
        f"Изменение: {'+' if amount > 0 else ''}{amount:.0f} ₽\n"
        f"Новый баланс: {new_balance:.0f} ₽"
    )
    # Уведомляем пользователя
    try:
        sign = "+" if amount > 0 else ""
        await message.bot.send_message(
            user_id,
            f"💰 <b>Баланс изменён</b>\n\n"
            f"{sign}{amount:.0f} ₽\nИтого: <b>{new_balance:.0f} ₽</b>",
            parse_mode="HTML"
        )
    except Exception:
        pass


# ── Серверы ────────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "adm_servers")
async def adm_servers(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Server).order_by(Server.sort_order))
        servers = result.scalars().all()

    rows = []
    for s in servers:
        status_icon = "✅" if s.is_online else "🔴"
        install_icon = "⚙️" if s.install_status == "installing" else ""
        rows.append([InlineKeyboardButton(
            text=f"{status_icon} {s.flag} {s.label} {install_icon}",
            callback_data=f"adm_server:{s.id}"
        )])

    rows.append([InlineKeyboardButton(
        text="➕ Добавить Xray ноду", callback_data="adm_add_server:vpn"
    )])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="adm_main")])

    await callback.message.edit_text(
        f"🖥️ <b>Серверы</b> (всего: {len(servers)})\n\n"
        "Xray-core + защищённый node-agent\n"
        "✅ = онлайн   🔴 = оффлайн   ⚙️ = устанавливается",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        parse_mode="HTML"
    )


@router.callback_query(F.data.startswith("adm_server:"))
async def adm_server_detail(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    server_id = int(callback.data.split(":")[1])
    async with AsyncSessionLocal() as session:
        server = await session.get(Server, server_id)
    status = "✅ Онлайн" if server.is_online else "🔴 Оффлайн"
    install = server.install_status or "unknown"

    text = (
        f"🖥️ <b>{server.flag} {server.label}</b>\n\n"
        "Тип: Xray node\n"
        f"Статус: {status}\n"
        f"Установка: <b>{install}</b>\n"
        f"Node API: {server.node_url or '—'}\n"
    )
    if server.install_log:
        last_log = server.install_log[-200:]
        text += f"\n<code>{last_log}</code>"

    rows = []
    if server.install_status in ("pending", "error"):
        rows.append([InlineKeyboardButton(
            text="🚀 Установить", callback_data=f"adm_install:{server_id}"
        )])
    if server.install_status == "ready":
        rows.append([InlineKeyboardButton(
            text="🔄 Переустановить", callback_data=f"adm_install:{server_id}"
        )])
    rows.append([
        InlineKeyboardButton(
            text="🔴 Деактивировать" if server.is_active else "✅ Активировать",
            callback_data=f"adm_toggle_server:{server_id}"
        ),
        InlineKeyboardButton(text="🗑 Удалить", callback_data=f"adm_delete_server:{server_id}"),
    ])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="adm_servers")])

    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        parse_mode="HTML"
    )


@router.callback_query(F.data.startswith("adm_install:"))
async def adm_install_server(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    server_id = int(callback.data.split(":")[1])

    async with AsyncSessionLocal() as session:
        server = await session.get(Server, server_id)
        server.install_status = "installing"
        server.install_log = "Запуск установки..."
        await session.commit()

    await callback.answer("🚀 Установка запущена в фоне")
    await callback.message.edit_text(
        "⚙️ <b>Установка запущена...</b>\n\n"
        "Это займёт 1-3 минуты. Статус обновится автоматически.\n"
        "Вы получите уведомление по завершении.",
        parse_mode="HTML"
    )

    # Запускаем установку в фоне
    import asyncio
    admin_id = callback.from_user.id
    bot = callback.bot

    async def run_install():
        from utils.server_provisioner import provision_vpn_server
        logs = []
        def log_cb(msg): logs.append(msg)

        result = await provision_vpn_server(server_id, log_cb)

        if result["ok"]:
            text = (
                f"✅ <b>Сервер {server_id} установлен!</b>\n\n"
                f"Node API: {result.get('node_url')}\n"
            )
            if result.get("vless_link"):
                text += f"\n<code>{result['vless_link'][:200]}</code>"
            if result.get("client_note"):
                text += f"\n\n📝 {result['client_note'][:300]}"
        else:
            text = f"❌ <b>Ошибка установки сервера {server_id}</b>\n\n<code>{result['error'][:500]}</code>"

        try:
            await bot.send_message(admin_id, text, parse_mode="HTML")
        except Exception:
            pass

    asyncio.create_task(run_install())


@router.callback_query(F.data.startswith("adm_toggle_server:"))
async def adm_toggle_server(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    server_id = int(callback.data.split(":")[1])
    async with AsyncSessionLocal() as session:
        server = await session.get(Server, server_id)
        server.is_active = not server.is_active
        await session.commit()
    await callback.answer(f"{'✅ Активирован' if server.is_active else '🔴 Деактивирован'}")
    await adm_server_detail(callback)


@router.callback_query(F.data.startswith("adm_delete_server:"))
async def adm_delete_server(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    server_id = int(callback.data.split(":")[1])
    async with AsyncSessionLocal() as session:
        server = await session.get(Server, server_id)
        await session.delete(server)
        await session.commit()
    await callback.answer("🗑 Сервер удалён")
    await adm_servers(callback)


# ── Добавление сервера (FSM) ──────────────────────────────────────────────────

@router.callback_query(F.data.startswith("adm_add_server:"))
async def adm_add_server_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return
    await state.set_state(ServerAddStates.label)

    type_desc = "<b>Xray нода</b>\nБот установит Xray-core, node-agent и создаст VLESS Reality inbound."
    await callback.message.edit_text(
        f"{type_desc}\n\n"
        "Введите <b>название</b> сервера (например: 🇩🇪 Germany 1):",
        parse_mode="HTML"
    )


@router.message(ServerAddStates.label)
async def adm_server_label(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.update_data(label=message.text)
    await state.set_state(ServerAddStates.ssh_host)
    await message.answer("Введите <b>IP адрес</b> сервера:", parse_mode="HTML")


@router.message(ServerAddStates.ssh_host)
async def adm_server_ssh_host(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.update_data(ssh_host=message.text.strip())
    await state.set_state(ServerAddStates.ssh_auth)
    await message.answer(
        "Введите <b>пароль root</b> для SSH-подключения:\n"
        "(или отправьте приватный SSH ключ начиная с -----BEGIN)",
        parse_mode="HTML"
    )


@router.message(ServerAddStates.ssh_auth)
async def adm_server_ssh_auth(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    text = message.text.strip()
    if text.startswith("-----BEGIN"):
        await state.update_data(ssh_key=text, ssh_password=None)
    else:
        await state.update_data(ssh_password=text, ssh_key=None)

    await _finalize_server_add(message, state)


async def _finalize_server_add(message_or_cb, state: FSMContext):
    data = await state.get_data()
    await state.clear()

    is_msg = isinstance(message_or_cb, Message)
    uid = message_or_cb.from_user.id if is_msg else message_or_cb.from_user.id

    # Создаём сервер в БД
    async with AsyncSessionLocal() as session:
        server = Server(
            label=data.get("label", "Новый сервер"),
            flag=data.get("label", "")[:2] if data.get("label", "").startswith(("🇷", "🇩", "🇺", "🇬", "🇫")) else "🌍",
            ssh_host=data.get("ssh_host"),
            ssh_port=22,
            ssh_user="root",
            ssh_password=data.get("ssh_password"),
            ssh_key=data.get("ssh_key"),
            install_status="pending",
            node_url=f"https://{data.get('ssh_host')}:8090",
        )
        session.add(server)
        await session.commit()
        server_id = server.id

    text = (
        f"✅ <b>Сервер добавлен!</b>\n\n"
        f"ID: {server_id}\n"
        "Тип: Xray node\n"
        f"IP: {data.get('ssh_host')}\n\n"
        f"Нажмите <b>🚀 Установить</b> чтобы запустить авто-настройку."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Установить сейчас", callback_data=f"adm_install:{server_id}")],
        [InlineKeyboardButton(text="◀️ К серверам", callback_data="adm_servers")],
    ])

    if is_msg:
        await message_or_cb.answer(text, reply_markup=kb, parse_mode="HTML")
    else:
        await message_or_cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")


# ── Быстрая статистика ─────────────────────────────────────────────────────────

async def show_quick_stats(message: Message):
    if not is_admin(message.from_user.id):
        return
    async with AsyncSessionLocal() as session:
        total_users = (await session.execute(select(func.count(User.id)))).scalar()
        active_subs = (await session.execute(
            select(func.count(Subscription.id))
            .where(Subscription.status == "active")
            .where(Subscription.expires_at > datetime.now())
        )).scalar()
        revenue_today = (await session.execute(
            select(func.sum(Payment.amount))
            .where(Payment.status == "paid")
            .where(Payment.paid_at > datetime.now() - timedelta(days=1))
        )).scalar() or 0
        revenue_month = (await session.execute(
            select(func.sum(Payment.amount))
            .where(Payment.status == "paid")
            .where(Payment.paid_at > datetime.now() - timedelta(days=30))
        )).scalar() or 0
        new_today = (await session.execute(
            select(func.count(User.id))
            .where(User.created_at > datetime.now() - timedelta(days=1))
        )).scalar()
        servers_online = (await session.execute(
            select(func.count(Server.id))
            .where(Server.is_active == True)
            .where(Server.is_online == True)
        )).scalar()

    await message.answer(
        f"📊 <b>Статистика</b>\n\n"
        f"👤 Всего пользователей: <b>{total_users}</b>\n"
        f"🆕 Новых сегодня: <b>{new_today}</b>\n"
        f"✅ Активных подписок: <b>{active_subs}</b>\n"
        f"🖥️ Серверов онлайн: <b>{servers_online}</b>\n\n"
        f"💰 Выручка за 24ч: <b>{revenue_today:.0f} ₽</b>\n"
        f"💰 Выручка за 30д: <b>{revenue_month:.0f} ₽</b>",
        parse_mode="HTML"
    )


# ── Настройки ──────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "adm_settings")
async def adm_settings(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Settings).where(Settings.id == 1))
        settings = result.scalar_one()

    def _toggle(val): return "✅" if val else "❌"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"{_toggle(settings.trial_enabled)} Пробный период",
            callback_data="adm_toggle_setting:trial_enabled"
        )],
        [InlineKeyboardButton(
            text=f"{_toggle(settings.referral_enabled)} Реферальная система",
            callback_data="adm_toggle_setting:referral_enabled"
        )],
        [InlineKeyboardButton(
            text=f"{_toggle(settings.channel_required)} Принудит. подписка на канал",
            callback_data="adm_toggle_setting:channel_required"
        )],
        [InlineKeyboardButton(
            text=f"{_toggle(settings.stars_enabled)} Telegram Stars",
            callback_data="adm_toggle_setting:stars_enabled"
        )],
        [InlineKeyboardButton(
            text=f"{_toggle(settings.renewal_discount_enabled)} Скидка при продлении",
            callback_data="adm_toggle_setting:renewal_discount_enabled"
        )],
        [InlineKeyboardButton(
            text=f"{_toggle(settings.promo_enabled)} Промокоды",
            callback_data="adm_toggle_setting:promo_enabled"
        )],
        [InlineKeyboardButton(
            text=f"{_toggle(settings.server_monitoring_enabled)} Мониторинг серверов",
            callback_data="adm_toggle_setting:server_monitoring_enabled"
        )],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="adm_main")],
    ])
    await callback.message.edit_text(
        "⚙️ <b>Настройки</b>\n\nКликните для включения/выключения:",
        reply_markup=kb,
        parse_mode="HTML"
    )


@router.callback_query(F.data.startswith("adm_toggle_setting:"))
async def adm_toggle_setting(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    field = callback.data.split(":")[1]
    boolean_fields = [
        "trial_enabled", "referral_enabled", "channel_required",
        "stars_enabled", "renewal_discount_enabled", "promo_enabled",
        "server_monitoring_enabled", "auto_backup_enabled",
        "balance_payment_enabled", "heleket_enabled", "cryptopay_enabled",
        "manual_payment_enabled", "card_link_enabled"
    ]
    if field not in boolean_fields:
        await callback.answer("Неизвестная настройка", show_alert=True)
        return

    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Settings).where(Settings.id == 1))
        settings = result.scalar_one()
        current = getattr(settings, field, False)
        setattr(settings, field, not current)
        await session.commit()

    await callback.answer(f"{'✅ Включено' if not current else '❌ Выключено'}")
    await adm_settings(callback)
