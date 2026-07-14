"""
Адаптер БД для бота поддержки.
Реализует тот же интерфейс что handlers.py ожидает от shop_bot.data_manager.database,
но работает с нашими SQLAlchemy моделями через asyncio.
"""
import os
import asyncio
import logging
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)


def _run(coro):
    """Запускает корутину синхронно (handlers.py использует синхронный API)."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                future = pool.submit(asyncio.run, coro)
                return future.result(timeout=10)
        else:
            return loop.run_until_complete(coro)
    except Exception as e:
        logger.error(f"DB adapter error: {e}")
        return None


# ─── Settings ────────────────────────────────────────────────────────────────

def get_setting(key: str):
    async def _get():
        from db.database import AsyncSessionLocal
        from db.models import Settings
        from sqlalchemy import select
        async with AsyncSessionLocal() as session:
            result = await session.execute(select(Settings).where(Settings.id == 1))
            s = result.scalar_one_or_none()
            if s:
                return getattr(s, key, None)
        return None
    return _run(_get())


# ─── Admin helpers ────────────────────────────────────────────────────────────

def is_admin(user_id: int) -> bool:
    ids = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()]
    if user_id in ids:
        return True
    async def _check():
        from db.database import AsyncSessionLocal
        from db.models import Settings
        from sqlalchemy import select
        async with AsyncSessionLocal() as session:
            result = await session.execute(select(Settings).where(Settings.id == 1))
            s = result.scalar_one_or_none()
            if s and s.support_operator_ids:
                op_ids = [int(x) for x in s.support_operator_ids.split(",") if x.strip().isdigit()]
                return user_id in op_ids
        return False
    return bool(_run(_check()))


def get_admin_ids() -> list:
    ids = [x for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()]
    async def _get():
        from db.database import AsyncSessionLocal
        from db.models import Settings
        from sqlalchemy import select
        async with AsyncSessionLocal() as session:
            result = await session.execute(select(Settings).where(Settings.id == 1))
            s = result.scalar_one_or_none()
            if s and s.support_operator_ids:
                return [x.strip() for x in s.support_operator_ids.split(",") if x.strip().isdigit()]
        return []
    extra = _run(_get()) or []
    return list(set(ids + extra))


# ─── Tickets ─────────────────────────────────────────────────────────────────

def create_support_ticket(user_id: int, subject: Optional[str]) -> Optional[int]:
    async def _create():
        from db.database import AsyncSessionLocal
        from db.models import SupportTicket, User
        async with AsyncSessionLocal() as session:
            # Ensure user exists
            user = await session.get(User, user_id)
            if not user:
                from db.models import User
                user = User(id=user_id, username=None, full_name=None)
                session.add(user)
                await session.flush()
            ticket = SupportTicket(
                user_id=user_id,
                text=subject or "Обращение без темы",
                subject=subject,
                status="open",
            )
            session.add(ticket)
            await session.commit()
            await session.refresh(ticket)
            return ticket.id
    return _run(_create())


def get_ticket(ticket_id: int) -> Optional[dict]:
    async def _get():
        from db.database import AsyncSessionLocal
        from db.models import SupportTicket
        async with AsyncSessionLocal() as session:
            t = await session.get(SupportTicket, ticket_id)
            if not t:
                return None
            return {
                "ticket_id": t.id,
                "user_id": t.user_id,
                "subject": t.subject or t.text,
                "text": t.text,
                "status": t.status,
                "answer": t.answer,
                "forum_chat_id": t.forum_chat_id,
                "message_thread_id": t.message_thread_id,
                "created_at": str(t.created_at)[:16] if t.created_at else None,
            }
    return _run(_get())


def get_user_tickets(user_id: int) -> list:
    async def _get():
        from db.database import AsyncSessionLocal
        from db.models import SupportTicket
        from sqlalchemy import select
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(SupportTicket)
                .where(SupportTicket.user_id == user_id)
                .order_by(SupportTicket.id.desc())
                .limit(20)
            )
            tickets = result.scalars().all()
            return [
                {
                    "ticket_id": t.id,
                    "user_id": t.user_id,
                    "subject": t.subject or t.text,
                    "status": t.status,
                    "forum_chat_id": t.forum_chat_id,
                    "message_thread_id": t.message_thread_id,
                    "created_at": str(t.created_at)[:16] if t.created_at else None,
                }
                for t in tickets
            ]
    return _run(_get()) or []


def get_ticket_by_thread(forum_chat_id: str, thread_id: int) -> Optional[dict]:
    async def _get():
        from db.database import AsyncSessionLocal
        from db.models import SupportTicket
        from sqlalchemy import select
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(SupportTicket)
                .where(SupportTicket.forum_chat_id == str(forum_chat_id))
                .where(SupportTicket.message_thread_id == thread_id)
            )
            t = result.scalar_one_or_none()
            if not t:
                return None
            return {
                "ticket_id": t.id,
                "user_id": t.user_id,
                "subject": t.subject or t.text,
                "status": t.status,
                "forum_chat_id": t.forum_chat_id,
                "message_thread_id": t.message_thread_id,
            }
    return _run(_get())


def set_ticket_status(ticket_id: int, status: str) -> bool:
    async def _set():
        from db.database import AsyncSessionLocal
        from db.models import SupportTicket
        async with AsyncSessionLocal() as session:
            t = await session.get(SupportTicket, ticket_id)
            if not t:
                return False
            t.status = status
            if status == "closed":
                t.answered_at = datetime.now()
            await session.commit()
            return True
    return bool(_run(_set()))


def update_ticket_thread_info(ticket_id: int, forum_chat_id: str, thread_id: int) -> bool:
    async def _update():
        from db.database import AsyncSessionLocal
        from db.models import SupportTicket
        async with AsyncSessionLocal() as session:
            t = await session.get(SupportTicket, ticket_id)
            if not t:
                return False
            t.forum_chat_id = str(forum_chat_id)
            t.message_thread_id = int(thread_id)
            await session.commit()
            return True
    return bool(_run(_update()))


def update_ticket_subject(ticket_id: int, subject: str) -> bool:
    async def _update():
        from db.database import AsyncSessionLocal
        from db.models import SupportTicket
        async with AsyncSessionLocal() as session:
            t = await session.get(SupportTicket, ticket_id)
            if not t:
                return False
            t.subject = subject
            t.text = subject
            await session.commit()
            return True
    return bool(_run(_update()))


def delete_ticket(ticket_id: int) -> bool:
    async def _del():
        from db.database import AsyncSessionLocal
        from db.models import SupportTicket
        async with AsyncSessionLocal() as session:
            t = await session.get(SupportTicket, ticket_id)
            if not t:
                return False
            await session.delete(t)
            await session.commit()
            return True
    return bool(_run(_del()))


# ─── Messages ────────────────────────────────────────────────────────────────

def add_support_message(ticket_id: int, sender: str, content: str) -> bool:
    async def _add():
        from db.database import AsyncSessionLocal
        from db.models import SupportMessage
        async with AsyncSessionLocal() as session:
            msg = SupportMessage(
                ticket_id=ticket_id,
                sender=sender,
                content=content,
            )
            session.add(msg)
            # Also update ticket.answer if admin reply
            if sender == "admin":
                from db.models import SupportTicket
                t = await session.get(SupportTicket, ticket_id)
                if t:
                    t.answer = content
                    t.status = "answered"
                    t.answered_at = datetime.now()
            await session.commit()
            return True
    return bool(_run(_add()))


def get_ticket_messages(ticket_id: int) -> list:
    async def _get():
        from db.database import AsyncSessionLocal
        from db.models import SupportMessage
        from sqlalchemy import select
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(SupportMessage)
                .where(SupportMessage.ticket_id == ticket_id)
                .order_by(SupportMessage.id)
            )
            msgs = result.scalars().all()
            return [
                {
                    "id": m.id,
                    "sender": m.sender,
                    "content": m.content,
                    "created_at": str(m.created_at)[:16] if m.created_at else None,
                }
                for m in msgs
            ]
    return _run(_get()) or []
