from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy import select, text
from typing import AsyncGenerator
import os

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql+asyncpg://vpnbot:vpnbot_secret@postgres:5432/vpnbot")

engine = create_async_engine(DATABASE_URL, echo=False, pool_pre_ping=True)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def init_db():
    from db.models import Base, Settings, Plan
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Safe column migrations — idempotent ALTER TABLE IF NOT EXISTS
    # Needed when upgrading existing installations (v11 → v13)
    _safe_cols = [
        ("users", "google_id",        "VARCHAR(100)"),
        ("users", "google_email",     "VARCHAR(200)"),
        ("users", "web_token",        "VARCHAR(100)"),
        ("users", "web_password_hash","VARCHAR(200)"),
        ("settings", "info_text",            "TEXT"),
        ("settings", "support_operator_ids",  "VARCHAR(500)"),
    ]
    async with engine.begin() as conn:
        for table, col, col_type in _safe_cols:
            try:
                await conn.execute(
                    text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col} {col_type}")
                )
            except Exception:
                pass

    # Создаём plan_servers таблицу если не существует (миграция для старых установок)
    async with engine.begin() as conn:
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS plan_servers (
                id SERIAL PRIMARY KEY,
                plan_id INTEGER NOT NULL REFERENCES plans(id),
                server_id INTEGER NOT NULL REFERENCES servers(id)
            )
        """))

    # Создаём настройки по умолчанию если нет
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Settings).where(Settings.id == 1))
        if not result.scalar_one_or_none():
            session.add(Settings(id=1))

        # Создаём стандартные тарифы если нет
        plans_count = await session.execute(select(Plan))
        if not plans_count.scalars().all():
            session.add_all([
                Plan(name="🔹 Базовый · 1 месяц",  price_rub=199,  duration_days=30,  traffic_gb=50,   sort_order=1),
                Plan(name="🔷 Стандарт · 3 месяца", price_rub=499,  duration_days=90,  traffic_gb=150,  sort_order=2),
                Plan(name="💎 Премиум · 1 год",     price_rub=1499, duration_days=365, traffic_gb=None, sort_order=3),
            ])
        await session.commit()
