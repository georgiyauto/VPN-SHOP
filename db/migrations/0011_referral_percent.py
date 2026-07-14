"""Migration 0011: referral_percent в settings + таблица referral_logs"""
import asyncio, sys
sys.path.insert(0, '/app')

from db.database import engine
from sqlalchemy import text


async def migrate():
    async with engine.begin() as conn:
        # referral_percent в settings
        await conn.execute(text("""
            ALTER TABLE settings
            ADD COLUMN IF NOT EXISTS referral_percent FLOAT DEFAULT 10.0
        """))
        print("  + settings.referral_percent")

        # таблица referral_logs
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS referral_logs (
                id          SERIAL PRIMARY KEY,
                referrer_id BIGINT NOT NULL REFERENCES users(id),
                payer_id    BIGINT NOT NULL REFERENCES users(id),
                amount      FLOAT  NOT NULL,
                source      VARCHAR(200),
                created_at  TIMESTAMP DEFAULT NOW()
            )
        """))
        print("  + table referral_logs")

        print("Migration 0011 done ✅")


asyncio.run(migrate())
