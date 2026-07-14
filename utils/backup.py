"""
Backup модуль — создаёт дамп PostgreSQL и отправляет админу в Telegram.
Автоматически через Celery + вручную через /backup.
Восстановление через веб-админку.
"""
import os
import asyncio
import logging
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

BACKUP_DIR = Path(os.getenv("BACKUP_DIR", "/app/backups"))
BACKUP_DIR.mkdir(exist_ok=True)


def get_db_params() -> dict:
    """Парсим DATABASE_URL для pg_dump."""
    db_url = os.getenv("DATABASE_URL", "postgresql+asyncpg://vpn:vpnpass@db:5432/vpnbot")
    # postgresql+asyncpg://user:pass@host:port/dbname
    db_url = db_url.replace("postgresql+asyncpg://", "").replace("postgresql://", "")
    userpass, hostdb = db_url.split("@")
    user, password = userpass.split(":")
    hostport, dbname = hostdb.split("/")
    if ":" in hostport:
        host, port = hostport.split(":")
    else:
        host, port = hostport, "5432"
    return {"user": user, "password": password, "host": host, "port": port, "dbname": dbname}


async def create_backup() -> Path | None:
    """Создаёт pg_dump и возвращает путь к файлу."""
    try:
        params = get_db_params()
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = BACKUP_DIR / f"vpnbot_backup_{timestamp}.sql.gz"

        env = os.environ.copy()
        env["PGPASSWORD"] = params["password"]

        proc = await asyncio.create_subprocess_exec(
            "pg_dump",
            "-h", params["host"],
            "-p", params["port"],
            "-U", params["user"],
            "-d", params["dbname"],
            "--no-password",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env
        )
        stdout, stderr = await proc.communicate()

        if proc.returncode != 0:
            logger.error(f"pg_dump failed: {stderr.decode()}")
            return None

        # Gzip
        import gzip
        with gzip.open(backup_path, "wb") as f:
            f.write(stdout)

        logger.info(f"Backup created: {backup_path} ({backup_path.stat().st_size // 1024} KB)")
        return backup_path

    except Exception as e:
        logger.error(f"Backup error: {e}")
        return None


async def send_backup_to_admins(bot=None):
    """Создаёт бэкап и отправляет всем админам в Telegram."""
    backup_path = await create_backup()
    if not backup_path:
        return False

    if bot is None:
        from aiogram import Bot
        bot = Bot(token=os.getenv("BOT_TOKEN"))
        close_bot = True
    else:
        close_bot = False

    admin_ids = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
    size_kb = backup_path.stat().st_size // 1024
    timestamp = datetime.now().strftime("%d.%m.%Y %H:%M")

    success = False
    for admin_id in admin_ids:
        try:
            with open(backup_path, "rb") as f:
                await bot.send_document(
                    admin_id,
                    document=f,
                    filename=backup_path.name,
                    caption=(
                        f"💾 <b>Резервная копия базы данных</b>\n\n"
                        f"📅 Дата: <b>{timestamp}</b>\n"
                        f"📦 Размер: <b>{size_kb} KB</b>\n\n"
                        f"<i>Для восстановления загрузите файл в разделе "
                        f"\"Backup\" веб-админки.</i>"
                    ),
                    parse_mode="HTML"
                )
            success = True
        except Exception as e:
            logger.error(f"Failed to send backup to admin {admin_id}: {e}")

    if close_bot:
        await bot.session.close()

    # Удаляем старые бэкапы (оставляем последние 7)
    backups = sorted(BACKUP_DIR.glob("vpnbot_backup_*.sql.gz"), key=lambda p: p.stat().st_mtime)
    for old in backups[:-7]:
        old.unlink(missing_ok=True)

    return success


async def restore_backup(backup_file_path: str) -> tuple[bool, str]:
    """Восстанавливает БД из sql.gz файла."""
    try:
        params = get_db_params()
        env = os.environ.copy()
        env["PGPASSWORD"] = params["password"]

        import gzip
        with gzip.open(backup_file_path, "rb") as f:
            sql_content = f.read()

        proc = await asyncio.create_subprocess_exec(
            "psql",
            "-h", params["host"],
            "-p", params["port"],
            "-U", params["user"],
            "-d", params["dbname"],
            "--no-password",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env
        )
        stdout, stderr = await proc.communicate(input=sql_content)

        if proc.returncode != 0:
            return False, stderr.decode()[:500]

        return True, "OK"
    except Exception as e:
        return False, str(e)
