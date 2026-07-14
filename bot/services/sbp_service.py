"""
СБП-платежи через Platega.io
Документация: https://app.platega.io/
Вебхук: заголовки X-MerchantId + X-Secret, статусы CONFIRMED / CANCELED / CHARGEBACK
"""
import logging
import httpx

logger = logging.getLogger(__name__)

PLATEGA_API_URL = "https://app.platega.io"


def _get_platega_creds(settings) -> tuple[str, str]:
    """Возвращает (merchant_id, secret_key) из настроек БД."""
    return (
        getattr(settings, "sbp_merchant_id", "") or "",
        getattr(settings, "sbp_secret_key", "") or "",
    )


async def create_sbp_invoice(
    amount_rub: float,
    order_id: str,
    description: str,
    settings,
    bot_domain: str,
) -> dict | None:
    """
    Создаёт счёт в Platega.io через СБП.
    Комиссия 11% включена — пользователь платит amount_rub.
    Возвращает dict: { url, invoice_id } или None при ошибке.
    """
    merchant_id, secret_key = _get_platega_creds(settings)
    if not merchant_id or not secret_key:
        logger.error("Platega SBP: merchant_id или secret_key не настроены")
        return None

    payload = {
        "merchant_id": merchant_id,
        "amount": round(float(amount_rub), 2),
        "order_id": order_id,
        "description": description,
        "callback_url": f"{bot_domain}/sbp/webhook",
        "success_url": f"{bot_domain}/sbp/success",
        "fail_url": f"{bot_domain}/sbp/fail",
        "payment_method": "sbp",
    }

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                f"{PLATEGA_API_URL}/api/payment/create",
                headers={
                    "X-MerchantId": merchant_id,
                    "X-Secret": secret_key,
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                json=payload,
            )
            data = r.json()
            logger.info(f"Platega create [{r.status_code}]: {data}")

            if r.status_code in (200, 201) and data.get("url"):
                return {
                    "url": data["url"],
                    "invoice_id": str(data.get("id") or data.get("invoice_id") or order_id),
                }
            else:
                logger.error(f"Platega error: {r.status_code} — {data}")
    except Exception as e:
        logger.error(f"Platega exception: {e}")

    return None


def verify_sbp_webhook(headers: dict, merchant_id_expected: str, secret_expected: str) -> bool:
    """
    Проверяет подлинность вебхука от Platega.
    Platega передаёт X-MerchantId и X-Secret в заголовках POST-запроса.
    """
    if not merchant_id_expected or not secret_expected:
        return False
    incoming_mid = headers.get("x-merchantid") or headers.get("X-MerchantId", "")
    incoming_sec = headers.get("x-secret") or headers.get("X-Secret", "")
    return incoming_mid == merchant_id_expected and incoming_sec == secret_expected
