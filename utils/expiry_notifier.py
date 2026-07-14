"""
utils/expiry_notifier.py

Фоновый планировщик уведомлений об истечении подписки.
Запускается один раз при старте бота (start_expiry_notifier).

Логика:
  — каждые 1 час проверяем подписки
  — за 3 дня до истечения: уведомление «осталось 3 дня»
  — за 1 день до истечения: уведомление «истекает завтра»
  — в день истечения (0 дней): уведомление «сегодня последний день»
  — каждое уведомление отправляется только 1 раз (храним флаги в колонке notified_days)

Чтобы не добавлять новую колонку в БД — храним уже отправленные уведомления
в Redis (set expiry_notified:{user_id}:{threshold_days}).
Если Redis недоступен — fallback на in-memory set (сбрасывается при рестарте бота).
"""
import asyncio
import logging
import os
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# Через сколько дней до истечения слать уведомления
NOTIFY_THRESHOLDS = [3, 1, 0]

# Интервал проверки (секунды)
CHECK_INTERVAL = 3600  # 1 час

_notifier_task: asyncio.Task | None = None

# Fallback-хранилище если Redis недоступен
_sent_in_memory: set[str] = set()


async def _mark_sent(key: str) -> None:
    try:
        import aioredis
        redis = await aioredis.from_url(
            os.getenv("REDIS_URL", "redis://redis:6379/0"),
            decode_responses=True
        )
        # TTL 40 дней — автоочистка старых записей
        await redis.setex(f"expiry_notified:{key}", 40 * 86400, "1")
        await redis.aclose()
    except Exception:
        _sent_in_memory.add(key)


async def _is_sent(key: str) -> bool:
    try:
        import aioredis
        redis = await aioredis.from_url(
            os.getenv("REDIS_URL", "redis://redis:6379/0"),
            decode_responses=True
        )
        val = await redis.get(f"expiry_notified:{key}")
        await redis.aclose()
        return val is not None
    except Exception:
        return key in _sent_in_memory


async def _run_check(bot):
    """Проверяет все активные подписки и рассылает уведомления."""
    from db.database import AsyncSessionLocal
    from db.models import Subscription, User, Plan
    from sqlalchemy import select

    now = datetime.now()

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Subscription, User, Plan)
            .join(User, User.id == Subscription.user_id)
            .join(Plan, Plan.id == Subscription.plan_id)
            .where(Subscription.status == "active")
            .where(Subscription.expires_at.isnot(None))
        )
        rows = result.all()

    logger.info(f"Expiry notifier: checking {len(rows)} active subscriptions")

    for sub, user, plan in rows:
        if not sub.expires_at:
            continue

        days_left = (sub.expires_at.date() - now.date()).days

        # Проверяем каждый порог
        for threshold in NOTIFY_THRESHOLDS:
            if days_left != threshold:
                continue

            # Ключ уникальности: user_id + sub_id + threshold
            cache_key = f"{user.id}:{sub.id}:{threshold}"
            if await _is_sent(cache_key):
                continue

            # Формируем текст уведомления
            expire_str = sub.expires_at.strftime("%d.%m.%Y в %H:%M")
            plan_name = plan.name if plan else "VPN"

            if threshold == 0:
                header = "⚠️ <b>Подписка истекает сегодня!</b>"
                body = (
                    f"Ваша подписка <b>{plan_name}</b> истекает\n"
                    f"🗓 <b>{expire_str}</b>\n\n"
                    f"Продлите сейчас, чтобы не потерять доступ к VPN."
                )
            elif threshold == 1:
                header = "⏰ <b>Подписка истекает завтра</b>"
                body = (
                    f"Ваша подписка <b>{plan_name}</b> истекает\n"
                    f"🗓 <b>{expire_str}</b>\n\n"
                    f"Осталось <b>1 день</b> — успейте продлить!"
                )
            else:
                header = f"📅 <b>Подписка истекает через {threshold} дня</b>"
                body = (
                    f"Ваша подписка <b>{plan_name}</b> истекает\n"
                    f"🗓 <b>{expire_str}</b>\n\n"
                    f"Осталось <b>{threshold} дня</b> — не забудьте продлить!"
                )

            text = f"{header}\n\n{body}"

            from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔄 Продлить подписку", callback_data="buy")],
            ])

            try:
                await bot.send_message(
                    user.id,
                    text,
                    reply_markup=kb,
                    parse_mode="HTML"
                )
                await _mark_sent(cache_key)
                logger.info(
                    f"Expiry notice sent → user {user.id} "
                    f"(@{user.username}), days_left={threshold}, sub={sub.id}"
                )
            except Exception as e:
                logger.warning(f"Could not send expiry notice to {user.id}: {e}")


async def _notifier_loop(bot):
    """Бесконечный цикл проверки."""
    logger.info(f"Expiry notifier started (interval={CHECK_INTERVAL}s, thresholds={NOTIFY_THRESHOLDS}d)")
    # Первый запуск через 1 минуту после старта бота
    await asyncio.sleep(60)
    while True:
        try:
            await _run_check(bot)
        except Exception as e:
            logger.error(f"Expiry notifier loop error: {e}", exc_info=True)
        await asyncio.sleep(CHECK_INTERVAL)


def start_expiry_notifier(bot):
    """Запускает планировщик уведомлений. Вызывается при старте бота."""
    global _notifier_task
    if _notifier_task is None or _notifier_task.done():
        _notifier_task = asyncio.create_task(_notifier_loop(bot))
        logger.info("Expiry notifier task created")


def stop_expiry_notifier():
    global _notifier_task
    if _notifier_task and not _notifier_task.done():
        _notifier_task.cancel()
        _notifier_task = None
