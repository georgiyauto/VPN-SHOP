"""Migration 0009: support_bot_token, support_bot_username, support_forum_chat_id, project_name"""
import asyncio, sys, os
sys.path.insert(0, '/app')

from db.database import engine
from sqlalchemy import text

async def migrate():
    async with engine.begin() as conn:
        await conn.execute(text("""
            ALTER TABLE settings
            ADD COLUMN IF NOT EXISTS support_bot_token    VARCHAR(200),
            ADD COLUMN IF NOT EXISTS support_bot_username VARCHAR(100),
            ADD COLUMN IF NOT EXISTS support_forum_chat_id VARCHAR(100),
            ADD COLUMN IF NOT EXISTS project_name         VARCHAR(100) DEFAULT '⚡ VPN ⚡'
        """))

        # Add forum fields to support_tickets
        await conn.execute(text("""
            ALTER TABLE support_tickets
            ADD COLUMN IF NOT EXISTS subject          VARCHAR(200),
            ADD COLUMN IF NOT EXISTS forum_chat_id    VARCHAR(100),
            ADD COLUMN IF NOT EXISTS message_thread_id BIGINT
        """))

        # Create support_messages table for per-ticket message history
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS support_messages (
                id          SERIAL PRIMARY KEY,
                ticket_id   INTEGER NOT NULL REFERENCES support_tickets(id) ON DELETE CASCADE,
                sender      VARCHAR(20) NOT NULL,  -- user / admin / note
                content     TEXT,
                created_at  TIMESTAMP DEFAULT NOW()
            )
        """))
        print("Migration 0009: support bot & forum columns added")

asyncio.run(migrate())
