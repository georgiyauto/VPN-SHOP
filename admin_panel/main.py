import os
import logging
from datetime import datetime, timedelta
from typing import Optional

from fastapi import FastAPI, HTTPException, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel
from sqlalchemy import select, func
from sqlalchemy import String
import secrets

from db.database import AsyncSessionLocal, init_db
from db.models import User, Subscription, Plan, Server, Payment, Settings, SupportTicket, BalanceLog, PlanServer
from bot.services.vpn_service import activate_subscription, sync_user_to_new_server

app = FastAPI(title="VPN Admin Panel")
security = HTTPBasic()
logger = logging.getLogger(__name__)

# Static files & templates
BASE_DIR = os.path.dirname(__file__)
app.mount("/static", StaticFiles(directory=f"{BASE_DIR}/static"), name="static")
templates = Jinja2Templates(directory=f"{BASE_DIR}/templates")


@app.on_event("startup")
async def startup():
    await init_db()


# ─── Auth ────────────────────────────────────────────────────────────────────

def verify_admin(credentials: HTTPBasicCredentials = Depends(security)):
    username_ok = secrets.compare_digest(credentials.username, os.getenv("ADMIN_USERNAME", "admin"))
    password_ok = secrets.compare_digest(credentials.password, os.getenv("ADMIN_PASSWORD", "admin"))
    if not (username_ok and password_ok):
        raise HTTPException(status_code=401, headers={"WWW-Authenticate": "Basic"})
    return credentials.username


# ─── Dashboard page ──────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
@app.get("/admin", response_class=HTMLResponse)
async def dashboard(request: Request, credentials: HTTPBasicCredentials = Depends(security)):
    username_ok = secrets.compare_digest(credentials.username, os.getenv("ADMIN_USERNAME", "admin"))
    password_ok = secrets.compare_digest(credentials.password, os.getenv("ADMIN_PASSWORD", "admin"))
    if not (username_ok and password_ok):
        raise HTTPException(status_code=401, headers={"WWW-Authenticate": "Basic"})
    import base64
    b64 = base64.b64encode(f"{credentials.username}:{credentials.password}".encode()).decode()
    return templates.TemplateResponse("dashboard.html", {"request": request, "admin_b64": b64})


# ─── Pydantic schemas ────────────────────────────────────────────────────────

class ServerCreate(BaseModel):
    label: str
    flag: str = "🌍"
    node_url: str
    node_path: str = "/"
    node_token: str
    node_cert: Optional[str] = None
    inbound_id: int = 1
    sort_order: int = 0

class ServerUpdate(BaseModel):
    label: Optional[str] = None
    flag: Optional[str] = None
    node_url: Optional[str] = None
    node_path: Optional[str] = None
    node_token: Optional[str] = None
    node_cert: Optional[str] = None
    inbound_id: Optional[int] = None
    is_active: Optional[bool] = None
    sort_order: Optional[int] = None


class InboundCreate(BaseModel):
    name: str
    preset: str = "vless-reality"
    port: int = 443
    listen: str = "0.0.0.0"
    server_name: str = "www.microsoft.com"
    destination: Optional[str] = None
    path: str = "/vpn"


class InboundUpdate(BaseModel):
    port: Optional[int] = None
    listen: Optional[str] = None
    server_name: Optional[str] = None
    destination: Optional[str] = None
    path: Optional[str] = None
    enabled: Optional[bool] = None

class PlanCreate(BaseModel):
    server_ids: list[int] = []  # пустой = все серверы
    name: str
    description: Optional[str] = None
    price_rub: float
    duration_days: int
    traffic_gb: Optional[float] = None
    sort_order: int = 0

class PlanUpdate(BaseModel):
    server_ids: list[int] | None = None  # None = не менять
    name: Optional[str] = None
    price_rub: Optional[float] = None
    duration_days: Optional[int] = None
    traffic_gb: Optional[float] = None
    is_active: Optional[bool] = None
    sort_order: Optional[int] = None

class SettingsUpdate(BaseModel):
    manual_payment_enabled: Optional[bool] = None
    manual_payment_text: Optional[str] = None
    support_username: Optional[str] = None
    welcome_text: Optional[str] = None
    sub_issued_text: Optional[str] = None
    info_text: Optional[str] = None
    privacy_policy_url: Optional[str] = None
    terms_of_service_url: Optional[str] = None
    support_chat_id: Optional[str] = None
    support_operator_ids: Optional[str] = None
    card_link_enabled: Optional[bool] = None
    card_link_url: Optional[str] = None
    card_link_text: Optional[str] = None
    heleket_enabled: Optional[bool] = None
    cryptopay_enabled: Optional[bool] = None
    sbp_enabled: Optional[bool] = None
    sbp_merchant_id: Optional[str] = None
    sbp_secret_key: Optional[str] = None
    referral_enabled: Optional[bool] = None
    referral_percent: Optional[float] = None
    referral_days_reward: Optional[int] = None
    trial_enabled: Optional[bool] = None
    trial_days: Optional[int] = None
    trial_traffic_gb: Optional[float] = None

class ActivateSubRequest(BaseModel):
    user_id: int
    plan_id: int


# ─── Servers API ─────────────────────────────────────────────────────────────

async def list_servers(_=Depends(verify_admin)):
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Server).order_by(Server.sort_order))
        servers = result.scalars().all()
        return [
            {
                "id": s.id, "label": s.label, "flag": s.flag,
                "node_url": s.node_url, "node_path": s.node_path,
                "inbound_id": s.inbound_id,
                "is_active": s.is_active, "sort_order": s.sort_order,
                "created_at": s.created_at.isoformat() if s.created_at else None,
            }
            for s in servers
        ]


@app.post("/api/servers", status_code=201)
async def create_server(data: ServerCreate, _=Depends(verify_admin)):
    async with AsyncSessionLocal() as session:
        server = Server(**data.dict())
        session.add(server)
        await session.flush()
        server_id = server.id
        server_obj = server

        # Сразу синхронизируем всех активных пользователей на новый сервер
        synced = await sync_user_to_new_server(server_obj, session)
        await session.commit()

    return {"id": server_id, "synced_users": synced, "message": f"Сервер добавлен, {synced} пользователей синхронизировано"}


@app.patch("/api/servers/{server_id}")
async def update_server(server_id: int, data: ServerUpdate, _=Depends(verify_admin)):
    async with AsyncSessionLocal() as session:
        server = await session.get(Server, server_id)
        if not server:
            raise HTTPException(404)
        for k, v in data.dict(exclude_none=True).items():
            setattr(server, k, v)
        await session.commit()
    return {"ok": True}


@app.delete("/api/servers/{server_id}")
async def delete_server(server_id: int, _=Depends(verify_admin)):
    async with AsyncSessionLocal() as session:
        server = await session.get(Server, server_id)
        if not server:
            raise HTTPException(404)
        await session.delete(server)
        await session.commit()
    return {"ok": True}


