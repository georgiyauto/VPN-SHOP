"""
Website API — единственный источник правды для сайта Kawa VPN.
Все данные (тарифы, настройки, серверы) читаются из той же БД что и бот/админка.
CORS открыт. Кэш отключён. Сайт всегда видит актуальные данные.
"""
import os
import hashlib
import secrets
import logging
from datetime import datetime, timedelta

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy import select, func

from db.database import AsyncSessionLocal, init_db
from db.models import User, Subscription, Plan, Server, Payment, Settings
from bot.services.vpn_service import get_active_subscription, _add_client_to_server
from bot.services.payment_service import (
    create_payment_record, create_heleket_invoice, create_cryptopay_invoice
)

app = FastAPI(title="Kawa VPN Website API", docs_url=None, redoc_url=None)
from website.miniapp_api import miniapp_router
app_v4_patched = True  # marker
logger = logging.getLogger(__name__)
WEBSITE_DIR = os.path.dirname(__file__)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

app.include_router(miniapp_router, prefix="/api/miniapp", tags=["miniapp"])


def nc(data: dict) -> JSONResponse:
    """JSONResponse с no-cache заголовками."""
    return JSONResponse(
        content=data,
        headers={"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"},
    )


@app.on_event("startup")
async def startup():
    await init_db()


# ── Сайт ─────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index():
    with open(os.path.join(WEBSITE_DIR, "index.html"), "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read(), headers={"Cache-Control": "no-store"})


# ── Тарифы — живые из БД ─────────────────────────────────────────────────────

@app.get("/api/web/plans")
async def get_plans():
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Plan).where(Plan.is_active == True).order_by(Plan.sort_order)
        )
        plans = result.scalars().all()
        return nc({
            "plans": [
                {
                    "id": p.id, "name": p.name, "description": p.description,
                    "price_rub": p.price_rub, "duration_days": p.duration_days,
                    "traffic_gb": p.traffic_gb,
                }
                for p in plans
            ]
        })


# ── Настройки — живые из БД ──────────────────────────────────────────────────

@app.get("/api/web/settings")
async def get_public_settings():
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Settings).where(Settings.id == 1))
        s = result.scalar_one_or_none()
        if not s:
            return nc({})

        srv_result = await session.execute(
            select(Server).where(Server.is_active == True).order_by(Server.sort_order)
        )
        servers = srv_result.scalars().all()

        return nc({
            "support_username": s.support_username,
            "manual_payment_enabled": s.manual_payment_enabled,
            "manual_payment_text": s.manual_payment_text,
            "heleket_enabled": s.heleket_enabled,
            "cryptopay_enabled": s.cryptopay_enabled,
            "card_link_enabled": s.card_link_enabled,
            "card_link_url": s.card_link_url if s.card_link_enabled else None,
            "card_link_text": s.card_link_text,
            "trial_enabled": s.trial_enabled,
            "trial_days": s.trial_days,
            "trial_traffic_gb": s.trial_traffic_gb,
            "referral_enabled": s.referral_enabled,
            "referral_days_reward": s.referral_days_reward,
            "welcome_text": s.welcome_text,
            "server_count": len(servers),
            "server_locations": [{"label": srv.label, "flag": srv.flag} for srv in servers],
            "bot_domain": os.getenv("BOT_DOMAIN", ""),
            "google_client_id": os.getenv("GOOGLE_CLIENT_ID", ""),
        })


# ── Публичный список серверов (без паролей) ───────────────────────────────────

@app.get("/api/web/servers")
async def get_public_servers():
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Server).where(Server.is_active == True).order_by(Server.sort_order)
        )
        servers = result.scalars().all()
        return nc({"servers": [{"label": srv.label, "flag": srv.flag} for srv in servers]})


# ── Публичная статистика для главной страницы ─────────────────────────────────

@app.get("/api/web/stats")
async def get_public_stats():
    async with AsyncSessionLocal() as session:
        server_count = (await session.execute(
            select(func.count(Server.id)).where(Server.is_active == True)
        )).scalar() or 0

        active_users = (await session.execute(
            select(func.count(Subscription.id))
            .where(Subscription.status == "active")
            .where(Subscription.expires_at > datetime.now())
        )).scalar() or 0

        return nc({"server_count": server_count, "active_users": active_users})


