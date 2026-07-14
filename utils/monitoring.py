"""Background health checks for authenticated Xray node-agents."""
import asyncio
import logging
from datetime import datetime
logger = logging.getLogger(__name__)
_monitor_task: asyncio.Task | None = None
INTERVAL = 30  # секунд
async def _check_all_servers():
    """Проверяет все активные серверы и обновляет is_online в БД."""
    try:
        from db.database import AsyncSessionLocal
        from db.models import Server
        from bot.services.xray_client import XrayClient
        from sqlalchemy import select
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(Server).where(Server.is_active == True)
            )
            servers = result.scalars().all()
            server_data = [
                (s.id, s.node_url, s.node_path or "/", s.node_token, s.node_cert)
                for s in servers if s.node_url and s.node_token
            ]
        if not server_data:
            return
        async def check_one(sid, url, path, node_token, node_cert):
            client = XrayClient(url, node_path=path, node_token=node_token, node_cert=node_cert)
            online = await client.ping()
            return sid, online
        results = await asyncio.gather(
            *[check_one(*sd) for sd in server_data],
            return_exceptions=True
        )
        async with AsyncSessionLocal() as session:
            for item in results:
                if isinstance(item, Exception):
                    continue
                sid, online = item
                srv = await session.get(Server, sid)
                if srv:
                    was_online = srv.is_online
                    srv.is_online = online
                    srv.last_checked = datetime.now()
                    if online:
                        srv.last_online = datetime.now()
                    if not online and was_online:
                        logger.warning(f"Server {sid} ({srv.label}) went OFFLINE")
                    elif online and not was_online:
                        logger.info(f"Server {sid} ({srv.label}) came back ONLINE")
            await session.commit()
    except Exception as e:
        logger.error(f"Monitoring error: {e}")
async def _monitor_loop():
    """Бесконечный цикл мониторинга."""
    logger.info(f"Server monitoring started (interval={INTERVAL}s)")
    while True:
        try:
            await _check_all_servers()
        except Exception as e:
            logger.error(f"Monitor loop error: {e}")
        await asyncio.sleep(INTERVAL)
def start_monitoring():
    """Запускает фоновый мониторинг. Вызывается при старте бота."""
    global _monitor_task
    if _monitor_task is None or _monitor_task.done():
        _monitor_task = asyncio.create_task(_monitor_loop())
        logger.info("Monitoring task created")
def stop_monitoring():
    global _monitor_task
    if _monitor_task and not _monitor_task.done():
        _monitor_task.cancel()
        _monitor_task = None
