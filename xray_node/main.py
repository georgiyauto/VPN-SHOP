"""Minimal authenticated control plane for a plain Xray-core process.

The agent owns the Xray JSON file, validates every candidate configuration with
``xray run -test`` and only then swaps the file and restarts the core.  It is
deliberately small: the VPN project remains the panel, Xray remains the data
plane, and no third-party panel is installed.
"""
from __future__ import annotations

import asyncio
import copy
import json
import os
import re
import secrets
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field, field_validator

CONFIG_PATH = Path(os.getenv("XRAY_CONFIG_PATH", "/etc/xray/config.json"))
STATE_PATH = Path(os.getenv("XRAY_STATE_PATH", "/var/lib/kawavpn-xray/state.json"))
XRAY_BINARY = os.getenv("XRAY_BINARY", "xray")
XRAY_RESTART_COMMAND = os.getenv("XRAY_RESTART_COMMAND", "supervisorctl restart xray")
NODE_TOKEN = os.getenv("XRAY_NODE_TOKEN", "")
PUBLIC_HOST = os.getenv("XRAY_PUBLIC_HOST", "")

app = FastAPI(title="KawaVPN Xray Node", version="1.0.0", docs_url=None, redoc_url=None)
config_lock = asyncio.Lock()
expiry_task: asyncio.Task | None = None


class InboundCreate(BaseModel):
    name: str = Field(min_length=2, max_length=64)
    preset: Literal["vless-reality", "vless-ws"] = "vless-reality"
    port: int = Field(default=443, ge=1, le=65535)
    listen: str = "0.0.0.0"
    server_name: str = "www.microsoft.com"
    destination: str | None = None
    path: str = "/vpn"

    @field_validator("name")
    @classmethod
    def safe_name(cls, value: str) -> str:
        value = re.sub(r"[^a-zA-Z0-9._-]+", "-", value.strip()).strip("-")
        if not value:
            raise ValueError("name must contain letters or numbers")
        return value


class InboundUpdate(BaseModel):
    port: int | None = Field(default=None, ge=1, le=65535)
    listen: str | None = None
    server_name: str | None = None
    destination: str | None = None
    path: str | None = None
    enabled: bool | None = None


class ClientCreate(BaseModel):
    uuid: str
    email: str = Field(min_length=1, max_length=128)
    expire_ms: int = 0
    total_gb: float = 0
    limit_ip: int = 0

    @field_validator("uuid")
    @classmethod
    def valid_uuid(cls, value: str) -> str:
        if not re.fullmatch(r"[0-9a-fA-F-]{32,36}", value):
            raise ValueError("invalid UUID")
        return value.lower()


class ClientUpdate(BaseModel):
    expire_ms: int = 0
    enabled: bool = True


def require_token(authorization: str | None = Header(default=None)) -> None:
    if not NODE_TOKEN:
        raise HTTPException(503, "XRAY_NODE_TOKEN is not configured")
    supplied = (authorization or "").removeprefix("Bearer ").strip()
    if not secrets.compare_digest(supplied, NODE_TOKEN):
        raise HTTPException(401, "invalid node token")


def _default_config() -> dict[str, Any]:
    return {
        "log": {"loglevel": "warning"},
        "api": {"tag": "api", "services": ["HandlerService", "LoggerService", "StatsService"]},
        "stats": {},
        "policy": {
            "levels": {"0": {"statsUserUplink": True, "statsUserDownlink": True}},
            "system": {"statsInboundUplink": True, "statsInboundDownlink": True},
        },
        "inbounds": [{
            "tag": "api",
            "listen": "127.0.0.1",
            "port": 10085,
            "protocol": "dokodemo-door",
            "settings": {"address": "127.0.0.1"},
        }],
        "outbounds": [
            {"tag": "direct", "protocol": "freedom"},
            {"tag": "blocked", "protocol": "blackhole"},
        ],
        "routing": {
            "domainStrategy": "AsIs",
            "rules": [{"type": "field", "inboundTag": ["api"], "outboundTag": "api"}],
        },
    }


def _read_json(path: Path, fallback: dict[str, Any]) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else copy.deepcopy(fallback)
    except (FileNotFoundError, json.JSONDecodeError):
        return copy.deepcopy(fallback)


def _read_config() -> dict[str, Any]:
    return _read_json(CONFIG_PATH, _default_config())


def _read_state() -> dict[str, Any]:
    return _read_json(STATE_PATH, {"inbounds": {}, "clients": {}})


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def _run(args: list[str], timeout: int = 15) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)