# ── Auth ──────────────────────────────────────────────────────────────────────

class AuthRequest(BaseModel):
    telegram_id: int
    password: str


def _hash(password: str, salt: str) -> str:
    return hashlib.sha256(f"{salt}{password}".encode()).hexdigest()


@app.post("/api/web/register")
async def register(data: AuthRequest):
    if len(data.password) < 6:
        raise HTTPException(400, detail="Пароль минимум 6 символов")
    async with AsyncSessionLocal() as session:
        existing = await session.get(User, data.telegram_id)
        if existing and existing.web_password_hash:
            raise HTTPException(400, detail="Аккаунт уже существует. Войдите.")

        salt = secrets.token_hex(8)
        token = secrets.token_hex(32)

        if existing:
            existing.web_password_hash = f"{salt}:{_hash(data.password, salt)}"
            existing.web_token = token
            user = existing
        else:
            import uuid, random, string
            user = User(
                id=data.telegram_id,
                sub_token=uuid.uuid4(),
                xray_uuid=uuid.uuid4(),
                referral_code=''.join(random.choices(string.ascii_uppercase + string.digits, k=8)),
                web_password_hash=f"{salt}:{_hash(data.password, salt)}",
                web_token=token,
            )
            session.add(user)
        await session.commit()
        return nc({"ok": True, "user": {"telegram_id": user.id, "token": token, "sub_token": str(user.sub_token)}})


@app.post("/api/web/login")
async def login(data: AuthRequest):
    async with AsyncSessionLocal() as session:
        user = await session.get(User, data.telegram_id)
        if not user or not user.web_password_hash:
            raise HTTPException(401, detail="Аккаунт не найден. Зарегистрируйтесь.")
        salt, stored = user.web_password_hash.split(":", 1)
        if _hash(data.password, salt) != stored:
            raise HTTPException(401, detail="Неверный пароль")
        if user.is_banned:
            raise HTTPException(403, detail="Аккаунт заблокирован")
        token = secrets.token_hex(32)
        user.web_token = token
        await session.commit()
        return nc({"ok": True, "user": {"telegram_id": user.id, "token": token, "sub_token": str(user.sub_token)}})


async def _verify(telegram_id: int, token: str) -> User:
    async with AsyncSessionLocal() as session:
        user = await session.get(User, telegram_id)
        if not user or user.web_token != token:
            raise HTTPException(401, detail="Сессия истекла. Войдите снова.")
        if user.is_banned:
            raise HTTPException(403, detail="Аккаунт заблокирован")
        return user


# ── Личный кабинет ────────────────────────────────────────────────────────────

@app.get("/api/web/me")
async def get_me(telegram_id: int, token: str):
    await _verify(telegram_id, token)
    async with AsyncSessionLocal() as session:
        user = await session.get(User, telegram_id)
        sub = await get_active_subscription(session, telegram_id)
        ref_count = (await session.execute(
            select(func.count(User.id)).where(User.referred_by == telegram_id)
        )).scalar() or 0
        s_r = await session.execute(select(Settings).where(Settings.id == 1))
        settings = s_r.scalar_one_or_none()

        return nc({
            "user": {
                "telegram_id": user.id,
                "username": user.username,
                "sub_token": str(user.sub_token),
                "trial_used": user.trial_used,
                "referral_code": user.referral_code,
                "referral_count": ref_count,
                "email": user.google_email or "",
                "name": user.full_name or "",
            },
            "subscription": {
                "plan_id": sub.plan_id,
                "status": sub.status,
                "expires_at": sub.expires_at.isoformat() if sub.expires_at else None,
                "traffic_limit_gb": sub.traffic_limit_gb,
                "traffic_used_gb": round(sub.traffic_used_gb or 0, 2),
            } if sub else None,
            "bot_domain": os.getenv("BOT_DOMAIN", ""),
            "trial_enabled": settings.trial_enabled if settings else False,
            "referral_days_reward": settings.referral_days_reward if settings else 0,
            "referral_enabled": settings.referral_enabled if settings else False,
        })


# ── Создать платёж ────────────────────────────────────────────────────────────

