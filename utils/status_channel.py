"""
utils/status_channel.py

🔔 Публикация в Telegram-канал статусов.

Вызывается из:
  - utils/monitoring.py     — при падении/восстановлении сервера
  - utils/sni_rotation.py   — после каждой SNI-ротации
  - utils/celery_app.py     — через Celery-задачи

Настройка: в Settings.status_channel_id указать @channel или числовой chat_id.
Бот должен быть администратором канала.
"""
import os
import logging
from datetime import datetime

from aiogram import Bot

logger = logging.getLogger(__name__)


async def _get_status_channel() -> tuple[str | None, bool, bool]:
    """Возвращает (channel_id, server_alerts_enabled, sni_alerts_enabled)."""
    try:
        from sqlalchemy import select
        from db.database import AsyncSessionLocal
        from db.models import Settings
        async with AsyncSessionLocal() as session:
            result = await session.execute(select(Settings).where(Settings.id == 1))
            settings = result.scalar_one_or_none()
            if settings:
                return (
                    settings.status_channel_id,
                    settings.status_channel_alerts,
                    settings.status_channel_sni_alerts,
                )
    except Exception as e:
        logger.warning(f"Could not load Settings for status channel: {e}")
    # fallback к env
    return (
        os.getenv("STATUS_CHANNEL_ID"),
        True,
        True,
    )


async def post_server_down(server_label: str, server_url: str, flag: str = "🖥️"):
    """Публикует в канал сообщение о падении сервера."""
    channel_id, alerts_on, _ = await _get_status_channel()
    if not channel_id or not alerts_on:
        return

    text = (
        f"🚨 <b>Сервер недоступен</b>\n\n"
        f"{flag} <b>{server_label}</b>\n"
        f"🌐 {server_url}\n"
        f"🕐 {datetime.now().strftime('%d.%m.%Y %H:%M')} UTC\n\n"
        f"<i>Ведутся работы по восстановлению...</i>"
    )
    await _send_to_channel(channel_id, text)


async def post_server_up(server_label: str, server_url: str, flag: str = "🖥️",
                         downtime_minutes: int = 0):
    """Публикует в канал сообщение о восстановлении сервера."""
    channel_id, alerts_on, _ = await _get_status_channel()
    if not channel_id or not alerts_on:
        return

    downtime_str = f"\n⏱️ Простой: ~{downtime_minutes} мин." if downtime_minutes > 0 else ""
    text = (
        f"✅ <b>Сервер восстановлен</b>\n\n"
        f"{flag} <b>{server_label}</b>\n"
        f"🌐 {server_url}\n"
        f"🕐 {datetime.now().strftime('%d.%m.%Y %H:%M')} UTC"
        f"{downtime_str}"
    )
    await _send_to_channel(channel_id, text)


async def post_sni_rotated(server_label: str, old_sni: str, new_sni: str,
                            fingerprint: str = "", flag: str = "🖥️"):
    """Публикует в канал информацию о смене SNI."""
    channel_id, _, sni_alerts_on = await _get_status_channel()
    if not channel_id or not sni_alerts_on:
        return

    fp_str = f"\n🔐 Fingerprint: <code>{fingerprint}</code>" if fingerprint else ""
    text = (
        f"🔄 <b>SNI обновлён</b>\n\n"
        f"{flag} <b>{server_label}</b>\n"
        f"Было: <code>{old_sni}</code>\n"
        f"Стало: <code>{new_sni}</code>"
        f"{fp_str}\n"
        f"🕐 {datetime.now().strftime('%d.%m.%Y %H:%M')} UTC"
    )
    await _send_to_channel(channel_id, text)


async def post_maintenance(message: str):
    """Произвольное сообщение технического обслуживания в канал."""
    channel_id, _, _ = await _get_status_channel()
    if not channel_id:
        return
    await _send_to_channel(channel_id, f"🔧 <b>Техническое обслуживание</b>\n\n{message}")


async def _send_to_channel(channel_id: str, text: str):
    """Низкоуровневая отправка сообщения в канал."""
    bot_token = os.getenv("BOT_TOKEN")
    if not bot_token:
        logger.error("BOT_TOKEN not set, cannot post to status channel")
        return
    bot = Bot(token=bot_token)
    try:
        await bot.send_message(channel_id, text, parse_mode="HTML")
        logger.info(f"Posted to status channel {channel_id}: {text[:60]}")
    except Exception as e:
        logger.error(f"Failed to post to status channel {channel_id}: {e}")
    finally:
        await bot.session.close()
