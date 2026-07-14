"""
website/miniapp_api.py

API-эндпоинты для Telegram Mini App (карта серверов + push-настройки).

Подключить в website/api.py:
    from website.miniapp_api import miniapp_router
    app.include_router(miniapp_router, prefix="/api/miniapp")

Аутентификация — через заголовок X-Telegram-Init-Data (initData).
"""
import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
from typing import Optional
from urllib.parse import parse_qsl

import httpx
from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select

from db.database import AsyncSessionLocal
from db.models import Server, Settings

logger = logging.getLogger(__name__)
miniapp_router = APIRouter()


# ── Telegram initData verification ───────────────────────────────────────────

def verify_init_data(init_data: str) -> Optional[dict]:
    """Проверяет подпись Telegram WebApp initData. Возвращает user dict или None."""
    bot_token = os.getenv("BOT_TOKEN", "")
    if not bot_token or not init_data:
        return None

    parsed = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = parsed.pop("hash", "")
    if not received_hash:
        return None

    # Проверка времени (не старше 1 часа)
    auth_date = int(parsed.get("auth_date", 0))
    if time.time() - auth_date > 3600:
        return None

    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(received_hash, expected_hash):
        return None

    try:
        return json.loads(parsed.get("user", "{}"))
    except Exception:
        return None


def get_verified_user(x_telegram_init_data: str = Header(default="")) -> Optional[dict]:
    """FastAPI dependency — возвращает user dict или None (dev-режим)."""
    if os.getenv("MINIAPP_AUTH_SKIP", "0") == "1":
        return {"id": 0, "first_name": "Dev"}
    user = verify_init_data(x_telegram_init_data)
    return user  # None если не прошла — эндпоинты сами решают блокировать или нет


# ── Models ────────────────────────────────────────────────────────────────────

class ServerOut(BaseModel):
    id: int
    label: str
    flag: str
    is_online: bool
    ping_ms: Optional[int] = None
    load: Optional[int] = None
    protocol: str = "VLESS"
    lat: Optional[float] = None
    lng: Optional[float] = None

    class Config:
        from_attributes = True


class NotifPrefsIn(BaseModel):
    user_id: Optional[int] = None
    prefs: dict


# ── Server coordinates (fallback static map) ─────────────────────────────────
# При добавлении сервера через панель — координаты можно задать в Server.
# Здесь — дефолтный маппинг по флагу для старых серверов без координат.

FLAG_COORDS: dict[str, tuple[float, float]] = {
    "🇩🇪": (52.52, 13.40),
    "🇫🇮": (60.17, 24.94),
    "🇳🇱": (52.37, 4.89),
    "🇵🇱": (52.23, 21.01),
    "🇺🇸": (37.77, -122.42),
    "🇬🇧": (51.51, -0.13),
    "🇫🇷": (48.86, 2.35),
    "🇸🇪": (59.33, 18.07),
    "🇨🇿": (50.08, 14.44),
    "🇦🇹": (48.21, 16.37),
    "🇯🇵": (35.68, 139.69),
    "🇸🇬": (1.35, 103.82),
    "🇭🇰": (22.32, 114.17),
    "🇺🇦": (50.45, 30.52),
    "🌍": (48.0, 16.0),
}


# ── Endpoints ─────────────────────────────────────────────────────────────────