@app.post("/api/servers/{server_id}/ping")
async def ping_server(server_id: int, _=Depends(verify_admin)):
    async with AsyncSessionLocal() as session:
        server = await session.get(Server, server_id)
        if not server:
            raise HTTPException(404)
    from bot.services.xray_client import XrayClient
    client = XrayClient(server.node_url, node_path=server.node_path, node_token=server.node_token, node_cert=server.node_cert)
    ok = await client.ping()
    return {"ok": ok, "status": "online" if ok else "offline"}


async def _node_client(server_id: int) -> tuple[Server, "XrayClient"]:
    from bot.services.xray_client import XrayClient
    async with AsyncSessionLocal() as session:
        server = await session.get(Server, server_id)
        if not server:
            raise HTTPException(404, "Сервер не найден")
        if not server.node_url or not server.node_token:
            raise HTTPException(409, "Xray node ещё не настроен")
        return server, XrayClient(
            server.node_url,
            node_path=server.node_path,
            node_token=server.node_token,
            node_cert=server.node_cert,
        )


@app.get("/api/servers/{server_id}/inbounds")
async def get_node_inbounds(server_id: int, _=Depends(verify_admin)):
    _server, client = await _node_client(server_id)
    try:
        return await client.get_inbounds()
    except Exception as exc:
        raise HTTPException(502, f"Node API: {exc}") from exc


@app.post("/api/servers/{server_id}/inbounds", status_code=201)
async def create_node_inbound(server_id: int, data: InboundCreate, _=Depends(verify_admin)):
    server, client = await _node_client(server_id)
    try:
        inbound = await client.create_inbound(data.model_dump())
    except Exception as exc:
        raise HTTPException(502, f"Node API: {exc}") from exc
    if not server.inbound_id:
        async with AsyncSessionLocal() as session:
            current = await session.get(Server, server_id)
            current.inbound_id = inbound["id"]
            await session.commit()
    return inbound


@app.patch("/api/servers/{server_id}/inbounds/{inbound_ref}")
async def update_node_inbound(server_id: int, inbound_ref: str, data: InboundUpdate, _=Depends(verify_admin)):
    _server, client = await _node_client(server_id)
    try:
        return await client.update_inbound(inbound_ref, data.model_dump(exclude_none=True))
    except Exception as exc:
        raise HTTPException(502, f"Node API: {exc}") from exc


@app.delete("/api/servers/{server_id}/inbounds/{inbound_ref}")
async def delete_node_inbound(server_id: int, inbound_ref: str, _=Depends(verify_admin)):
    _server, client = await _node_client(server_id)
    try:
        return {"ok": await client.delete_inbound_config(inbound_ref)}
    except Exception as exc:
        raise HTTPException(502, f"Node API: {exc}") from exc


# ─── Plans API ────────────────────────────────────────────────────────────────

@app.get("/api/plans")
async def list_plans(_=Depends(verify_admin)):
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Plan).order_by(Plan.sort_order))
        plans = result.scalars().all()
        # Загружаем server_ids для каждого тарифа
        plan_server_map = {}
        for plan in plans:
            ps_result = await session.execute(
                select(PlanServer).where(PlanServer.plan_id == plan.id)
            )
            plan_server_map[plan.id] = [ps.server_id for ps in ps_result.scalars().all()]
        return [
            {
                "id": p.id, "name": p.name, "description": p.description,
                "price_rub": p.price_rub, "duration_days": p.duration_days,
                "traffic_gb": p.traffic_gb, "is_active": p.is_active,
                "sort_order": p.sort_order, "server_ids": plan_server_map.get(p.id, []),
            }
            for p in plans
        ]


@app.post("/api/plans", status_code=201)
async def create_plan(data: PlanCreate, _=Depends(verify_admin)):
    async with AsyncSessionLocal() as session:
        d = data.dict()
        server_ids = d.pop("server_ids", [])
        plan = Plan(**d)
        session.add(plan)
        await session.flush()
        for sid in server_ids:
            session.add(PlanServer(plan_id=plan.id, server_id=sid))
        plan_id = plan.id
        await session.commit()
    return {"id": plan_id}


@app.patch("/api/plans/{plan_id}")
async def update_plan(plan_id: int, data: PlanUpdate, _=Depends(verify_admin)):
    async with AsyncSessionLocal() as session:
        plan = await session.get(Plan, plan_id)
        if not plan:
            raise HTTPException(404)
        d = data.dict(exclude_none=True)
        server_ids = d.pop("server_ids", None)
        for k, v in d.items():
            setattr(plan, k, v)
        if server_ids is not None:
            from sqlalchemy import delete
            await session.execute(delete(PlanServer).where(PlanServer.plan_id == plan_id))
            for sid in server_ids:
                session.add(PlanServer(plan_id=plan_id, server_id=sid))
        await session.commit()
    return {"ok": True}


@app.delete("/api/plans/{plan_id}")
async def delete_plan(plan_id: int, _=Depends(verify_admin)):
    async with AsyncSessionLocal() as session:
        plan = await session.get(Plan, plan_id)
        if not plan:
            raise HTTPException(404)
        plan.is_active = False
        await session.commit()
    return {"ok": True}


# ─── Users API ────────────────────────────────────────────────────────────────

async def list_users(page: int = 1, per_page: int = 50, search: str = "", _=Depends(verify_admin)):
    async with AsyncSessionLocal() as session:
        q = select(User)
        if search:
            q = q.where(
                User.username.ilike(f"%{search}%") |
                User.full_name.ilike(f"%{search}%") |
                (User.id == int(search) if search.isdigit() else False)
            )
        total = (await session.execute(select(func.count()).select_from(q.subquery()))).scalar()
        result = await session.execute(q.offset((page - 1) * per_page).limit(per_page))
        users = result.scalars().all()

        user_list = []
        for u in users:
            sub_r = await session.execute(
                select(Subscription)
                .where(Subscription.user_id == u.id)
                .where(Subscription.status == "active")
            )
            sub = sub_r.scalar_one_or_none()
            user_list.append({
                "id": u.id, "username": u.username, "full_name": u.full_name,
                "is_banned": u.is_banned, "trial_used": u.trial_used,
                "created_at": u.created_at.isoformat() if u.created_at else None,
                "sub_active": bool(sub),
                "sub_expires": sub.expires_at.isoformat() if sub and sub.expires_at else None,
                "sub_token": str(u.sub_token),
            })
        return {"total": total, "users": user_list}


@app.post("/api/users/{user_id}/activate")
async def admin_activate_sub(user_id: int, data: ActivateSubRequest, _=Depends(verify_admin)):
    ok = await activate_subscription(user_id, data.plan_id)
    if not ok:
        raise HTTPException(400, "Activation failed")
    return {"ok": True}


@app.post("/api/users/{user_id}/ban")
async def ban_user(user_id: int, _=Depends(verify_admin)):
    async with AsyncSessionLocal() as session:
        user = await session.get(User, user_id)
        if not user:
            raise HTTPException(404)
        user.is_banned = True
        await session.commit()
    from bot.services.vpn_service import revoke_user_access
    removed, total = await revoke_user_access(user_id)
    return {"ok": True, "xray_removed": removed, "xray_nodes": total}


