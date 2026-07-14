"""Rotate Reality SNI through the authenticated plain-Xray node API."""
from __future__ import annotations

import os
import random
from pathlib import Path

from bot.services.xray_client import XrayClient

SNI_WHITELIST_PATH = Path(os.getenv(
    "SNI_WHITELIST_PATH",
    Path(__file__).resolve().parents[1] / "whitelist.txt",
))
FINGERPRINTS = ["chrome", "firefox", "safari"]


def load_whitelist() -> list[str]:
    try:
        values = []
        for raw in SNI_WHITELIST_PATH.read_text(encoding="utf-8").splitlines():
            value = raw.strip().lower()
            if value and not value.startswith("#") and "." in value and " " not in value:
                values.append(value)
        return values
    except FileNotFoundError:
        return ["www.microsoft.com", "www.cloudflare.com", "www.apple.com"]


async def rotate_sni_on_server(
    node_url: str,
    node_token: str,
    inbound_id: int,
    node_path: str = "/",
    node_cert: str | None = None,
) -> dict:
    choices = load_whitelist()
    if not choices:
        return {"ok": False, "error": "SNI whitelist is empty"}
    sni = random.choice(choices)
    fingerprint = random.choice(FINGERPRINTS)
    try:
        async with XrayClient(
            node_url, node_path=node_path, node_token=node_token, node_cert=node_cert
        ) as client:
            await client.update_inbound(inbound_id, {
                "server_name": sni,
                "destination": f"{sni}:443",
            })
        return {"ok": True, "sni": sni, "fingerprint": fingerprint}
    except Exception as exc:
        return {"ok": False, "error": str(exc), "sni": sni, "fingerprint": fingerprint}
