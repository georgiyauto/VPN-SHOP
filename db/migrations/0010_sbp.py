"""Migration 0010: СБП Platega.io — поля в settings + pay_url в payments"""
import asyncio, sys
sys.path.insert(0, '/app')

from db.database import engine
from sqlalchemy import text


async def migrate():
    async with engine.begin() as conn:
        # Настройки СБП в таблице settings
        for col, col_type, default in [
            ("sbp_enabled",     "BOOLEAN",      "DEFAULT FALSE"),
            ("sbp_merchant_id", "VARCHAR(200)",  ""),
            ("sbp_secret_key",  "VARCHAR(200)",  ""),
        ]:
            try:
                await conn.execute(text(
                    f"ALTER TABLE settings ADD COLUMN IF NOT EXISTS {col} {col_type} {default}"
                ))
                print(f"  + settings.{col}")
            except Exception as e:
                print(f"  skip settings.{col}: {e}")

        # pay_url в payments для хранения ссылки на оплату
        try:
            await conn.execute(text(
                "ALTER TABLE payments ADD COLUMN IF NOT EXISTS pay_url VARCHAR(500)"
            ))
            print("  + payments.pay_url")
        except Exception as e:
            print(f"  skip payments.pay_url: {e}")

        print("Migration 0010 done ✅")


asyncio.run(migrate())