@app.post("/api/users/{user_id}/unban")
async def unban_user(user_id: int, _=Depends(verify_admin)):
    async with AsyncSessionLocal() as session:
        user = await session.get(User, user_id)
        if not user:
            raise HTTPException(404)
        user.is_banned = False
        await session.commit()
    from bot.services.vpn_service import restore_user_access
    restored, total = await restore_user_access(user_id)
    return {"ok": True, "xray_restored": restored, "xray_nodes": total}


# ─── Settings API ─────────────────────────────────────────────────────────────

@app.post("/api/settings")
async def update_settings_post(data: dict, _=Depends(verify_admin)):
    """POST alias for settings update (used by setup wizard)."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Settings).where(Settings.id == 1))
        settings = result.scalar_one_or_none()
        if not settings:
            settings = Settings(id=1)
            session.add(settings)
        for key, val in data.items():
            if hasattr(settings, key) and val is not None:
                setattr(settings, key, val)
        await session.commit()
    return {"ok": True}


@app.get("/api/settings")
async def get_settings(_=Depends(verify_admin)):
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Settings).where(Settings.id == 1))
        s = result.scalar_one()
        d = {k: v for k, v in vars(s).items() if not k.startswith("_")}
        d["google_client_id"] = os.getenv("GOOGLE_CLIENT_ID", "")
        return d


@app.patch("/api/settings")
async def update_settings(data: SettingsUpdate, _=Depends(verify_admin)):
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Settings).where(Settings.id == 1))
        settings = result.scalar_one()
        for k, v in data.dict(exclude_none=True).items():
            setattr(settings, k, v)
        await session.commit()
    return {"ok": True}


# ─── Payments API ─────────────────────────────────────────────────────────────

@app.get("/api/payments")
async def list_payments(page: int = 1, per_page: int = 50, _=Depends(verify_admin)):
    async with AsyncSessionLocal() as session:
        total = (await session.execute(select(func.count(Payment.id)))).scalar()
        result = await session.execute(
            select(Payment).order_by(Payment.created_at.desc())
            .offset((page - 1) * per_page).limit(per_page)
        )
        payments = result.scalars().all()
        return {
            "total": total,
            "payments": [
                {
                    "id": p.id, "user_id": p.user_id, "plan_id": p.plan_id,
                    "method": p.method, "amount": p.amount, "status": p.status,
                    "created_at": p.created_at.isoformat() if p.created_at else None,
                    "paid_at": p.paid_at.isoformat() if p.paid_at else None,
                }
                for p in payments
            ]
        }


# ─── Stats API ────────────────────────────────────────────────────────────────

@app.get("/api/stats/revenue")
async def revenue_stats(_=Depends(verify_admin)):
    async with AsyncSessionLocal() as session:
        # По дням за последние 30 дней
        result = await session.execute(
            select(
                func.date(Payment.paid_at).label("date"),
                func.sum(Payment.amount).label("amount"),
                func.count(Payment.id).label("count"),
            )
            .where(Payment.status == "paid")
            .where(Payment.paid_at > datetime.now() - timedelta(days=30))
            .group_by(func.date(Payment.paid_at))
            .order_by(func.date(Payment.paid_at))
        )
        rows = result.all()
        return [{"date": str(r.date), "amount": float(r.amount or 0), "count": r.count} for r in rows]


async def dashboard_stats(_=Depends(verify_admin)):
    """Живая статистика для дашборда — вызывается из JS, не из Jinja2."""
    async with AsyncSessionLocal() as session:
        total_users = (await session.execute(select(func.count(User.id)))).scalar() or 0
        active_subs = (await session.execute(
            select(func.count(Subscription.id))
            .where(Subscription.status == "active")
            .where(Subscription.expires_at > datetime.now())
        )).scalar() or 0
        total_servers = (await session.execute(
            select(func.count(Server.id)).where(Server.is_active == True)
        )).scalar() or 0
        revenue_month = (await session.execute(
            select(func.sum(Payment.amount))
            .where(Payment.status == "paid")
            .where(Payment.paid_at > datetime.now() - timedelta(days=30))
        )).scalar() or 0
        new_users_today = (await session.execute(
            select(func.count(User.id))
            .where(User.created_at > datetime.now() - timedelta(days=1))
        )).scalar() or 0
        return {
            "total_users": total_users,
            "active_subs": active_subs,
            "total_servers": total_servers,
            "revenue_month": float(revenue_month),
            "new_users_today": new_users_today,
        }


# ─── SNI Rotation API ────────────────────────────────────────────────────────

@app.patch("/api/servers/{server_id}/sni-rotation")
async def toggle_sni_rotation(server_id: int, enabled: bool, _=Depends(verify_admin)):
    """Включить/выключить SNI ротацию для сервера."""
    async with AsyncSessionLocal() as session:
        server = await session.get(Server, server_id)
        if not server:
            raise HTTPException(404)
        server.sni_rotation_enabled = enabled
        await session.commit()
    return {"ok": True, "sni_rotation_enabled": enabled}


@app.post("/api/servers/{server_id}/rotate-sni-now")
async def rotate_sni_now(server_id: int, _=Depends(verify_admin)):
    """Принудительно сменить SNI прямо сейчас (не дожидаясь планировщика)."""
    async with AsyncSessionLocal() as session:
        server = await session.get(Server, server_id)
        if not server:
            raise HTTPException(404)

    from utils.sni_rotation import rotate_sni_on_server
    result = await rotate_sni_on_server(
        node_url=server.node_url,
        node_token=server.node_token,
        inbound_id=server.inbound_id,
        node_path=server.node_path or "/",
        node_cert=server.node_cert,
    )

    if result["ok"]:
        from db.models import SniRotationLog
        async with AsyncSessionLocal() as session:
            server = await session.get(Server, server_id)
            server.current_sni = result["sni"]
            server.sni_last_rotated = datetime.now()
            session.add(SniRotationLog(
                server_id=server_id,
                new_sni=result["sni"],
                fingerprint=result.get("fingerprint", ""),
                success=True,
            ))
            await session.commit()

    return result


@app.get("/api/servers/{server_id}/sni-log")
async def get_sni_log(server_id: int, limit: int = 20, _=Depends(verify_admin)):
    """История ротаций SNI для сервера."""
    from db.models import SniRotationLog
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(SniRotationLog)
            .where(SniRotationLog.server_id == server_id)
            .order_by(SniRotationLog.rotated_at.desc())
            .limit(limit)
        )
        logs = result.scalars().all()
        return [
            {
                "id": l.id,
                "sni": l.new_sni,
                "fingerprint": l.fingerprint,
                "success": l.success,
                "error": l.error,
                "rotated_at": l.rotated_at.isoformat() if l.rotated_at else None,
            }
            for l in logs
        ]


@app.get("/api/sni/whitelist")
async def get_whitelist_preview(_=Depends(verify_admin)):
    """Показать первые 20 доменов из whitelist и общее кол-во."""
    import os
    from utils.sni_rotation import load_whitelist
    domains = load_whitelist()
    return {
        "total": len(domains),
        "preview": domains[:20],
        "path": os.getenv("SNI_WHITELIST_PATH", "whitelist.txt"),
    }


@app.get("/api/sni/log-all")
async def get_all_sni_logs(limit: int = 50, _=Depends(verify_admin)):
    """История всех SNI ротаций по всем серверам."""
    from db.models import SniRotationLog
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(SniRotationLog, Server.label)
            .join(Server, Server.id == SniRotationLog.server_id)
            .order_by(SniRotationLog.rotated_at.desc())
            .limit(limit)
        )
        rows = result.all()
        return [
            {
                "id": log.id,
                "server_id": log.server_id,
                "server_label": label,
                "sni": log.new_sni,
                "fingerprint": log.fingerprint,
                "success": log.success,
                "error": log.error,
                "rotated_at": log.rotated_at.isoformat() if log.rotated_at else None,
            }
            for log, label in rows
        ]


# ─── Backup endpoints ────────────────────────────────────────────────────────

@app.post("/api/backup/create")
async def api_create_backup(_=Depends(verify_admin)):
    """Создать бэкап прямо сейчас."""
    from utils.backup import create_backup
    path = await create_backup()
    if not path:
        raise HTTPException(500, "Ошибка создания бэкапа")
    return {
        "ok": True,
        "filename": path.name,
        "size_kb": path.stat().st_size // 1024
    }


@app.get("/api/backup/download/{filename}")
async def api_download_backup(filename: str, _=Depends(verify_admin)):
    """Скачать бэкап файл."""
    from fastapi.responses import FileResponse
    from pathlib import Path
    backup_path = Path(os.getenv("BACKUP_DIR", "/app/backups")) / filename
    if not backup_path.exists() or not backup_path.name.startswith("vpnbot_backup_"):
        raise HTTPException(404, "Файл не найден")
    return FileResponse(backup_path, filename=filename, media_type="application/gzip")


@app.get("/api/backup/list")
async def api_list_backups(_=Depends(verify_admin)):
    """Список доступных бэкапов."""
    from pathlib import Path
    backup_dir = Path(os.getenv("BACKUP_DIR", "/app/backups"))
    backup_dir.mkdir(exist_ok=True)
    files = sorted(backup_dir.glob("vpnbot_backup_*.sql.gz"), key=lambda p: p.stat().st_mtime, reverse=True)
    return [
        {
            "filename": f.name,
            "size_kb": f.stat().st_size // 1024,
            "created": datetime.fromtimestamp(f.stat().st_mtime).isoformat()
        }
        for f in files
    ]


@app.post("/api/backup/restore")
async def api_restore_backup(request: Request, _=Depends(verify_admin)):
    """Восстановить БД из загруженного файла."""
    import shutil, tempfile
    from utils.backup import restore_backup
    form = await request.form()
    file = form.get("file")
    if not file:
        raise HTTPException(400, "Файл не передан")
    with tempfile.NamedTemporaryFile(suffix=".sql.gz", delete=False) as tmp:
        content = await file.read()
        tmp.write(content)
        tmp_path = tmp.name
    ok, msg = await restore_backup(tmp_path)
    import os as _os
    _os.unlink(tmp_path)
    if not ok:
        raise HTTPException(500, f"Ошибка восстановления: {msg}")
    return {"ok": True, "message": "База данных восстановлена"}


# ─── Balance endpoints ────────────────────────────────────────────────────────

class BalanceUpdate(BaseModel):
    user_id: int
    amount: float
    comment: Optional[str] = "Изменение администратором"


@app.post("/api/users/balance")
async def api_update_balance(data: BalanceUpdate, _=Depends(verify_admin)):
    """Пополнить или списать баланс пользователя."""
    from db.models import BalanceLog
    async with AsyncSessionLocal() as session:
        user = await session.get(User, data.user_id)
        if not user:
            raise HTTPException(404, "Пользователь не найден")
        user.balance = (user.balance or 0) + data.amount
        log = BalanceLog(user_id=data.user_id, amount=data.amount, comment=data.comment)
        session.add(log)
        await session.commit()
        new_balance = user.balance

    # Реферальное начисление — только при пополнении (amount > 0)
    if data.amount > 0:
        try:
            from utils.referral import accrue_referral_reward
            from bot.services.vpn_service import _bot_instance
            await accrue_referral_reward(
                payer_user_id=data.user_id,
                amount_rub=data.amount,
                source_label=f"пополнение баланса (админ)",
                bot=_bot_instance,
            )
        except Exception as _ref_err:
            import logging
            logging.getLogger(__name__).warning(f"Referral accrual error (admin topup): {_ref_err}")

    return {"ok": True, "new_balance": new_balance}


@app.get("/api/users/{user_id}/balance-log")
async def api_balance_log(user_id: int, _=Depends(verify_admin)):
    """История операций с балансом пользователя."""
    from db.models import BalanceLog
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(BalanceLog)
            .where(BalanceLog.user_id == user_id)
            .order_by(BalanceLog.created_at.desc())
            .limit(50)
        )
        logs = result.scalars().all()
    return [
        {
            "id": l.id,
            "amount": l.amount,
            "comment": l.comment,
            "created_at": l.created_at.isoformat()
        }
        for l in logs
    ]


# ─── Stats dashboard endpoint ─────────────────────────────────────────────────

@app.get("/api/stats/dashboard")
async def stats_dashboard(_=Depends(verify_admin)):
    async with AsyncSessionLocal() as session:
        from db.models import Subscription, Payment
        total_users = (await session.execute(select(func.count(User.id)))).scalar()
        active_subs = (await session.execute(
            select(func.count(Subscription.id))
            .where(Subscription.status == "active")
            .where(Subscription.expires_at > datetime.now())
        )).scalar()
        total_servers = (await session.execute(
            select(func.count(Server.id)).where(Server.is_active == True)
        )).scalar()
        revenue_month = (await session.execute(
            select(func.sum(Payment.amount))
            .where(Payment.status == "paid")
            .where(Payment.paid_at > datetime.now() - timedelta(days=30))
        )).scalar() or 0
        new_today = (await session.execute(
            select(func.count(User.id))
            .where(User.created_at > datetime.now() - timedelta(days=1))
        )).scalar()
    return {
        "total_users": total_users,
        "active_subs": active_subs,
        "total_servers": total_servers,
        "revenue_month": round(revenue_month, 2),
        "new_users_today": new_today,
    }


# ─── Broadcast send endpoint ──────────────────────────────────────────────────

class BroadcastPayload(BaseModel):
    type: str           # text / photo / video / document / copy
    target: str = "all" # all / active
    text: Optional[str] = None
    file_id: Optional[str] = None
    caption: Optional[str] = None
    button_text: Optional[str] = None
    button_url: Optional[str] = None
    # Для copy_message (сохраняет премиум эмодзи)
    from_chat_id: Optional[int] = None
    message_id: Optional[int] = None


@app.post("/api/broadcast/send")
async def api_broadcast_send(payload: BroadcastPayload, _=Depends(verify_admin)):
    """Запустить рассылку через бота."""
    import asyncio
    from aiogram import Bot
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    from db.models import Broadcast

    bot = Bot(token=os.getenv("BOT_TOKEN"))

    async with AsyncSessionLocal() as session:
        if payload.target == "active":
            from db.models import Subscription
            result = await session.execute(
                select(User.id)
                .join(Subscription, Subscription.user_id == User.id)
                .where(Subscription.status == "active")
                .where(Subscription.expires_at > datetime.now())
                .where(User.is_banned == False)
                .distinct()
            )
        else:
            result = await session.execute(
                select(User.id).where(User.is_banned == False)
            )
        user_ids = list(result.scalars().all())

    reply_markup = None
    if payload.button_text and payload.button_url:
        reply_markup = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=payload.button_text, url=payload.button_url)]
        ])

    sent = 0
    failed = 0

    for user_id in user_ids:
        try:
            # copy_message — сохраняет премиум эмодзи и форматирование
            if payload.from_chat_id and payload.message_id:
                await bot.copy_message(
                    chat_id=user_id,
                    from_chat_id=payload.from_chat_id,
                    message_id=payload.message_id,
                    reply_markup=reply_markup,
                )
            elif payload.type == "text":
                await bot.send_message(user_id, payload.text, parse_mode="HTML", reply_markup=reply_markup)
            elif payload.type == "photo":
                await bot.send_photo(user_id, payload.file_id, caption=payload.caption or "", parse_mode="HTML", reply_markup=reply_markup)
            elif payload.type == "video":
                await bot.send_video(user_id, payload.file_id, caption=payload.caption or "", parse_mode="HTML", reply_markup=reply_markup)
            elif payload.type == "document":
                await bot.send_document(user_id, payload.file_id, caption=payload.caption or "", parse_mode="HTML", reply_markup=reply_markup)
            sent += 1
        except Exception:
            failed += 1

        if (sent + failed) % 25 == 0:
            await asyncio.sleep(1)

    await bot.session.close()

    async with AsyncSessionLocal() as session:
        record = Broadcast(
            admin_id=0, target=payload.target, msg_type=payload.type,
            text=payload.text, file_id=payload.file_id, caption=payload.caption,
            button_text=payload.button_text, button_url=payload.button_url,
            sent_count=sent, failed_count=failed,
        )
        session.add(record)
        await session.commit()

    return {"ok": True, "sent": sent, "failed": failed}


@app.get("/api/broadcast/history")
async def api_broadcast_history(_=Depends(verify_admin)):
    from db.models import Broadcast
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Broadcast).order_by(Broadcast.created_at.desc()).limit(20)
        )
        rows = result.scalars().all()
    return [
        {
            "id": r.id,
            "target": r.target,
            "type": r.msg_type,
            "sent": r.sent_count,
            "failed": r.failed_count,
            "created_at": r.created_at.isoformat(),
        }
        for r in rows
    ]


# ─── Users list — include balance ─────────────────────────────────────────────

@app.get("/api/users")
async def get_users(page: int = 1, search: Optional[str] = None, _=Depends(verify_admin)):
    async with AsyncSessionLocal() as session:
        from db.models import Subscription
        query = select(User)
        if search:
            query = query.where(
                (User.username.ilike(f"%{search}%")) |
                (User.full_name.ilike(f"%{search}%")) |
                (func.cast(User.id, String).ilike(f"%{search}%"))
            )
        query = query.order_by(User.created_at.desc()).offset((page - 1) * 50).limit(50)
        result = await session.execute(query)
        users = result.scalars().all()

        users_data = []
        for u in users:
            sub_result = await session.execute(
                select(Subscription)
                .where(Subscription.user_id == u.id)
                .where(Subscription.status == "active")
                .order_by(Subscription.expires_at.desc())
            )
            active_sub = sub_result.scalar_one_or_none()
            users_data.append({
                "id": u.id,
                "username": u.username,
                "full_name": u.full_name,
                "balance": u.balance or 0,
                "partner_balance": u.partner_balance or 0,
                "partner_earned": u.partner_earned or 0,
                "is_banned": u.is_banned,
                "sub_active": bool(active_sub and active_sub.expires_at and active_sub.expires_at > datetime.now()),
                "sub_expires": active_sub.expires_at.isoformat() if active_sub and active_sub.expires_at else None,
                "created_at": u.created_at.isoformat() if u.created_at else None,
            })
        return users_data


# ─── Advanced Statistics ──────────────────────────────────────────────────────

@app.get("/api/stats/revenue-chart")
async def stats_revenue_chart(days: int = 30, _=Depends(verify_admin)):
    """Выручка по дням за последние N дней."""
    from db.models import Payment
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(
                func.date(Payment.paid_at).label("day"),
                func.sum(Payment.amount).label("revenue"),
                func.count(Payment.id).label("count")
            )
            .where(Payment.status == "paid")
            .where(Payment.paid_at > datetime.now() - timedelta(days=days))
            .group_by(func.date(Payment.paid_at))
            .order_by(func.date(Payment.paid_at))
        )
        rows = result.all()
    return [{"day": str(r.day), "revenue": float(r.revenue or 0), "count": r.count} for r in rows]


@app.get("/api/stats/new-users-chart")
async def stats_new_users_chart(days: int = 30, _=Depends(verify_admin)):
    """Новые пользователи по дням."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(
                func.date(User.created_at).label("day"),
                func.count(User.id).label("count")
            )
            .where(User.created_at > datetime.now() - timedelta(days=days))
            .group_by(func.date(User.created_at))
            .order_by(func.date(User.created_at))
        )
        rows = result.all()
    return [{"day": str(r.day), "count": r.count} for r in rows]


