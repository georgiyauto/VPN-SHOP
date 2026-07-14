"""Add privacy_policy_url and terms_of_service_url to settings"""
import asyncio
from db.database import engine
from sqlalchemy import text

async def upgrade():
    async with engine.begin() as conn:
        await conn.execute(text(
            "ALTER TABLE settings ADD COLUMN IF NOT EXISTS privacy_policy_url VARCHAR(500)"
        ))
        await conn.execute(text(
            "ALTER TABLE settings ADD COLUMN IF NOT EXISTS terms_of_service_url VARCHAR(500)"
        ))
    print("Migration 0007: legal url columns added")

if __name__ == "__main__":
    asyncio.run(upgrade())
