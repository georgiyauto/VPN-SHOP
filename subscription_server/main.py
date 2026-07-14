from dotenv import load_dotenv
load_dotenv("/app/.env", override=True)
import asyncio
import base64
import logging
import os
from datetime import datetime

import httpx
from fastapi import FastAPI, HTTPException, Request, Header
from fastapi.responses import PlainTextResponse, JSONResponse
from sqlalchemy import select

from db.database import AsyncSessionLocal, init_db
from db.models import User, Subscription, Server, Payment, Settings
from bot.services.xray_client import XrayClient
from bot.services.payment_service import verify_heleket_webhook, verify_cryptopay_webhook
from bot.services.sbp_service import verify_sbp_webhook

app = FastAPI(title="VPN Subscription Server")
logger = logging.getLogger(__name__)


@app.on_event("startup")
async def startup():
    await init_db()


# ─── Subscription endpoint ──────────────────────────────────────────────────

@app.get("/sub/{token}", response_class=PlainTextResponse)
async def get_subscription(token: str, request: Request = None):
    """
    Главный endpoint — отдаёт base64 файл со всеми конфигами пользователя.
    Вставляется в Hiddify / v2rayNG / Sing-Box как ссылка подписки.
    """
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(User).where(User.sub_token == token)
        )
        user = result.scalar_one_or_none()
        if not user:
            raise HTTPException(status_code=404, detail="Not found")
        if user.is_banned:
            raise HTTPException(status_code=403, detail="User is banned")

        sub_result = await session.execute(
            select(Subscription)
            .where(Subscription.user_id == user.id)
            .where(Subscription.status == "active")
            .order_by(Subscription.expires_at.desc())
        )
        sub = sub_result.scalars().first()
        if not sub:
            raise HTTPException(status_code=403, detail="No active subscription")

        # Проверяем не истекла ли
        if sub.expires_at and sub.expires_at < datetime.now():
            raise HTTPException(status_code=403, detail="Subscription expired")

        # Серверы для тарифа пользователя (если привязаны) или все
        from db.models import PlanServer
        plan_id = sub.plan_id if sub else None
        if plan_id:
            ps_result = await session.execute(
                select(Server)
                .join(PlanServer, PlanServer.server_id == Server.id)
                .where(PlanServer.plan_id == plan_id)
                .where(Server.is_active == True)
                .where(Server.install_status == "ready")
                .where(Server.node_url.is_not(None))
                .where(Server.node_token.is_not(None))
                .order_by(Server.sort_order)
            )
            servers = ps_result.scalars().all()
        if not plan_id or not servers:
            servers_result = await session.execute(
                select(Server)
                .where(Server.is_active == True)
                .where(Server.install_status == "ready")
                .where(Server.node_url.is_not(None))
                .where(Server.node_token.is_not(None))
                .order_by(Server.sort_order)
            )
            servers = servers_result.scalars().all()

    if not servers:
        raise HTTPException(status_code=503, detail="No servers available")

    # Параллельно собираем конфиги со всех серверов
    tasks = [_fetch_server_configs(srv, str(user.xray_uuid)) for srv in servers]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    all_configs = []
    for result in results:
        if isinstance(result, list):
            all_configs.extend(result)

    if not all_configs:
        raise HTTPException(status_code=503, detail="No configs available")

    # Добавляем флаги к меткам серверов если их нет
    from utils.flags import label_with_flag
    labeled_configs = []
    for cfg in all_configs:
        # Находим #label в конце конфига и добавляем флаг
        import re
        from urllib.parse import unquote, quote
        def add_flag_to_label(m):
            label = unquote(m.group(1))
            label_flagged = label_with_flag(label)
            return "#" + quote(label_flagged)
        cfg = re.sub(r"#(.+)$", add_flag_to_label, cfg)
        labeled_configs.append(cfg)

    # Получаем project_name для заголовка подписки
    project_name = "VPN"
    try:
        async with AsyncSessionLocal() as session:
            from sqlalchemy import select as _sel
            from db.models import Settings as _S
            _r = await session.execute(_sel(_S).where(_S.id == 1))
            _s = _r.scalar_one_or_none()
            if _s and getattr(_s, "project_name", None):
                project_name = _s.project_name
    except Exception:
        pass

    raw = "\n".join(labeled_configs)
    encoded = base64.b64encode(raw.encode()).decode()

    from fastapi.responses import Response as FastResponse
    return FastResponse(
        content=encoded,
        media_type="text/plain; charset=utf-8",
        headers={
            "subscription-userinfo": f"upload=0; download=0; total=0; expire={int(sub.expires_at.timestamp()) if sub and sub.expires_at else 0}",
            "profile-title": f"base64:{base64.b64encode(project_name.encode()).decode()}",
            "profile-update-interval": "12",
            "content-disposition": "attachment; filename=\"vpn.txt\"",
        }
    )