class CreatePaymentRequest(BaseModel):
    telegram_id: int
    token: str
    plan_id: int
    method: str


@app.post("/api/web/create-payment")
async def create_payment_web(data: CreatePaymentRequest):
    await _verify(data.telegram_id, data.token)
    async with AsyncSessionLocal() as session:
        plan = await session.get(Plan, data.plan_id)
        if not plan or not plan.is_active:
            raise HTTPException(404, "Тариф не найден")
        s_r = await session.execute(select(Settings).where(Settings.id == 1))
        settings = s_r.scalar_one()
        if data.method == "heleket" and not settings.heleket_enabled:
            raise HTTPException(400, "Способ оплаты недоступен")
        if data.method == "cryptopay" and not settings.cryptopay_enabled:
            raise HTTPException(400, "Способ оплаты недоступен")
        payment = await create_payment_record(
            session, data.telegram_id, data.plan_id, data.method, plan.price_rub
        )
        order_id = str(payment.id)
        plan_name = plan.name
        plan_price = plan.price_rub
        await session.commit()

    if data.method == "heleket":
        inv = await create_heleket_invoice(plan_price, order_id, f"Kawa VPN · {plan_name}")
        if not inv:
            raise HTTPException(500, "Ошибка создания платежа")
        return nc({"url": inv["url"]})

    if data.method == "cryptopay":
        inv = await create_cryptopay_invoice(plan_price, order_id, f"Kawa VPN · {plan_name}")
        if not inv:
            raise HTTPException(500, "Ошибка создания платежа")
        return nc({"url": inv["url"], "amount_usdt": inv.get("amount_usdt")})

    raise HTTPException(400, "Неизвестный метод")


# ── Пробный период ────────────────────────────────────────────────────────────

class TokenRequest(BaseModel):
    telegram_id: int
    token: str


@app.post("/api/web/trial")
async def activate_trial_web(data: TokenRequest):
    await _verify(data.telegram_id, data.token)
    async with AsyncSessionLocal() as session:
        user = await session.get(User, data.telegram_id)
        s_r = await session.execute(select(Settings).where(Settings.id == 1))
        settings = s_r.scalar_one()

        if user.trial_used:
            raise HTTPException(400, "Пробный период уже использован")
        if not settings.trial_enabled:
            raise HTTPException(400, "Пробный период отключён")
        if await get_active_subscription(session, data.telegram_id):
            raise HTTPException(400, "У вас уже есть активная подписка")

        from bot.services.vpn_service import get_all_active_servers
        import asyncio

        user.trial_used = True
        expires_at = datetime.now() + timedelta(days=settings.trial_days)
        new_sub = Subscription(
            user_id=user.id, plan_id=None, status="active",
            traffic_limit_gb=settings.trial_traffic_gb, expires_at=expires_at,
        )
        session.add(new_sub)
        await session.commit()
        servers = await get_all_active_servers(session)
        xray_uuid = str(user.xray_uuid)
        expire_ms = int(expires_at.timestamp() * 1000)
        traffic = settings.trial_traffic_gb

    import asyncio
    await asyncio.gather(*[
        _add_client_to_server(srv, xray_uuid, expire_ms, traffic)
        for srv in servers
    ], return_exceptions=True)

    return nc({"ok": True, "expires_at": expires_at.isoformat(), "days": settings.trial_days})


# ─── WebApp API ───────────────────────────────────────────────────────────────

from fastapi.responses import FileResponse
import os as _os

@app.get("/webapp/")
async def webapp_index():
    """Telegram Mini App HTML."""
    path = _os.path.join(_os.path.dirname(__file__), "webapp.html")
    return FileResponse(path)