def _x25519(private_key: str | None = None) -> tuple[str, str]:
    args = [XRAY_BINARY, "x25519"]
    if private_key:
        args.extend(["-i", private_key])
    result = _run(args)
    if result.returncode:
        raise HTTPException(500, f"xray x25519 failed: {(result.stderr or result.stdout).strip()}")
    private_match = re.search(r"Private\s*Key:\s*(\S+)", result.stdout, re.I)
    public_match = re.search(
        r"(?:Public\s*Key|Password(?:\s*\(Public\s*Key\))?):\s*(\S+)",
        result.stdout,
        re.I,
    )
    private_value = private_key or (private_match.group(1) if private_match else "")
    public_value = public_match.group(1) if public_match else ""
    if not private_value or not public_value:
        raise HTTPException(500, "could not parse xray x25519 output")
    return private_value, public_value


def _user_inbounds(config: dict[str, Any]) -> list[dict[str, Any]]:
    return [item for item in config.get("inbounds", []) if item.get("tag") != "api"]


def _find_inbound(config: dict[str, Any], ref: str | int) -> dict[str, Any]:
    items = _user_inbounds(config)
    if isinstance(ref, int) or str(ref).isdigit():
        index = int(ref) - 1
        if 0 <= index < len(items):
            return items[index]
    for item in items:
        if item.get("tag") == str(ref):
            return item
    raise HTTPException(404, "inbound not found")


def _inbound_view(item: dict[str, Any], index: int, state: dict[str, Any]) -> dict[str, Any]:
    stream = item.get("streamSettings", {})
    reality = stream.get("realitySettings", {})
    ws = stream.get("wsSettings", {})
    clients = item.get("settings", {}).get("clients", [])
    meta = state.get("inbounds", {}).get(item.get("tag", ""), {})
    return {
        "id": index,
        "tag": item.get("tag"),
        "port": item.get("port"),
        "listen": item.get("listen", "0.0.0.0"),
        "protocol": item.get("protocol"),
        "network": stream.get("network", "raw"),
        "security": stream.get("security", "none"),
        "server_name": (reality.get("serverNames") or [""])[0],
        "destination": reality.get("target") or reality.get("dest"),
        "path": ws.get("path"),
        "client_count": len(clients),
        "enabled": meta.get("enabled", True),
    }