async def _fetch_server_configs(server: Server, uuid: str) -> list[str]:
    """Fetch current client links from a plain-Xray node."""
    try:
        async with XrayClient(
            server.node_url, node_path=server.node_path or "/", node_token=server.node_token,
            node_cert=server.node_cert,
        ) as xray:
            return await xray.get_client_configs(server.inbound_id, uuid, server.label, email=uuid)
    except Exception as e:
        logger.warning(f"Failed to fetch configs from {server.label}: {e}")
        return []


# ─── Heleket webhook ────────────────────────────────────────────────────────

@app.post("/heleket/webhook")
async def heleket_webhook(request: Request):
    body = await request.body()
    sign = request.headers.get("X-Sign", "")

    if not verify_heleket_webhook(body, sign):
        raise HTTPException(status_code=400, detail="Invalid signature")

    data = await request.json()
    if data.get("status") == "paid":
        order_id = data.get("order_id")
        await _process_successful_payment(int(order_id), data.get("payment_id"))

    return JSONResponse({"ok": True})


# ─── CryptoPay webhook ───────────────────────────────────────────────────────

@app.post("/cryptopay/webhook")
async def cryptopay_webhook(request: Request):
    body = await request.body()
    sign = request.headers.get("Crypto-Pay-API-Token", "")

    if not verify_cryptopay_webhook(body, sign):
        raise HTTPException(status_code=400, detail="Invalid signature")

    data = await request.json()
    if data.get("update_type") == "invoice_paid":
        invoice = data.get("payload", {})
        order_id = invoice.get("payload")  # мы передавали order_id в payload
        await _process_successful_payment(int(order_id), str(invoice.get("invoice_id")))

    return JSONResponse({"ok": True})


# ─── Platega СБП webhook ─────────────────────────────────────────────────────

@app.post("/sbp/webhook")
async def sbp_webhook(request: Request):
    """
    Platega.io отправляет POST с заголовками X-MerchantId и X-Secret.
    Тело: JSON с полями order_id, status (CONFIRMED / CANCELED / CHARGEBACK),
          amount, invoice_id.
    """
    body = await request.body()
    headers = dict(request.headers)

    # Получаем настройки из БД для проверки подписи
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Settings).where(Settings.id == 1))
        settings = result.scalar_one_or_none()

    if not settings:
        raise HTTPException(status_code=500, detail="Settings not found")

    merchant_id = getattr(settings, "sbp_merchant_id", "") or ""
    secret_key  = getattr(settings, "sbp_secret_key", "") or ""

    if not verify_sbp_webhook(headers, merchant_id, secret_key):
        logger.warning("SBP webhook: invalid signature")
        raise HTTPException(status_code=400, detail="Invalid signature")

    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    logger.info(f"SBP webhook received: {data}")

    status   = data.get("status", "")
    order_id = data.get("order_id") or data.get("orderId", "")
    amount   = float(data.get("amount", 0) or 0)

    if status == "CONFIRMED" and order_id:
        # Ищем payment по external_id (= order_id / invoice_id от Platega)
        async with AsyncSessionLocal() as session:
            from db.models import Payment as Pmt
            result = await session.execute(
                select(Pmt).where(Pmt.external_id == str(order_id))
            )
            payment = result.scalar_one_or_none()

        if payment and payment.status != "paid":
            await _process_successful_payment(payment.id, str(order_id))
        else:
            logger.info(f"SBP webhook: payment {order_id} not found or already paid")

    elif status in ("CANCELED", "CHARGEBACK"):
        logger.info(f"SBP webhook: payment {order_id} status={status}")

    # Platega ждёт HTTP 200 — всегда возвращаем ok
    return JSONResponse({"ok": True})