@app.get("/api/stats/conversion")
async def stats_conversion(_=Depends(verify_admin)):
    """Конверсия: зарегались -> купили."""
    from db.models import Payment, Subscription
    async with AsyncSessionLocal() as session:
        total = (await session.execute(select(func.count(User.id)))).scalar() or 1
        bought = (await session.execute(
            select(func.count(func.distinct(Payment.user_id))).where(Payment.status == "paid")
        )).scalar() or 0
        active = (await session.execute(
            select(func.count(func.distinct(Subscription.user_id)))
            .where(Subscription.status == "active")
            .where(Subscription.expires_at > datetime.now())
        )).scalar() or 0
    return {
        "total_users": total,
        "paid_users": bought,
        "active_users": active,
        "conversion_percent": round(bought / total * 100, 1),
        "retention_percent": round(active / max(bought, 1) * 100, 1),
    }


@app.get("/api/stats/top-traffic")
async def stats_top_traffic(_=Depends(verify_admin)):
    """Топ-10 пользователей по трафику."""
    from db.models import Subscription
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(User.id, User.username, User.full_name, Subscription.traffic_used_gb)
            .join(Subscription, Subscription.user_id == User.id)
            .where(Subscription.status == "active")
            .order_by(Subscription.traffic_used_gb.desc())
            .limit(10)
        )
        rows = result.all()
    return [
        {"id": r[0], "username": r[1], "full_name": r[2], "traffic_gb": round(r[3] or 0, 2)}
        for r in rows
    ]


