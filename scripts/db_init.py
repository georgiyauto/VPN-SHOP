"""
db_init.py — запускается supervisord при старте контейнера.
Ждёт PostgreSQL и создаёт таблицы.
"""
import asyncio
import time
import sys
import os

sys.path.insert(0, '/app')

# Load .env
try:
    from dotenv import load_dotenv
    load_dotenv('/app/.env')
except Exception:
    pass


async def wait_for_postgres(max_retries=30):
    import asyncpg
    db_url = os.getenv('DATABASE_URL', '')
    # Convert asyncpg URL format
    dsn = db_url.replace('postgresql+asyncpg://', 'postgresql://')
    
    for attempt in range(max_retries):
        try:
            conn = await asyncpg.connect(dsn, timeout=5)
            await conn.close()
            print(f"[db_init] PostgreSQL ready (attempt {attempt+1})")
            return True
        except Exception as e:
            print(f"[db_init] Waiting for PostgreSQL... ({attempt+1}/{max_retries}): {e}")
            time.sleep(2)
    return False


async def main():
    print("[db_init] Starting DB initialization...")
    
    ok = await wait_for_postgres()
    if not ok:
        print("[db_init] ERROR: PostgreSQL not available after retries")
        sys.exit(1)
    
    from db.database import init_db
    await init_db()
    print("[db_init] Tables created/verified OK")

    # A fresh installation always starts with its local plain-Xray node.
    from sqlalchemy import select
    from db.database import AsyncSessionLocal
    from db.models import Server
    token = os.getenv("XRAY_NODE_TOKEN", "").strip()
    if token:
        async with AsyncSessionLocal() as session:
            existing = (await session.execute(select(Server).limit(1))).scalar_one_or_none()
            if not existing:
                session.add(Server(
                    label="Local Xray",
                    flag="🌐",
                    node_url="http://127.0.0.1:8090",
                    node_path="/",
                    node_token=token,
                    inbound_id=1,
                    ssh_host=os.getenv("XRAY_PUBLIC_HOST") or None,
                    install_status="ready",
                    install_log="Local plain-Xray node registered",
                    is_online=True,
                ))
                await session.commit()
                print("[db_init] Local Xray node registered")


if __name__ == '__main__':
    asyncio.run(main())
