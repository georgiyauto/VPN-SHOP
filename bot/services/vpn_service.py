import asyncio
import logging
import os
from datetime import datetime, timedelta
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import User, Subscription, Plan, Server, Settings, PlanServer
from db.database import AsyncSessionLocal
from bot.services.xray_client import XrayClient

logger = logging.getLogger(__name__)

# Глобальный экземпляр бота — устанавливается при старте (set_bot_instance)
_bot_instance = None

def set_bot_instance(bot):
    """Вызывается из bot/main.py при старте, чтобы vpn_service мог слать уведомления."""
    global _bot_instance
    _bot_instance = bot


async def get_or_create_user(session: AsyncSession, telegram_id: int, username: str = None, full_name: str = None) -> User:
    result = await session.execute(select(User).where(User.id == telegram_id))
    user = result.scalar_one_or_none()
    if not user:
        import uuid, random, string
        ref_code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))
        user = User(
            id=telegram_id,
            username=username,
            full_name=full_name,
            sub_token=uuid.uuid4(),
            xray_uuid=uuid.uuid4(),
            referral_code=ref_code,
        )
        session.add(user)
        await session.flush()
    return user


async def get_active_subscription(session: AsyncSession, user_id: int) -> Subscription | None:
    from sqlalchemy.orm import joinedload
    result = await session.execute(
        select(Subscription)
        .options(joinedload(Subscription.plan))
        .where(Subscription.user_id == user_id)
        .where(Subscription.status == "active")
        .order_by(Subscription.expires_at.desc())
    )
    return result.scalars().first()


async def get_all_active_servers(session: AsyncSession) -> list[Server]:
    result = await session.execute(
        select(Server)
        .where(Server.is_active == True)
        .where(Server.install_status == "ready")
        .where(Server.node_url.is_not(None))
        .where(Server.node_token.is_not(None))
        .order_by(Server.sort_order)
    )
    return result.scalars().all()


async def get_servers_for_plan(session: AsyncSession, plan_id: int | None) -> list[Server]:
    """Вернуть серверы для тарифа. Если у тарифа нет привязанных серверов — все серверы."""
    if plan_id:
        result = await session.execute(
            select(Server)
            .join(PlanServer, PlanServer.server_id == Server.id)
            .where(PlanServer.plan_id == plan_id)
            .where(Server.is_active == True)
            .where(Server.install_status == "ready")
            .where(Server.node_url.is_not(None))
            .where(Server.node_token.is_not(None))
            .order_by(Server.sort_order)
        )
        servers = result.scalars().all()
        if servers:
            return servers
    # Fallback — все серверы
    return await get_all_active_servers(session)


async def get_cheapest_plan_servers(session: AsyncSession) -> list[Server]:
    """Серверы самого дешёвого активного тарифа (для пробного периода)."""
    result = await session.execute(
        select(Plan)
        .where(Plan.is_active == True)
        .order_by(Plan.price_rub.asc())
    )
    cheapest = result.scalars().first()
    if cheapest:
        return await get_servers_for_plan(session, cheapest.id)
    return await get_all_active_servers(session)