@app.get("/sbp/success")
async def sbp_success():
    return JSONResponse({"status": "ok", "message": "Оплата обработана"})


@app.get("/sbp/fail")
async def sbp_fail():
    return JSONResponse({"status": "fail", "message": "Платёж не завершён"})


# ─── Common payment processor ────────────────────────────────────────────────

async def _process_successful_payment(payment_id: int, external_id: str = None):
    from bot.handlers.payment_handlers import handle_successful_payment
    from aiogram import Bot

    bot = Bot(token=os.getenv("BOT_TOKEN"))
    try:
        await handle_successful_payment(payment_id, bot)
    finally:
        await bot.session.close()


# ─── Happ redirect page ──────────────────────────────────────────────────────

HAPP_REDIRECT_HTML = """<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Подключение к VPN...</title>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
      background: #0d1117; color: #e6edf3;
      display: flex; align-items: center; justify-content: center;
      min-height: 100vh; padding: 20px;
    }}
    .card {{
      background: #161b22; border: 1px solid #30363d;
      border-radius: 16px; padding: 32px 24px;
      max-width: 420px; width: 100%; text-align: center;
    }}
    .logo {{ font-size: 56px; margin-bottom: 16px; }}
    h1 {{ font-size: 22px; font-weight: 700; margin-bottom: 8px; }}
    p {{ color: #8b949e; font-size: 15px; line-height: 1.6; margin-bottom: 24px; }}
    .btn {{
      display: block; width: 100%;
      background: linear-gradient(135deg, #6e40c9, #8b5cf6);
      color: #fff; border: none; border-radius: 12px;
      padding: 14px 24px; font-size: 16px; font-weight: 600;
      cursor: pointer; text-decoration: none; margin-bottom: 12px;
    }}
    .btn-secondary {{
      background: #21262d; border: 1px solid #30363d; color: #e6edf3;
    }}
    .hint {{ font-size: 13px; color: #6e7681; margin-top: 16px; }}
    .spinner {{
      width: 32px; height: 32px; border: 3px solid #30363d;
      border-top-color: #8b5cf6; border-radius: 50%;
      animation: spin 0.8s linear infinite; margin: 0 auto 20px;
    }}
    @keyframes spin {{ to {{ transform: rotate(360deg); }} }}
  </style>
</head>
<body>
  <div class="card">
    <div class="logo">🔐</div>
    <h1>Открываю Happ VPN</h1>
    <p>Подписка добавится автоматически.<br>Разрешите открытие приложения когда появится запрос.</p>
    <div class="spinner" id="spinner"></div>
    <a class="btn" id="btn-open" href="{happ_url}">📱 Открыть Happ</a>
    <a class="btn btn-secondary" href="{sub_url}" id="btn-copy" onclick="copyLink(event)">📋 Скопировать ссылку</a>
    <p class="hint">Нет приложения? <a href="https://play.google.com/store/apps/details?id=com.happ.vpn" style="color:#8b5cf6">Скачать Happ</a> для Android<br>или <a href="https://apps.apple.com/app/happ-proxy-utility/id6504287215" style="color:#8b5cf6">App Store</a> для iOS</p>
  </div>
  <script>
    const happUrl = "{happ_url}";
    const subUrl = "{sub_url}";
    // Автоматически открываем приложение через 800мс
    setTimeout(() => {{
      window.location.href = happUrl;
      document.getElementById('spinner').style.display = 'none';
    }}, 800);
    function copyLink(e) {{
      e.preventDefault();
      navigator.clipboard.writeText(subUrl).then(() => {{
        document.getElementById('btn-copy').textContent = '✅ Скопировано!';
        setTimeout(() => document.getElementById('btn-copy').textContent = '📋 Скопировать ссылку', 2000);
      }});
    }}
  </script>
</body>
</html>"""


