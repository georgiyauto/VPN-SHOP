"""Add partner_balance and partner_earned to users"""
import asyncio
from db.database import engine
from sqlalchemy import text

async def upgrade():
    async with engine.begin() as conn:
        await conn.execute(text(
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS partner_balance FLOAT DEFAULT 0.0"
        ))
        await conn.execute(text(
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS partner_earned FLOAT DEFAULT 0.0"
        ))
    print("Migration 0006: partner_balance columns added")

if __name__ == "__main__":
    asyncio.run(upgrade())