async def activate_subscription(user_id: int, plan_id: int) -> bool:
    """Активировать подписку: создать запись и добавить клиента на серверы тарифа"""
    async with AsyncSessionLocal() as session:
        user = await session.get(User, user_id)
        plan = await session.get(Plan, plan_id)
        if not user or not plan:
            return False

        # Если есть активная подписка — прибавляем дни к её сроку
        old = await get_active_subscription(session, user_id)
        previous_plan_id = old.plan_id if old else None
        if old and old.expires_at and old.expires_at > datetime.now():
            # Продление: дни прибавляются к текущей дате окончания
            expires_at = old.expires_at + timedelta(days=plan.duration_days)
            # Обновляем трафик если новый план даёт больше
            if plan.traffic_gb and (not old.traffic_limit_gb or plan.traffic_gb > old.traffic_limit_gb):
                old.traffic_limit_gb = plan.traffic_gb
            old.expires_at = expires_at
            old.plan_id = plan_id
            await session.commit()
            logger.info(f"Extended sub for user {user_id}: +{plan.duration_days}d → expires {expires_at.date()}")
        else:
            # Новая подписка — старую деактивируем
            if old:
                old.status = "expired"
            expires_at = datetime.now() + timedelta(days=plan.duration_days)
            sub = Subscription(
                user_id=user_id,
                plan_id=plan_id,
                status="active",
                traffic_limit_gb=plan.traffic_gb,
                expires_at=expires_at,
            )
            session.add(sub)
            await session.commit()

        # ── Реферальное вознаграждение (через центральную утилиту) ──────────
        # bot передаём None — уведомление отправится из payment_handlers после
        # активации, чтобы не дублировать (см. handle_successful_payment)
        try:
            from utils.referral import accrue_referral_reward
            await accrue_referral_reward(
                payer_user_id=user_id,
                amount_rub=plan.price_rub,
                source_label=f"тариф {plan.name}",
                bot=_bot_instance,   # глобальный bot (см. ниже)
                session=session,
            )
        except Exception as _ref_err:
            logger.warning(f"Referral accrual error: {_ref_err}")

        # Серверы тарифа
        servers = await get_servers_for_plan(session, plan_id)
        expire_ms = int(expires_at.timestamp() * 1000)

        if user.is_banned:
            logger.warning("Subscription activated for banned user %s; Xray access remains revoked", user_id)
            return True

        # A plan change must also revoke access to nodes that are no longer in
        # the new plan; otherwise cached links would keep working.
        stale_servers: list[Server] = []
        if previous_plan_id and previous_plan_id != plan_id:
            previous_servers = await get_servers_for_plan(session, previous_plan_id)
            new_ids = {server.id for server in servers}
            stale_servers = [server for server in previous_servers if server.id not in new_ids]

        # Формируем читаемый label для Xray node-agent: @username_id или просто id
        _uname = user.username or ""
        _user_label = f"@{_uname}_{user.id}" if _uname else str(user.id)

        tasks = [
            _add_client_to_server(srv, str(user.xray_uuid), expire_ms, plan.traffic_gb, _user_label)
            for srv in servers
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        success = sum(1 for r in results if r is True)
        if stale_servers:
            await asyncio.gather(
                *[_remove_client_from_server(server, str(user.xray_uuid)) for server in stale_servers],
                return_exceptions=True,
            )
        logger.info(f"Activated sub for user {user_id}: {success}/{len(servers)} servers OK")
        return True


async def _add_client_to_server(
    server: Server, uuid: str, expire_ms: int,
    traffic_gb: float | None, user_label: str | None = None
) -> bool:
    try:
        email = user_label if user_label else uuid
        async with XrayClient(
            server.node_url, node_path=server.node_path, node_token=server.node_token,
            node_cert=server.node_cert,
        ) as xray:
            return await xray.add_client(
                inbound_id=server.inbound_id,
                uuid=uuid,
                email=email,
                expire_ms=expire_ms,
                total_gb=traffic_gb or 0,
            )
    except Exception as e:
        logger.error(f"Failed to add client to {server.label}: {e}")
        return False


async def _remove_client_from_server(server: Server, uuid: str) -> bool:
    try:
        async with XrayClient(
            server.node_url, node_path=server.node_path, node_token=server.node_token,
            node_cert=server.node_cert,
        ) as xray:
            return await xray.delete_client(server.inbound_id, uuid)
    except Exception as exc:
        logger.error("Failed to remove client from %s: %s", server.label, exc)
        return False


async def revoke_user_access(user_id: int) -> tuple[int, int]:
    """Immediately remove a user from every ready Xray node."""
    async with AsyncSessionLocal() as session:
        user = await session.get(User, user_id)
        if not user:
            return 0, 0
        servers = await get_all_active_servers(session)
        uuid = str(user.xray_uuid)
    results = await asyncio.gather(
        *[_remove_client_from_server(server, uuid) for server in servers],
        return_exceptions=True,
    )
    success = sum(result is True for result in results)
    return success, len(servers)


async def restore_user_access(user_id: int) -> tuple[int, int]:
    """Restore a previously banned user when an active subscription exists."""
    async with AsyncSessionLocal() as session:
        user = await session.get(User, user_id)
        if not user or user.is_banned:
            return 0, 0
        subscription = await get_active_subscription(session, user_id)
        if not subscription or (subscription.expires_at and subscription.expires_at <= datetime.now()):
            return 0, 0
        servers = await get_servers_for_plan(session, subscription.plan_id)
        uuid = str(user.xray_uuid)
        expire_ms = int(subscription.expires_at.timestamp() * 1000) if subscription.expires_at else 0
        label = f"@{user.username}_{user.id}" if user.username else str(user.id)
        traffic = subscription.traffic_limit_gb
    results = await asyncio.gather(
        *[_add_client_to_server(server, uuid, expire_ms, traffic, label) for server in servers],
        return_exceptions=True,
    )
    success = sum(result is True for result in results)
    return success, len(servers)


async def sync_user_to_new_server(server: Server, session: AsyncSession):
    """При добавлении нового сервера — синхронизировать всех активных пользователей"""
    result = await session.execute(
        select(User, Subscription)
        .join(Subscription, Subscription.user_id == User.id)
        .where(Subscription.status == "active")
        .where(Subscription.expires_at > datetime.now())
    )
    rows = result.all()
    logger.info(f"Syncing {len(rows)} active users to new server {server.label}")

    tasks = []
    for user, sub in rows:
        expire_ms = int(sub.expires_at.timestamp() * 1000) if sub.expires_at else 0
        _uname = user.username or ""
        _user_label = f"@{_uname}_{user.id}" if _uname else str(user.id)
        tasks.append(_add_client_to_server(server, str(user.xray_uuid), expire_ms, sub.traffic_limit_gb, _user_label))

    results = await asyncio.gather(*tasks, return_exceptions=True)
    ok = sum(1 for r in results if r is True)
    logger.info(f"Sync done: {ok}/{len(rows)} users added to {server.label}")
    return ok


async def deactivate_expired():
    """Celery task — деактивировать истёкшие подписки"""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Subscription)
            .where(Subscription.status == "active")
            .where(Subscription.expires_at < datetime.now())
        )
        expired = result.scalars().all()
        for sub in expired:
            sub.status = "expired"
        await session.commit()
        logger.info(f"Deactivated {len(expired)} expired subscriptions")
