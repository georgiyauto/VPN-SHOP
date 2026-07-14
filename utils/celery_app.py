import asyncio
import os
import logging
from datetime import datetime
from celery import Celery
from celery.schedules import crontab

redis_url = os.getenv("REDIS_URL", "redis://redis:6379/0")
app = Celery("vpnbot", broker=redis_url, backend=redis_url)

app.conf.beat_schedule = {
    # Деактивация истёкших подписок — каждый час в :00
    "deactivate-expired-every-hour": {
        "task": "utils.celery_app.deactivate_expired_task",
        "schedule": crontab(minute="0"),
    },
    # Уведомления об истечении — каждый день в 10:00
    "notify-expiring-daily": {
        "task": "utils.celery_app.notify_expiring_task",
        "schedule": crontab(hour="10", minute="0"),
    },
    # SNI ротация — каждый час в :30 (не пересекается с деактивацией)
    "rotate-sni-every-hour": {
        "task": "utils.celery_app.rotate_sni_task",
        "schedule": crontab(minute="30"),
    },
    # Мониторинг серверов — каждые 5 минут
    "monitor-servers": {
        "task": "utils.celery_app.monitor_servers_task",
        "schedule": crontab(minute="*/5"),
    },
    # Напоминания о продлении — каждый день в 11:00
    "renewal-reminders-daily": {
        "task": "utils.celery_app.renewal_reminders_task",
        "schedule": crontab(hour="11", minute="0"),
    },
    # Автобэкап БД — каждый день в 03:00
    "auto-backup-daily": {
        "task": "utils.celery_app.auto_backup_task",
        "schedule": crontab(hour="3", minute="0"),
    },
}
app.conf.timezone = "Europe/Moscow"

logger = logging.getLogger(__name__)


def run_async(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ── Деактивация истёкших ──────────────────────────────────────────────────────

@app.task(name="utils.celery_app.deactivate_expired_task")
def deactivate_expired_task():
    from bot.services.vpn_service import deactivate_expired
    run_async(deactivate_expired())


# ── Уведомления об истечении ──────────────────────────────────────────────────

@app.task(name="utils.celery_app.notify_expiring_task")
def notify_expiring_task():
    run_async(_notify_expiring())


async def _notify_expiring():
    from datetime import datetime, timedelta
    from sqlalchemy import select
    from db.database import AsyncSessionLocal
    from db.models import User, Subscription
    from aiogram import Bot

    bot = Bot(token=os.getenv("BOT_TOKEN"))
    try:
        async with AsyncSessionLocal() as session:
            soon = datetime.now() + timedelta(days=3)
            result = await session.execute(
                select(User, Subscription)
                .join(Subscription, Subscription.user_id == User.id)
                .where(Subscription.status == "active")
                .where(Subscription.expires_at < soon)
                .where(Subscription.expires_at > datetime.now())
            )
            for user, sub in result.all():
                days_left = (sub.expires_at - datetime.now()).days
                try:
                    await bot.send_message(
                        user.id,
                        f"⚠️ <b>Внимание!</b>\n\nВаша подписка истекает через <b>{days_left} дн.</b>\nНе забудьте продлить!",
                        parse_mode="HTML"
                    )
                except Exception:
                    pass
    finally:
        await bot.session.close()


# ── SNI ротация ───────────────────────────────────────────────────────────────

@app.task(name="utils.celery_app.rotate_sni_task")
def rotate_sni_task():
    """
    Каждый час меняет SNI на всех серверах у которых включена ротация.
    Подписки обновляются автоматически — /sub/{token} читает
    актуальный SNI из панели при каждом запросе.
    """
    run_async(_rotate_all_sni())


async def _rotate_all_sni():
    from sqlalchemy import select
    from db.database import AsyncSessionLocal
    from db.models import Server, SniRotationLog
    from utils.sni_rotation import rotate_sni_on_server

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Server)
            .where(Server.is_active == True)
            .where(Server.sni_rotation_enabled == True)
        )
        servers = result.scalars().all()

    if not servers:
        logger.info("SNI rotation: no servers enabled, skipping")
        return

    logger.info(f"SNI rotation: starting for {len(servers)} servers")

    for server in servers:
        res = await rotate_sni_on_server(
            node_url=server.node_url,
            node_token=server.node_token,
            inbound_id=server.inbound_id,
            node_path=server.node_path or "/",
            node_cert=server.node_cert,
        )

        # Обновляем current_sni и время последней ротации в таблице servers
        async with AsyncSessionLocal() as session:
            srv = await session.get(Server, server.id)
            if srv:
                if res["ok"]:
                    srv.current_sni = res["sni"]
                    srv.sni_last_rotated = datetime.now()
                log = SniRotationLog(
                    server_id=server.id,
                    new_sni=res.get("sni", ""),
                    fingerprint=res.get("fingerprint", ""),
                    success=res["ok"],
                    error=res.get("error"),
                )
                session.add(log)
                await session.commit()

        if res["ok"]:
            logger.info(f"[{server.label}] SNI → {res['sni']} ({res['fingerprint']})")
            await _notify_log(server.label, res["sni"])
        else:
            logger.error(f"[{server.label}] SNI rotation FAILED: {res['error']}")


async def _notify_log(server_label: str, new_sni: str):
    """Логируем в Telegram чат если задан SNI_LOG_CHAT_ID."""
    log_chat = os.getenv("SNI_LOG_CHAT_ID")
    if not log_chat:
        return
    try:
        from aiogram import Bot
        bot = Bot(token=os.getenv("BOT_TOKEN"))
        await bot.send_message(
            int(log_chat),
            f"🔄 <b>SNI ротация</b>\n"
            f"Сервер: <b>{server_label}</b>\n"
            f"Новый SNI: <code>{new_sni}</code>",
            parse_mode="HTML"
        )
        await bot.session.close()
    except Exception:
        pass


# ── Автобэкап ─────────────────────────────────────────────────────────────────

@app.task(name="utils.celery_app.auto_backup_task")
def auto_backup_task():
    """Ежедневный автобэкап БД — отправляет файл всем админам в Telegram."""
    run_async(_do_auto_backup())


async def _do_auto_backup():
    from sqlalchemy import select
    from db.database import AsyncSessionLocal
    from db.models import Settings
    from utils.backup import send_backup_to_admins

    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Settings).where(Settings.id == 1))
        settings = result.scalar_one_or_none()
        if settings and not settings.auto_backup_enabled:
            logger.info("Auto backup disabled in settings, skipping")
            return

    ok = await send_backup_to_admins()
    if ok:
        logger.info("Auto backup completed successfully")
    else:
        logger.error("Auto backup FAILED")


# ── Мониторинг серверов каждые 5 минут ───────────────────────────────────────

@app.task(name="utils.celery_app.monitor_servers_task")
def monitor_servers_task():
    run_async(_monitor_servers())


async def _monitor_servers():
    from utils.monitoring import run_monitoring
    await run_monitoring()


# ── Напоминание о продлении — каждый день в 11:00 ────────────────────────────

@app.task(name="utils.celery_app.renewal_reminders_task")
def renewal_reminders_task():
    run_async(_renewal_reminders())


async def _renewal_reminders():
    from utils.monitoring import run_renewal_reminders
    await run_renewal_reminders()