from fastapi.responses import HTMLResponse

@app.get("/app", response_class=HTMLResponse)
async def happ_redirect(url: str = ""):
    """Промежуточная страница для открытия Happ — редиректит на happ://import?url=..."""
    if not url:
        raise HTTPException(status_code=400, detail="url parameter required")
    from urllib.parse import quote
    happ_url = f"happ://import?url={quote(url, safe='')}"
    html = HAPP_REDIRECT_HTML.format(
        happ_url=happ_url,
        sub_url=url,
    )
    return HTMLResponse(content=html)




# ─── Sub info (для Mini App) ─────────────────────────────────────────────────

@app.get("/sub-info/{token}")
async def get_sub_info(token: str):
    """Возвращает JSON с информацией о подписке для Mini App."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.sub_token == token))
        user = result.scalar_one_or_none()
        if not user:
            raise HTTPException(status_code=404, detail="Not found")

        from sqlalchemy.orm import joinedload
        sub_result = await session.execute(
            select(Subscription)
            .options(joinedload(Subscription.plan))
            .where(Subscription.user_id == user.id)
            .where(Subscription.status == "active")
            .order_by(Subscription.expires_at.desc())
        )
        sub = sub_result.scalar_one_or_none()
        if not sub:
            raise HTTPException(status_code=403, detail="No active subscription")

        try:
            plan_name = sub.plan.name if sub.plan else "VPN подписка"
        except Exception:
            plan_name = "VPN подписка"

        return {
            "user_id": user.id,
            "username": user.username,
            "plan_name": plan_name,
            "expires_at": sub.expires_at.isoformat() if sub.expires_at else None,
            "traffic_limit_gb": sub.traffic_limit_gb,
            "traffic_used_gb": sub.traffic_used_gb or 0,
        }


# ─── Mini App (Setup page) ───────────────────────────────────────────────────

@app.get("/setup", response_class=HTMLResponse)
async def miniapp_setup(url: str = "", token: str = ""):
    """Telegram Mini App для установки VPN."""
    import os as _os
    # Если url не передан — ищем по token
    if not url and token:
        async with AsyncSessionLocal() as session:
            result = await session.execute(select(User).where(User.sub_token == token))
            user = result.scalar_one_or_none()
            if user:
                domain = _os.getenv("BOT_DOMAIN", "").rstrip("/")
                port = _os.getenv("SUB_PORT", "8433")
                base = domain if ":" in domain.split("//")[-1] else f"{domain}:{port}"
                url = f"{base}/sub/{token}"

    html_path = _os.path.join(_os.path.dirname(__file__), "..", "website", "miniapp_setup.html")
    try:
        html = open(html_path).read()
        # Подставляем url и token в скрипт
        html = html.replace(
            "const SUB_URL = params.get('url') || '';",
            f"const SUB_URL = params.get('url') || '{url}';"
        ).replace(
            "const SUB_TOKEN = params.get('token') || '';",
            f"const SUB_TOKEN = params.get('token') || '{token}';"
        )
    except Exception as e:
        logger.error(f"miniapp_setup read error: {e}")
        html = f"<html><body>Ошибка загрузки страницы: {e}</body></html>"
    return HTMLResponse(content=html)

# ─── Health check ────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "time": datetime.now().isoformat()}