# ─── Promo codes CRUD ─────────────────────────────────────────────────────────

class PromoCreate(BaseModel):
    code: str
    discount_type: str = "percent"   # percent / fixed
    discount_value: float
    max_uses: Optional[int] = None
    valid_until: Optional[str] = None   # ISO datetime string


@app.get("/api/promos")
async def get_promos(_=Depends(verify_admin)):
    from db.models import PromoCode
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(PromoCode).order_by(PromoCode.created_at.desc()))
        promos = result.scalars().all()
    return [
        {
            "id": p.id, "code": p.code,
            "discount_type": p.discount_type,
            "discount_value": p.discount_value,
            "max_uses": p.max_uses,
            "uses_count": p.uses_count,
            "valid_until": p.valid_until.isoformat() if p.valid_until else None,
            "is_active": p.is_active,
        }
        for p in promos
    ]


@app.post("/api/promos")
async def create_promo(data: PromoCreate, _=Depends(verify_admin)):
    from db.models import PromoCode
    from datetime import datetime as dt
    async with AsyncSessionLocal() as session:
        promo = PromoCode(
            code=data.code.upper().strip(),
            discount_type=data.discount_type,
            discount_value=data.discount_value,
            max_uses=data.max_uses,
            valid_until=dt.fromisoformat(data.valid_until) if data.valid_until else None,
        )
        session.add(promo)
        await session.commit()
        return {"ok": True, "id": promo.id}