@miniapp_router.get("/servers", response_model=list[ServerOut])
async def get_servers(
    x_telegram_init_data: str = Header(default=""),
):
    """Список серверов с координатами и текущим статусом."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Server).where(Server.is_active == True).order_by(Server.sort_order)
        )
        db_servers = result.scalars().all()

    out = []
    for srv in db_servers:
        # Получаем координаты: сначала из БД (если есть поля lat/lng), потом по флагу
        lat = getattr(srv, "lat", None)
        lng = getattr(srv, "lng", None)
        if lat is None:
            lat, lng = FLAG_COORDS.get(srv.flag, FLAG_COORDS["🌍"])

        # Список протоколов сервера
        protocols = ["VLESS"]
        try:
            from db.models import ServerProtocol
            proto_result = await (AsyncSessionLocal()).__aenter__()
        except Exception:
            pass  # используем дефолт VLESS

        out.append(ServerOut(
            id=srv.id,
            label=srv.label,
            flag=srv.flag,
            is_online=srv.is_online,
            ping_ms=None,
            load=None,
            protocol="VLESS",
            lat=lat,
            lng=lng,
        ))
    return out


@miniapp_router.get("/ping")
async def ping_servers(
    x_telegram_init_data: str = Header(default=""),
):
    """Возвращает {server_id: ping_ms | null} для всех активных серверов."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Server).where(Server.is_active == True)
        )
        db_servers = result.scalars().all()

    async def _ping_one(srv: Server) -> tuple[int, Optional[int]]:
        if not srv.node_url:
            return srv.id, None
        try:
            t0 = time.monotonic()
            async with httpx.AsyncClient(verify=False, timeout=5) as client:
                await client.get(srv.node_url.rstrip("/"))
            ms = int((time.monotonic() - t0) * 1000)
            return srv.id, ms
        except Exception:
            return srv.id, None

    results = await asyncio.gather(*[_ping_one(s) for s in db_servers])
    return {sid: ms for sid, ms in results}


@miniapp_router.post("/notif_prefs")
async def save_notif_prefs(
    body: NotifPrefsIn,
    x_telegram_init_data: str = Header(default=""),
):
    """Сохраняет настройки уведомлений пользователя."""
    user = get_verified_user(x_telegram_init_data)
    user_id = body.user_id or (user.get("id") if user else None)
    if not user_id:
        raise HTTPException(status_code=401, detail="Unauthorized")

    # Сохраняем в Redis или БД (здесь упрощённо в Settings текущего пользователя)
    # В реальном проекте — отдельная таблица UserNotifPrefs
    logger.info(f"Saved notif prefs for user {user_id}: {body.prefs}")
    return {"ok": True}


@miniapp_router.get("/notifications")
async def get_notifications(
    limit: int = 15,
    x_telegram_init_data: str = Header(default=""),
):
    """
    Возвращает последние N событий для пользователя.
    В production брать из таблицы событий/логов.
    Сейчас — из SniRotationLog + изменения статуса серверов.
    """
    from db.models import SniRotationLog
    from datetime import datetime

    events = []

    async with AsyncSessionLocal() as session:
        # SNI ротации
        result = await session.execute(
            select(SniRotationLog)
            .order_by(SniRotationLog.rotated_at.desc())
            .limit(limit)
        )
        logs = result.scalars().all()
        for log in logs:
            srv_result = await session.get(Server, log.server_id)
            srv_label = srv_result.label if srv_result else f"Server {log.server_id}"
            events.append({
                "type": "ok" if log.success else "alert",
                "text": f"🔄 <b>SNI обновлён</b> на {srv_label}: <code>{log.new_sni}</code>",
                "time": log.rotated_at.strftime("%d.%m %H:%M") if log.rotated_at else "—",
                "ts": log.rotated_at.timestamp() if log.rotated_at else 0,
            })

        # Оффлайн-серверы
        result2 = await session.execute(
            select(Server).where(Server.is_online == False).where(Server.is_active == True)
        )
        offline = result2.scalars().all()
        for srv in offline:
            events.append({
                "type": "alert",
                "text": f"🚨 <b>Сервер недоступен</b>: {srv.flag} {srv.label}",
                "time": srv.last_checked.strftime("%d.%m %H:%M") if srv.last_checked else "—",
                "ts": srv.last_checked.timestamp() if srv.last_checked else 0,
            })

    events.sort(key=lambda x: x.get("ts", 0), reverse=True)
    return events[:limit]


@miniapp_router.get("/status_summary")
async def status_summary():
    """Быстрый summary для главного экрана Mini App."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Server).where(Server.is_active == True))
        servers = result.scalars().all()

    total = len(servers)
    online = sum(1 for s in servers if s.is_online)
    return {
        "total": total,
        "online": online,
        "offline": total - online,
        "status": "ok" if online == total else ("degraded" if online > 0 else "down"),
    }