@app.get("/api/webapp/status")
async def webapp_status(token: str):
    """Статус подписки пользователя для Mini App — по sub_token."""
    import uuid as _uuid
    from db.database import AsyncSessionLocal
    from db.models import User, Subscription, Plan
    from sqlalchemy import select
    from datetime import datetime as dt

    async with AsyncSessionLocal() as session:
        try:
            token_uuid = _uuid.UUID(token)
        except ValueError:
            return {"has_sub": False}

        result = await session.execute(select(User).where(User.sub_token == token_uuid))
        user = result.scalar_one_or_none()
        if not user:
            return {"has_sub": False}

        sub_result = await session.execute(
            select(Subscription, Plan)
            .outerjoin(Plan, Plan.id == Subscription.plan_id)
            .where(Subscription.user_id == user.id)
            .where(Subscription.status == "active")
            .order_by(Subscription.expires_at.desc())
        )
        row = sub_result.first()

    BOT_DOMAIN = _os.getenv("BOT_DOMAIN", "")
    sub_url = f"{BOT_DOMAIN}/sub/{token}"

    if not row:
        return {
            "has_sub": False,
            "sub_url": sub_url,
            "balance": user.balance or 0,
            "full_name": user.full_name,
        }

    sub, plan = row
    days_left = (sub.expires_at - dt.now()).days if sub.expires_at else 9999

    return {
        "has_sub": True,
        "sub_url": sub_url,
        "plan_name": plan.name if plan else "—",
        "days_left": max(0, days_left),
        "traffic_used": sub.traffic_used_gb or 0,
        "traffic_limit": sub.traffic_limit_gb,
        "max_devices": plan.max_devices if plan and hasattr(plan, "max_devices") else 1,
        "balance": user.balance or 0,
        "full_name": user.full_name,
    }


# ── Google OAuth ──────────────────────────────────────────────────────────────
# Uses Google Identity "id_token" flow (sign-in with Google button sends
# a credential JWT that we verify against Google's public keys).

class GoogleAuthRequest(BaseModel):
    credential: str          # JWT from Google Identity Services


async def _verify_google_token(credential: str) -> dict:
    """Verify Google ID token via Google's tokeninfo endpoint."""
    import httpx
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(
            "https://oauth2.googleapis.com/tokeninfo",
            params={"id_token": credential}
        )
    if r.status_code != 200:
        raise HTTPException(401, detail="Недействительный токен Google")
    data = r.json()
    if "error" in data:
        raise HTTPException(401, detail="Токен Google отклонён")
    # Verify audience matches our client_id
    client_id = os.getenv("GOOGLE_CLIENT_ID", "")
    if client_id and data.get("aud") != client_id:
        raise HTTPException(401, detail="Токен выдан другому приложению")
    return data


@app.post("/api/web/google-auth")
async def google_auth(data: GoogleAuthRequest):
    """Sign in / register via Google. Creates account if first time."""
    import uuid, random, string

    google_data = await _verify_google_token(data.credential)

    google_id = google_data.get("sub")
    email = google_data.get("email", "")
    name = google_data.get("name") or google_data.get("given_name") or email.split("@")[0]

    if not google_id:
        raise HTTPException(401, detail="Не удалось получить ID Google аккаунта")

    async with AsyncSessionLocal() as session:
        # Find existing user by google_id
        result = await session.execute(
            select(User).where(User.google_id == google_id)
        )
        user = result.scalar_one_or_none()

        if not user:
            # New user — create account (no telegram_id, use negative synthetic ID)
            # We use a hash of google_id as a stable synthetic user_id
            import hashlib
            synthetic_id = int(hashlib.sha256(google_id.encode()).hexdigest()[:12], 16) % (10**15)
            # Make sure it doesn't collide
            existing_by_id = await session.get(User, synthetic_id)
            if existing_by_id and not existing_by_id.google_id:
                synthetic_id += 1

            token = secrets.token_hex(32)
            ref_code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))

            user = User(
                id=synthetic_id,
                google_id=google_id,
                google_email=email,
                full_name=name,
                username=email.split("@")[0],
                sub_token=uuid.uuid4(),
                xray_uuid=uuid.uuid4(),
                referral_code=ref_code,
                web_token=token,
            )
            session.add(user)
        else:
            # Existing user — refresh token and update name/email
            token = secrets.token_hex(32)
            user.web_token = token
            user.google_email = email
            if name and not user.full_name:
                user.full_name = name

        if user.is_banned:
            raise HTTPException(403, detail="Аккаунт заблокирован")

        await session.commit()

        return nc({
            "ok": True,
            "user": {
                "telegram_id": user.id,
                "token": user.web_token,
                "sub_token": str(user.sub_token),
                "name": user.full_name or name,
                "email": user.google_email,
            }
        })