@app.delete("/api/promos/{promo_id}")
async def delete_promo(promo_id: int, _=Depends(verify_admin)):
    from db.models import PromoCode
    async with AsyncSessionLocal() as session:
        promo = await session.get(PromoCode, promo_id)
        if not promo:
            raise HTTPException(404)
        promo.is_active = False
        await session.commit()
    return {"ok": True}


# ─── Server online status ─────────────────────────────────────────────────────

@app.get("/api/servers")
async def get_servers(_=Depends(verify_admin)):
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Server).order_by(Server.sort_order))
        servers = result.scalars().all()
    return [
        {
            "id": s.id, "label": s.label, "flag": s.flag,
            "node_url": s.node_url, "node_path": s.node_path,
            "inbound_id": s.inbound_id,
            "is_active": s.is_active, "sort_order": s.sort_order,
            "sni_rotation_enabled": s.sni_rotation_enabled,
            "current_sni": s.current_sni,
            "sni_last_rotated": s.sni_last_rotated.isoformat() if s.sni_last_rotated else None,
            "is_online": getattr(s, "is_online", True),
            "last_checked": s.last_checked.isoformat() if getattr(s, "last_checked", None) else None,
            "install_status": s.install_status,
            "install_log": s.install_log,
            "ssh_host": s.ssh_host,
        }
        for s in servers
    ]


# ─── Server provisioning endpoints ───────────────────────────────────────────

class ServerProvisionCreate(BaseModel):
    label: str
    flag: str = "🌍"
    ssh_host: str
    ssh_port: int = 22
    ssh_user: str = "root"
    ssh_password: Optional[str] = None
    ssh_key: Optional[str] = None
    sort_order: int = 0
    node_port: int = 8090


class ServerProvisionReady(BaseModel):
    label: str
    flag: str = "🌍"
    ssh_host: str
    node_token: str
    node_cert: Optional[str] = None
    node_url_override: Optional[str] = None   # полный URL: http://IP:порт
    sort_order: int = 0
    skip_ssh: bool = True


@app.post("/api/servers/provision-ready")
async def provision_server_ready(data: ServerProvisionReady, _=Depends(verify_admin)):
    """Attach a server that already runs the KawaVPN Xray node-agent."""
    import asyncio
    from db.models import Server as ServerModel

    node_url = data.node_url_override or f"http://{data.ssh_host}:8090"

    async with AsyncSessionLocal() as session:
        server = ServerModel(
            label=data.label,
            flag=data.flag,
            ssh_host=data.ssh_host,
            ssh_port=22,
            ssh_user="root",
            node_url=node_url,
            node_token=data.node_token,
            node_cert=data.node_cert,
            sort_order=data.sort_order,
            install_status="installing",
            install_log="Подключение к Xray node-agent...",
        )
        session.add(server)
        await session.commit()
        server_id = server.id

    async def run():
        from utils.server_provisioner import provision_ready_server
        await provision_ready_server(server_id, node_url, data.node_token, data.node_cert)

    asyncio.create_task(run())
    return {"ok": True, "server_id": server_id, "status": "connecting"}


@app.post("/api/servers/provision")
async def provision_server_endpoint(data: ServerProvisionCreate, _=Depends(verify_admin)):
    """Добавить сервер и запустить авто-установку в фоне."""
    import asyncio
    from db.models import Server as ServerModel

    async with AsyncSessionLocal() as session:
        server = ServerModel(
            label=data.label,
            flag=data.flag,
            ssh_host=data.ssh_host,
            ssh_port=data.ssh_port,
            ssh_user=data.ssh_user,
            ssh_password=data.ssh_password,
            ssh_key=data.ssh_key,
            sort_order=data.sort_order,
            install_status="pending",
            node_url=f"https://{data.ssh_host}:{data.node_port}",
        )
        session.add(server)
        await session.commit()
        server_id = server.id

    # Запускаем установку в фоне
    async def run():
        from utils.server_provisioner import provision_vpn_server
        await provision_vpn_server(server_id)

    asyncio.create_task(run())

    return {"ok": True, "server_id": server_id, "status": "installing"}


@app.get("/api/servers/{server_id}/install-status")
async def get_install_status(server_id: int, _=Depends(verify_admin)):
    from utils.server_provisioner import get_install_status
    return await get_install_status(server_id)


class ServerManualSetup(BaseModel):
    node_url: Optional[str] = None
    node_token: Optional[str] = None
    inbound_id: Optional[int] = 1


@app.post("/api/servers/{server_id}/set-manual")
async def set_server_manual(server_id: int, data: ServerManualSetup, _=Depends(verify_admin)):
    """Отметить сервер как настроенный вручную (без SSH установки)."""
    async with AsyncSessionLocal() as session:
        server = await session.get(Server, server_id)
        if not server:
            raise HTTPException(404)
        if data.node_url:
            server.node_url = data.node_url
        if data.node_token:
            server.node_token = data.node_token
        if data.inbound_id:
            server.inbound_id = data.inbound_id
        server.install_status = "ready"
        server.install_log = "Xray node подключён вручную"
        await session.commit()
    return {"ok": True}


@app.post("/api/servers/{server_id}/reinstall")
async def reinstall_server(server_id: int, _=Depends(verify_admin)):
    """Переустановить сервер."""
    import asyncio
    async with AsyncSessionLocal() as session:
        server = await session.get(Server, server_id)
        if not server:
            raise HTTPException(404)
        server.install_status = "installing"
        server.install_log = "Переустановка запущена..."
        await session.commit()

    async def run():
        from utils.server_provisioner import provision_vpn_server
        await provision_vpn_server(server_id)

    asyncio.create_task(run())
    return {"ok": True, "status": "installing"}


