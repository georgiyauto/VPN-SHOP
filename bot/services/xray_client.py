"""Async client for the KawaVPN plain-Xray node agent."""
from __future__ import annotations

import logging
import ssl
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


class XrayClient:
    def __init__(
        self,
        node_url: str,
        username: str | None = None,
        password: str | None = None,
        node_path: str = "/",
        node_token: str | None = None,
        node_cert: str | None = None,
    ):
        base = (node_url or "http://127.0.0.1:8090").rstrip("/")
        path = (node_path or "/").strip("/")
        self.base = f"{base}/{path}" if path else base
        self.node_token = node_token or password or ""
        if node_cert:
            context = ssl.create_default_context()
            context.load_verify_locations(cadata=node_cert)
            self.verify: bool | ssl.SSLContext = context
        else:
            self.verify = True
        self._client: Optional[httpx.AsyncClient] = None

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.node_token}"}

    async def __aenter__(self):
        self._client = httpx.AsyncClient(timeout=30, follow_redirects=True, verify=self.verify)
        return self

    async def __aexit__(self, *_):
        if self._client:
            await self._client.aclose()
            self._client = None

    async def _request(self, method: str, path: str, **kwargs):
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=30, follow_redirects=True, verify=self.verify)
        try:
            response = await client.request(method, f"{self.base}{path}", headers=self._headers(), **kwargs)
            response.raise_for_status()
            return response.json()
        finally:
            if owns_client:
                await client.aclose()

    async def ping(self) -> bool:
        try:
            data = await self._request("GET", "/health")
            return bool(data.get("ok"))
        except Exception as exc:
            logger.debug("Xray node ping failed for %s: %s", self.base, exc)
            return False

    async def get_inbounds(self) -> list[dict]:
        return await self._request("GET", "/api/inbounds")

    async def create_inbound(self, payload: dict) -> dict:
        return await self._request("POST", "/api/inbounds", json=payload)

    async def update_inbound(self, inbound_ref: int | str, payload: dict) -> dict:
        return await self._request("PATCH", f"/api/inbounds/{inbound_ref}", json=payload)

    async def delete_inbound_config(self, inbound_ref: int | str) -> bool:
        data = await self._request("DELETE", f"/api/inbounds/{inbound_ref}")
        return bool(data.get("ok"))

    async def add_client(self, inbound_id: int, uuid: str, email: str, expire_ms: int = 0,
                         total_gb: float = 0, limit_ip: int = 0) -> bool:
        data = await self._request("POST", f"/api/inbounds/{inbound_id}/clients", json={
            "uuid": uuid,
            "email": email,
            "expire_ms": expire_ms,
            "total_gb": total_gb,
            "limit_ip": limit_ip,
        })
        return bool(data.get("ok"))

    async def update_client_expiry(self, inbound_id: int, uuid: str, expire_ms: int,
                                   email: str | None = None) -> bool:
        ref = email or uuid
        data = await self._request("PATCH", f"/api/inbounds/{inbound_id}/clients/{ref}", json={
            "expire_ms": expire_ms,
            "enabled": True,
        })
        return bool(data.get("ok"))

    async def delete_client(self, inbound_id: int, uuid: str, email: str | None = None) -> bool:
        ref = email or uuid
        data = await self._request("DELETE", f"/api/inbounds/{inbound_id}/clients/{ref}")
        return bool(data.get("ok"))

    async def get_client_stats(self, email: str) -> dict | None:
        return None

    async def get_client_configs(self, inbound_id: int, uuid: str, server_label: str,
                                 email: str | None = None) -> list[str]:
        ref = email or uuid
        return await self._request(
            "GET",
            f"/api/inbounds/{inbound_id}/clients/{ref}/links",
            params={"label": server_label},
        )
