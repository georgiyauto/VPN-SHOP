import hashlib
import hmac
import json
import os
import uuid
import logging
import httpx
from sqlalchemy.ext.asyncio import AsyncSession
from db.models import Payment

logger = logging.getLogger(__name__)

# ─── Heleket ────────────────────────────────────────────────────────────────

HELEKET_API_URL = "https://heleket.com/api/v1"


async def create_heleket_invoice(amount_rub: float, order_id: str, description: str) -> dict | None:
    api_key = os.getenv("HELEKET_API_KEY", "")
    shop_id = os.getenv("HELEKET_SHOP_ID", "")
    bot_domain = os.getenv("BOT_DOMAIN", "")

    payload = {
        "shop_id": shop_id,
        "amount": str(amount_rub),
        "currency": "RUB",
        "order_id": order_id,
        "description": description,
        "success_url": f"{bot_domain}/heleket/success",
        "fail_url": f"{bot_domain}/heleket/fail",
        "callback_url": f"{bot_domain}/heleket/webhook",
    }
    sign_str = ":".join([str(payload[k]) for k in sorted(payload.keys())])
    payload["sign"] = hmac.new(
        os.getenv("HELEKET_SECRET", "").encode(),
        sign_str.encode(),
        hashlib.sha256
    ).hexdigest()

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                f"{HELEKET_API_URL}/payment/create",
                headers={"Authorization": f"Bearer {api_key}"},
                json=payload
            )
            data = r.json()
            if data.get("status") == "success":
                return {"url": data["data"]["url"], "payment_id": data["data"]["payment_id"]}
    except Exception as e:
        logger.error(f"Heleket invoice error: {e}")
    return None


def verify_heleket_webhook(body: bytes, sign_header: str) -> bool:
    secret = os.getenv("HELEKET_SECRET", "")
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sign_header)


# ─── CryptoPay ──────────────────────────────────────────────────────────────

CRYPTO_PAY_API = "https://pay.crypt.bot/api"


async def create_cryptopay_invoice(amount_rub: float, order_id: str, description: str) -> dict | None:
    token = os.getenv("CRYPTOPAY_TOKEN", "")
    # CryptoPay работает в USDT — примерный курс 90 руб/USDT
    # В продакшне нужно брать актуальный курс
    amount_usdt = round(amount_rub / 90, 2)

    payload = {
        "asset": "USDT",
        "amount": str(amount_usdt),
        "description": description,
        "payload": order_id,
        "allow_comments": False,
        "allow_anonymous": False,
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                f"{CRYPTO_PAY_API}/createInvoice",
                headers={"Crypto-Pay-API-Token": token},
                json=payload
            )
            data = r.json()
            if data.get("ok"):
                inv = data["result"]
                return {
                    "url": inv["bot_invoice_url"],
                    "invoice_id": inv["invoice_id"],
                    "amount_usdt": amount_usdt,
                }
    except Exception as e:
        logger.error(f"CryptoPay invoice error: {e}")
    return None


def verify_cryptopay_webhook(body: bytes, sign_header: str) -> bool:
    token = os.getenv("CRYPTOPAY_TOKEN", "")
    secret = hashlib.sha256(token.encode()).digest()
    expected = hmac.new(secret, body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sign_header)


# ─── Helpers ────────────────────────────────────────────────────────────────

async def create_payment_record(
    session: AsyncSession,
    user_id: int,
    plan_id: int,
    method: str,
    amount: float,
    external_id: str = None,
) -> Payment:
    payment = Payment(
        user_id=user_id,
        plan_id=plan_id,
        method=method,
        amount=amount,
        status="pending",
        external_id=external_id or str(uuid.uuid4()),
    )
    session.add(payment)
    await session.flush()
    return payment