# ─────────────────────────────────────────────────────────────────────────────
# SETUP WIZARD (v5) — первоначальная настройка после установки
# Доступен без авторизации пока BOT_TOKEN не задан.
# После сохранения токена — требует логин/пароль.
# ─────────────────────────────────────────────────────────────────────────────

def _setup_needed() -> bool:
    """True если бот ещё не настроен (нет BOT_TOKEN или он пустой)."""
    return not os.getenv("BOT_TOKEN", "").strip()


@app.get("/setup", response_class=HTMLResponse)
async def setup_page(request: Request, credentials: HTTPBasicCredentials = Depends(security)):
    """Страница первоначальной настройки."""
    username_ok = secrets.compare_digest(credentials.username, os.getenv("ADMIN_USERNAME", "admin"))
    password_ok = secrets.compare_digest(credentials.password, os.getenv("ADMIN_PASSWORD", "admin"))
    if not (username_ok and password_ok):
        raise HTTPException(status_code=401, headers={"WWW-Authenticate": "Basic"})
    import base64
    b64 = base64.b64encode(f"{credentials.username}:{credentials.password}".encode()).decode()
    html = _SETUP_HTML.replace(
        "window.__ADMIN_B64__ = null;",
        f"window.__ADMIN_B64__ = '{b64}';"
    )
    return HTMLResponse(content=html)


@app.post("/api/setup/save")
async def setup_save(request: Request, _=Depends(verify_admin)):
    """
    Сохраняет начальные настройки:
    - Записывает .env файл
    - Инициализирует настройки в БД
    - Перезапускает бота (посылает SIGHUP docker compose сервису)
    """
    data = await request.json()

    bot_token      = data.get("bot_token", "").strip()
    admin_tg_id    = data.get("admin_tg_id", "").strip()
    bot_domain     = data.get("bot_domain", "").strip()
    admin_password = data.get("admin_password", "admin")
    support_chat   = data.get("support_chat_id", "").strip()
    status_channel = data.get("status_channel_id", "").strip()
    pg_password    = data.get("pg_password", "vpnbot_secret").strip()

    if not bot_token:
        raise HTTPException(status_code=400, detail="BOT_TOKEN обязателен")
    if not admin_tg_id:
        raise HTTPException(status_code=400, detail="ADMIN_IDS обязателен")

    # ── Записываем .env ──────────────────────────────────────────────────────
    env_path = "/app/.env"
    try:
        # Читаем существующий .env (если есть)
        existing = {}
        if os.path.exists(env_path):
            for line in open(env_path):
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, _, v = line.partition("=")
                    existing[k.strip()] = v.strip()

        existing.update({
            "BOT_TOKEN": bot_token,
            "ADMIN_IDS": admin_tg_id,
            "BOT_DOMAIN": bot_domain,
            "ADMIN_PASSWORD": admin_password,
            "SUPPORT_CHAT_ID": support_chat,
            "STATUS_CHANNEL_ID": status_channel,
            "POSTGRES_PASSWORD": pg_password,
        })
        # Extended fields from wizard
        heleket_key     = data.get("heleket_key", "").strip()
        cryptopay_token = data.get("cryptopay_token", "").strip()
        google_client_id = data.get("google_client_id", "").strip()
        if heleket_key:
            existing["HELEKET_API_KEY"] = heleket_key
        if cryptopay_token:
            existing["CRYPTOPAY_TOKEN"] = cryptopay_token
        if google_client_id:
            existing["GOOGLE_CLIENT_ID"] = google_client_id
            os.environ["GOOGLE_CLIENT_ID"] = google_client_id

        lines = []
        for k, v in existing.items():
            lines.append(f"{k}={v}")
        with open(env_path, "w") as f:
            f.write("\n".join(lines) + "\n")
    except Exception as e:
        logger.error(f"Setup: failed to write .env: {e}")

    # ── Обновляем Settings в БД ──────────────────────────────────────────────
    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(select(Settings).where(Settings.id == 1))
            settings = result.scalar_one_or_none()
            if not settings:
                settings = Settings(id=1)
                session.add(settings)
            settings.support_chat_id      = support_chat or None
            settings.status_channel_id    = status_channel or None
            settings.status_channel_alerts = True
            # Extended wizard fields
            for field in ["trial_enabled","trial_days","trial_traffic_gb",
                          "referral_enabled","referral_percent","referral_days_reward",
                          "manual_payment_enabled","manual_payment_text",
                          "card_link_enabled","card_link_url","card_link_text","privacy_policy_url","terms_of_service_url",
                          "heleket_enabled","cryptopay_enabled","sbp_enabled","sbp_merchant_id","sbp_secret_key",
                          "support_username","welcome_text","sub_issued_text"]:
                val = data.get(field)
                if val is not None and hasattr(settings, field):
                    setattr(settings, field, val)
            await session.commit()
    except Exception as e:
        logger.error(f"Setup: DB update failed: {e}")

    # ── Перезапуск бота ──────────────────────────────────────────────────────
    import subprocess, signal
    try:
        subprocess.Popen(["sh", "-c", "sleep 2 && kill -HUP 1"], close_fds=True)
    except Exception:
        pass

    return {"ok": True, "redirect": "/"}


# ─────────────────────────────────────────────────────────────────────────────
# BOT CONTROL — управление ботом через supervisorctl
# ─────────────────────────────────────────────────────────────────────────────
import subprocess as _sp
import time as _time

def _supervisorctl(*args) -> tuple[int, str]:
    cmd = ["supervisorctl", "-c", "/app/supervisord.conf"] + list(args)
    result = _sp.run(cmd, capture_output=True, text=True)
    return result.returncode, (result.stdout + result.stderr).strip()


@app.get("/api/bot/status")
async def bot_status(_=Depends(verify_admin)):
    rc, out = _supervisorctl("status", "bot")
    running = "RUNNING" in out
    token_set = bool(os.getenv("BOT_TOKEN", "").strip())
    return {"running": running, "status_line": out, "token_set": token_set}


@app.post("/api/bot/start")
async def bot_start(_=Depends(verify_admin)):
    rc, out = _supervisorctl("start", "bot")
    return {"ok": rc == 0, "output": out}


@app.post("/api/bot/stop")
async def bot_stop(_=Depends(verify_admin)):
    rc, out = _supervisorctl("stop", "bot")
    return {"ok": rc == 0, "output": out}


@app.post("/api/bot/restart")
async def bot_restart(_=Depends(verify_admin)):
    rc, out = _supervisorctl("restart", "bot")
    return {"ok": rc == 0, "output": out}


class BotTokenUpdate(BaseModel):
    bot_token: str
    admin_ids: str = ""