def _candidate_path(config: dict[str, Any]) -> str:
    fd, name = tempfile.mkstemp(prefix="xray-candidate-", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(config, handle, ensure_ascii=False, indent=2)
    return name


def _validate(config: dict[str, Any]) -> None:
    candidate = _candidate_path(config)
    try:
        result = _run([XRAY_BINARY, "run", "-test", "-config", candidate], timeout=20)
        if result.returncode:
            message = (result.stderr or result.stdout or "invalid Xray config").strip()
            raise HTTPException(422, message[-2000:])
    finally:
        os.unlink(candidate)


def _restart_xray() -> None:
    result = subprocess.run(
        XRAY_RESTART_COMMAND,
        shell=True,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode:
        raise HTTPException(500, (result.stderr or result.stdout or "Xray restart failed").strip())


async def _commit(config: dict[str, Any], state: dict[str, Any]) -> None:
    _validate(config)
    _atomic_json(CONFIG_PATH, config)
    _atomic_json(STATE_PATH, state)
    _restart_xray()


async def prune_expired_clients() -> int:
    """Remove expired clients from Xray and persist the pruned metadata."""
    async with config_lock:
        config, state = _read_config(), _read_state()
        now_ms = int(time.time() * 1000)
        removed = 0
        for inbound in _user_inbounds(config):
            tag = inbound.get("tag", "")
            metadata = state.get("clients", {}).get(tag, {})
            clients = inbound.setdefault("settings", {}).setdefault("clients", [])
            keep = []
            for client in clients:
                email = client.get("email", "")
                expires = int(metadata.get(email, {}).get("expire_ms") or 0)
                if expires and expires <= now_ms:
                    metadata.pop(email, None)
                    removed += 1
                else:
                    keep.append(client)
            inbound["settings"]["clients"] = keep
        if removed:
            await _commit(config, state)
        return removed


async def _expiry_loop() -> None:
    while True:
        try:
            await prune_expired_clients()
        except asyncio.CancelledError:
            raise
        except Exception:
            # Health endpoints remain available; the next pass retries cleanup.
            pass
        await asyncio.sleep(60)


def _make_inbound(data: InboundCreate, state: dict[str, Any]) -> dict[str, Any]:
    common = {
        "tag": data.name,
        "listen": data.listen,
        "port": data.port,
        "protocol": "vless",
        "settings": {"clients": [], "decryption": "none"},
        "sniffing": {"enabled": True, "destOverride": ["http", "tls", "quic"], "routeOnly": True},
    }
    if data.preset == "vless-reality":
        private_key, public_key = _x25519()
        short_id = secrets.token_hex(8)
        common["streamSettings"] = {
            "network": "raw",
            "security": "reality",
            "realitySettings": {
                "show": False,
                "target": data.destination or f"{data.server_name}:443",
                "xver": 0,
                "serverNames": [data.server_name],
                "privateKey": private_key,
                "shortIds": [short_id],
            },
        }
        state.setdefault("inbounds", {})[data.name] = {
            "preset": data.preset,
            "public_key": public_key,
            "enabled": True,
        }
    else:
        common["streamSettings"] = {
            "network": "ws",
            "security": "none",
            "wsSettings": {"path": data.path, "headers": {"Host": data.server_name}},
        }
        state.setdefault("inbounds", {})[data.name] = {"preset": data.preset, "enabled": True}
    return common


@app.on_event("startup")
async def startup() -> None:
    global expiry_task
    if not CONFIG_PATH.exists():
        _atomic_json(CONFIG_PATH, _default_config())
    if not STATE_PATH.exists():
        _atomic_json(STATE_PATH, {"inbounds": {}, "clients": {}})
    expiry_task = asyncio.create_task(_expiry_loop())


@app.on_event("shutdown")
async def shutdown() -> None:
    global expiry_task
    if expiry_task:
        expiry_task.cancel()
        try:
            await expiry_task
        except asyncio.CancelledError:
            pass
        expiry_task = None


@app.get("/health", dependencies=[Depends(require_token)])
async def health() -> dict[str, Any]:
    config = _read_config()
    try:
        _validate(config)
        valid = True
        error = None
    except HTTPException as exc:
        valid = False
        error = str(exc.detail)
    version = _run([XRAY_BINARY, "version"]).stdout.splitlines()
    return {
        "ok": valid,
        "core": version[0] if version else "xray",
        "inbounds": len(_user_inbounds(config)),
        "error": error,
    }


@app.get("/api/inbounds", dependencies=[Depends(require_token)])
async def list_inbounds() -> list[dict[str, Any]]:
    config, state = _read_config(), _read_state()
    return [_inbound_view(item, index, state) for index, item in enumerate(_user_inbounds(config), 1)]


@app.post("/api/inbounds", status_code=201, dependencies=[Depends(require_token)])
async def create_inbound(data: InboundCreate) -> dict[str, Any]:
    async with config_lock:
        config, state = _read_config(), _read_state()
        if any(item.get("tag") == data.name or item.get("port") == data.port for item in config.get("inbounds", [])):
            raise HTTPException(409, "inbound tag or port already exists")
        item = _make_inbound(data, state)
        config.setdefault("inbounds", []).append(item)
        await _commit(config, state)
        return _inbound_view(item, len(_user_inbounds(config)), state)


@app.patch("/api/inbounds/{ref}", dependencies=[Depends(require_token)])
async def update_inbound(ref: str, data: InboundUpdate) -> dict[str, Any]:
    async with config_lock:
        config, state = _read_config(), _read_state()
        item = _find_inbound(config, ref)
        tag = item["tag"]
        patch = data.model_dump(exclude_none=True)
        if "port" in patch:
            if any(x is not item and x.get("port") == patch["port"] for x in config.get("inbounds", [])):
                raise HTTPException(409, "port already exists")
            item["port"] = patch["port"]
        if "listen" in patch:
            item["listen"] = patch["listen"]
        stream = item.setdefault("streamSettings", {})
        reality = stream.get("realitySettings")
        if reality is not None:
            if "server_name" in patch:
                reality["serverNames"] = [patch["server_name"]]
            if "destination" in patch:
                reality["target"] = patch["destination"]
        if "path" in patch and stream.get("wsSettings") is not None:
            stream["wsSettings"]["path"] = patch["path"]
        if "enabled" in patch:
            state.setdefault("inbounds", {}).setdefault(tag, {})["enabled"] = patch["enabled"]
        await _commit(config, state)
        return _inbound_view(item, _user_inbounds(config).index(item) + 1, state)


@app.delete("/api/inbounds/{ref}", dependencies=[Depends(require_token)])
async def delete_inbound(ref: str) -> dict[str, bool]:
    async with config_lock:
        config, state = _read_config(), _read_state()
        item = _find_inbound(config, ref)
        config["inbounds"].remove(item)
        state.get("inbounds", {}).pop(item.get("tag"), None)
        state.get("clients", {}).pop(item.get("tag"), None)
        await _commit(config, state)
        return {"ok": True}


@app.post("/api/inbounds/{ref}/clients", dependencies=[Depends(require_token)])
async def add_client(ref: str, data: ClientCreate) -> dict[str, bool]:
    async with config_lock:
        config, state = _read_config(), _read_state()
        item = _find_inbound(config, ref)
        clients = item.setdefault("settings", {}).setdefault("clients", [])
        existing = next((client for client in clients if client.get("id") == data.uuid or client.get("email") == data.email), None)
        client = {"id": data.uuid, "email": data.email, "level": 0}
        if item.get("streamSettings", {}).get("security") == "reality":
            client["flow"] = "xtls-rprx-vision"
        if existing:
            existing.update(client)
        else:
            clients.append(client)
        state.setdefault("clients", {}).setdefault(item["tag"], {})[data.email] = data.model_dump()
        await _commit(config, state)
        return {"ok": True}


@app.patch("/api/inbounds/{ref}/clients/{client_ref}", dependencies=[Depends(require_token)])
async def update_client(ref: str, client_ref: str, data: ClientUpdate) -> dict[str, bool]:
    async with config_lock:
        config, state = _read_config(), _read_state()
        item = _find_inbound(config, ref)
        clients = item.setdefault("settings", {}).setdefault("clients", [])
        client = next((x for x in clients if x.get("id") == client_ref or x.get("email") == client_ref), None)
        if not client:
            raise HTTPException(404, "client not found")
        if not data.enabled:
            clients.remove(client)
        meta = state.setdefault("clients", {}).setdefault(item["tag"], {}).setdefault(client.get("email", client_ref), {})
        meta.update(data.model_dump())
        await _commit(config, state)
        return {"ok": True}


@app.delete("/api/inbounds/{ref}/clients/{client_ref}", dependencies=[Depends(require_token)])
async def delete_client(ref: str, client_ref: str) -> dict[str, bool]:
    async with config_lock:
        config, state = _read_config(), _read_state()
        item = _find_inbound(config, ref)
        clients = item.setdefault("settings", {}).setdefault("clients", [])
        client = next((x for x in clients if x.get("id") == client_ref or x.get("email") == client_ref), None)
        if client:
            clients.remove(client)
            state.get("clients", {}).get(item["tag"], {}).pop(client.get("email", client_ref), None)
            await _commit(config, state)
        return {"ok": True}


@app.get("/api/inbounds/{ref}/clients/{client_ref}/links", dependencies=[Depends(require_token)])
async def client_links(ref: str, client_ref: str, label: str = "Xray") -> list[str]:
    config, state = _read_config(), _read_state()
    item = _find_inbound(config, ref)
    client = next((x for x in item.get("settings", {}).get("clients", []) if x.get("id") == client_ref or x.get("email") == client_ref), None)
    if not client:
        raise HTTPException(404, "client not found")
    host = PUBLIC_HOST or os.getenv("BOT_DOMAIN", "").removeprefix("https://").removeprefix("http://").split(":")[0]
    if not host:
        raise HTTPException(422, "XRAY_PUBLIC_HOST is not configured")
    link_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
    stream = item.get("streamSettings", {})
    network = stream.get("network", "raw")
    security = stream.get("security", "none")
    params = [f"type={quote(network)}", f"security={quote(security)}", "encryption=none"]
    if security == "reality":
        reality = stream.get("realitySettings", {})
        meta = state.get("inbounds", {}).get(item["tag"], {})
        public_key = meta.get("public_key")
        if not public_key:
            _, public_key = _x25519(reality.get("privateKey"))
        params.extend([
            f"sni={quote((reality.get('serverNames') or [''])[0])}",
            f"pbk={quote(public_key)}",
            f"sid={quote((reality.get('shortIds') or [''])[0])}",
            "fp=chrome",
            "flow=xtls-rprx-vision",
        ])
    elif network == "ws":
        ws = stream.get("wsSettings", {})
        ws_host = ws.get("headers", {}).get("Host", host)
        params.extend([f"path={quote(ws.get('path', '/'))}", f"host={quote(ws_host)}"])
    return [f"vless://{client['id']}@{link_host}:{item['port']}?{'&'.join(params)}#{quote(label)}"]