@app.post("/api/bot/token")
async def bot_set_token(data: BotTokenUpdate, _=Depends(verify_admin)):
    import re
    token = data.bot_token.strip()
    admin_ids = data.admin_ids.strip()
    if not token:
        raise HTTPException(status_code=400, detail="Токен не может быть пустым")
    if not re.match(r"^\d+:[A-Za-z0-9_-]{30,}$", token):
        raise HTTPException(status_code=400, detail="Неверный формат токена. Пример: 123456789:AAFxxxx...")
    env_path = "/app/.env"
    existing: dict[str, str] = {}
    if os.path.exists(env_path):
        for line in open(env_path):
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, _, v = line.partition("=")
                existing[k.strip()] = v.strip()
    existing["BOT_TOKEN"] = token
    if admin_ids:
        existing["ADMIN_IDS"] = admin_ids
    with open(env_path, "w") as f:
        f.write("\n".join(f"{k}={v}" for k, v in existing.items()) + "\n")
    os.environ["BOT_TOKEN"] = token
    if admin_ids:
        os.environ["ADMIN_IDS"] = admin_ids
    _supervisorctl("stop", "bot")
    _time.sleep(1)
    rc, out = _supervisorctl("start", "bot")
    return {"ok": True, "output": out}


class AdminIdsUpdate(BaseModel):
    admin_ids: str


@app.post("/api/bot/admins")
async def bot_set_admins(data: AdminIdsUpdate, _=Depends(verify_admin)):
    admin_ids = data.admin_ids.strip()
    env_path = "/app/.env"
    existing: dict[str, str] = {}
    if os.path.exists(env_path):
        for line in open(env_path):
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, _, v = line.partition("=")
                existing[k.strip()] = v.strip()
    existing["ADMIN_IDS"] = admin_ids
    with open(env_path, "w") as f:
        f.write("\n".join(f"{k}={v}" for k, v in existing.items()) + "\n")
    os.environ["ADMIN_IDS"] = admin_ids
    return {"ok": True}



# ── HTML страницы настройки (встроен в Python, не требует Jinja2) ─────────────

_SETUP_HTML = open(
    os.path.join(os.path.dirname(__file__), "templates", "setup.html"),
    encoding="utf-8"
).read()


# ─────────────────────────────────────────────────────────────────────────────
# SUPPORT TICKETS — управление тикетами поддержки
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/api/support/tickets")
async def get_tickets(status: Optional[str] = None, page: int = 1, per_page: int = 50, _=Depends(verify_admin)):
    async with AsyncSessionLocal() as session:
        from sqlalchemy import select, func as sqlfunc
        q = select(SupportTicket).order_by(SupportTicket.created_at.desc())
        if status and status != "all":
            q = q.where(SupportTicket.status == status)
        q = q.offset((page - 1) * per_page).limit(per_page)
        result = await session.execute(q)
        tickets = result.scalars().all()

        items = []
        for t in tickets:
            user = await session.get(User, t.user_id)
            items.append({
                "id": t.id,
                "user_id": t.user_id,
                "user_name": user.full_name if user else str(t.user_id),
                "user_username": user.username if user else None,
                "text": t.text,
                "status": t.status,
                "answer": t.answer,
                "admin_id": t.admin_id,
                "created_at": t.created_at.isoformat() if t.created_at else None,
                "answered_at": t.answered_at.isoformat() if t.answered_at else None,
            })
        return {"items": items, "total": len(items)}


@app.post("/api/support/tickets/{ticket_id}/reply")
async def reply_ticket(ticket_id: int, data: dict, _=Depends(verify_admin)):
    answer = data.get("answer", "").strip()
    if not answer:
        raise HTTPException(status_code=400, detail="Ответ не может быть пустым")
    from datetime import datetime
    async with AsyncSessionLocal() as session:
        ticket = await session.get(SupportTicket, ticket_id)
        if not ticket:
            raise HTTPException(status_code=404, detail="Тикет не найден")
        ticket.answer = answer
        ticket.status = "answered"
        ticket.answered_at = datetime.utcnow()
        await session.commit()

    # Try to send reply via bot
    try:
        import httpx
        bot_token = os.getenv("BOT_TOKEN", "")
        if bot_token:
            msg = f"✅ <b>Ответ на ваш тикет #{ticket_id}</b>\n\n{answer}"
            async with httpx.AsyncClient() as client:
                await client.post(
                    f"https://api.telegram.org/bot{bot_token}/sendMessage",
                    json={"chat_id": ticket.user_id, "text": msg, "parse_mode": "HTML"},
                    timeout=10
                )
    except Exception:
        pass

    return {"ok": True}


@app.post("/api/support/tickets/{ticket_id}/close")
async def close_ticket(ticket_id: int, _=Depends(verify_admin)):
    async with AsyncSessionLocal() as session:
        ticket = await session.get(SupportTicket, ticket_id)
        if not ticket:
            raise HTTPException(status_code=404, detail="Тикет не найден")
        ticket.status = "closed"
        await session.commit()
    return {"ok": True}


@app.delete("/api/support/tickets/{ticket_id}")
async def delete_ticket(ticket_id: int, _=Depends(verify_admin)):
    async with AsyncSessionLocal() as session:
        ticket = await session.get(SupportTicket, ticket_id)
        if not ticket:
            raise HTTPException(status_code=404, detail="Тикет не найден")
        await session.delete(ticket)
        await session.commit()
    return {"ok": True}


@app.get("/api/support/stats")
async def support_stats(_=Depends(verify_admin)):
    async with AsyncSessionLocal() as session:
        from sqlalchemy import select, func as sqlfunc
        open_count = (await session.execute(
            select(sqlfunc.count()).select_from(SupportTicket).where(SupportTicket.status == "open")
        )).scalar()
        answered_count = (await session.execute(
            select(sqlfunc.count()).select_from(SupportTicket).where(SupportTicket.status == "answered")
        )).scalar()
        closed_count = (await session.execute(
            select(sqlfunc.count()).select_from(SupportTicket).where(SupportTicket.status == "closed")
        )).scalar()
        return {"open": open_count, "answered": answered_count, "closed": closed_count}


# ─────────────────────────────────────────────────────────────────────────────
# BALANCE MANAGEMENT — управление балансом пользователей
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/api/users/{user_id}/detail")
async def get_user_detail(user_id: int, _=Depends(verify_admin)):
    async with AsyncSessionLocal() as session:
        user = await session.get(User, user_id)
        if not user:
            raise HTTPException(status_code=404, detail="Пользователь не найден")
        # Balance log
        from sqlalchemy import select
        logs_res = await session.execute(
            select(BalanceLog).where(BalanceLog.user_id == user_id)
            .order_by(BalanceLog.created_at.desc()).limit(20)
        )
        logs = logs_res.scalars().all()
        # Subscriptions
        subs_res = await session.execute(
            select(Subscription).where(Subscription.user_id == user_id)
            .order_by(Subscription.started_at.desc()).limit(5)
        )
        subs = subs_res.scalars().all()

        return {
            "id": user.id,
            "full_name": user.full_name,
            "username": user.username,
            "balance": user.balance or 0,
            "is_banned": user.is_banned,
            "created_at": user.created_at.isoformat() if user.created_at else None,
            "balance_log": [
                {"amount": l.amount, "comment": l.comment,
                 "created_at": l.created_at.isoformat() if l.created_at else None}
                for l in logs
            ],
            "subscriptions": [
                {"status": s.status, "expires_at": s.expires_at.isoformat() if s.expires_at else None,
                 "plan_id": s.plan_id}
                for s in subs
            ]
        }
