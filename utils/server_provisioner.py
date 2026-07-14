"""Provision plain Xray nodes over SSH.

No web panel is installed on a node.  The provisioner installs official
Xray-core plus the small KawaVPN node agent and registers the node in the main
panel.  Blocking SSH work is moved to a worker thread so FastAPI and the bot do
not stall while a server is being prepared.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import secrets
from pathlib import Path
from typing import Callable

import paramiko

from bot.services.xray_client import XrayClient
from db.database import AsyncSessionLocal
from db.models import Server

logger = logging.getLogger(__name__)
NODE_PORT = 8090
XRAY_VERSION = "v26.3.27"
XRAY_LINUX64_SHA256 = "23cd9af937744d97776ee35ecad4972cf4b2109d1e0fe6be9930467608f7c8ae"
AGENT_SOURCE = Path(__file__).resolve().parents[1] / "xray_node" / "main.py"


async def _save_status(server_id: int, status: str, message: str) -> None:
    async with AsyncSessionLocal() as session:
        server = await session.get(Server, server_id)
        if server:
            server.install_status = status
            server.install_log = message[-12000:]
            await session.commit()


async def _append_status(server_id: int, message: str, callback: Callable | None = None) -> None:
    async with AsyncSessionLocal() as session:
        server = await session.get(Server, server_id)
        if not server:
            return
        server.install_log = ((server.install_log or "") + message.rstrip() + "\n")[-12000:]
        await session.commit()
    if callback:
        result = callback(message)
        if asyncio.iscoroutine(result):
            await result


def _connect(server: Server) -> paramiko.SSHClient:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    kwargs = {
        "hostname": server.ssh_host,
        "port": server.ssh_port or 22,
        "username": server.ssh_user or "root",
        "timeout": 20,
        "banner_timeout": 20,
        "auth_timeout": 20,
    }
    if server.ssh_key:
        from io import StringIO
        key_data = StringIO(server.ssh_key)
        key = None
        for key_type in (paramiko.Ed25519Key, paramiko.RSAKey, paramiko.ECDSAKey):
            try:
                key_data.seek(0)
                key = key_type.from_private_key(key_data)
                break
            except Exception:
                continue
        if not key:
            raise RuntimeError("Не удалось прочитать SSH ключ")
        kwargs["pkey"] = key
    else:
        kwargs["password"] = server.ssh_password
    client.connect(**kwargs)
    return client


def _exec(ssh: paramiko.SSHClient, command: str, timeout: int = 240) -> str:
    stdin, stdout, stderr = ssh.exec_command(command, timeout=timeout)
    code = stdout.channel.recv_exit_status()
    output = stdout.read().decode("utf-8", "replace")
    error = stderr.read().decode("utf-8", "replace")
    if code:
        raise RuntimeError(f"Команда завершилась с кодом {code}: {error or output}")
    return output


def _install_sync(server: Server, token: str) -> str:
    host = (server.ssh_host or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9.:-]+", host):
        raise RuntimeError("Некорректный SSH host")
    try:
        ipaddress.ip_address(host)
        subject_alt_name = f"IP:{host}"
    except ValueError:
        subject_alt_name = f"DNS:{host}"
    ssh = _connect(server)
    try:
        _exec(ssh, "export DEBIAN_FRONTEND=noninteractive; apt-get update -qq && apt-get install -y -qq curl unzip openssl ca-certificates python3 python3-venv")
        xray_url = f"https://github.com/XTLS/Xray-core/releases/download/{XRAY_VERSION}/Xray-linux-64.zip"
        _exec(ssh, (
            "if ! command -v xray >/dev/null; then "
            f"curl -fsSL {xray_url} -o /tmp/xray.zip && "
            f"echo '{XRAY_LINUX64_SHA256}  /tmp/xray.zip' | sha256sum -c - && "
            "install -d -m 0755 /usr/local/share/xray && "
            "unzip -jo /tmp/xray.zip xray -d /usr/local/bin && "
            "unzip -jo /tmp/xray.zip geoip.dat geosite.dat -d /usr/local/share/xray && "
            "chmod 0755 /usr/local/bin/xray && rm -f /tmp/xray.zip; "
            "fi"
        ))
        _exec(ssh, "install -d -m 0750 /opt/kawavpn-node /var/lib/kawavpn-xray /usr/local/etc/xray")
        _exec(ssh, (
            "if [ ! -s /etc/kawavpn-node.key ] || [ ! -s /etc/kawavpn-node.crt ]; then "
            "openssl req -x509 -newkey rsa:3072 -sha256 -nodes -days 825 "
            "-keyout /etc/kawavpn-node.key -out /etc/kawavpn-node.crt "
            f"-subj '/CN={host}' -addext 'subjectAltName={subject_alt_name}'; "
            "chmod 0600 /etc/kawavpn-node.key; chmod 0644 /etc/kawavpn-node.crt; fi"
        ))
        with ssh.open_sftp() as sftp:
            sftp.put(str(AGENT_SOURCE), "/opt/kawavpn-node/main.py")
        _exec(ssh, "python3 -m venv /opt/kawavpn-node/venv && /opt/kawavpn-node/venv/bin/pip install --disable-pip-version-check -q fastapi==0.111.0 uvicorn==0.29.0 pydantic==2.7.1")
        env_lines = [
            f"XRAY_NODE_TOKEN={token}",
            f"XRAY_PUBLIC_HOST={server.ssh_host}",
            "XRAY_CONFIG_PATH=/usr/local/etc/xray/config.json",
            "XRAY_STATE_PATH=/var/lib/kawavpn-xray/state.json",
            "XRAY_BINARY=/usr/local/bin/xray",
            "XRAY_RESTART_COMMAND=systemctl restart xray",
        ]
        env_content = "\n".join(env_lines) + "\n"
        service_content = """[Unit]
Description=KawaVPN Xray node agent
After=network-online.target xray.service
Wants=network-online.target

[Service]
Type=simple
User=root
EnvironmentFile=/etc/kawavpn-node.env
WorkingDirectory=/opt/kawavpn-node
ExecStart=/opt/kawavpn-node/venv/bin/uvicorn main:app --host 0.0.0.0 --port 8090 --ssl-keyfile /etc/kawavpn-node.key --ssl-certfile /etc/kawavpn-node.crt
Restart=always
RestartSec=3
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
"""
        xray_service_content = """[Unit]
Description=Xray Service
After=network.target

[Service]
Type=simple
ExecStart=/usr/local/bin/xray run -config /usr/local/etc/xray/config.json
Restart=on-failure
RestartSec=3
LimitNOFILE=1048576

[Install]
WantedBy=multi-user.target
"""
        for remote_path, content, mode in (
            ("/etc/kawavpn-node.env", env_content, 0o600),
            ("/etc/systemd/system/kawavpn-node.service", service_content, 0o644),
            ("/etc/systemd/system/xray.service", xray_service_content, 0o644),
        ):
            with ssh.open_sftp() as sftp:
                with sftp.file(remote_path, "w") as handle:
                    handle.write(content)
                sftp.chmod(remote_path, mode)
        _exec(ssh, "systemctl daemon-reload && systemctl enable --now xray kawavpn-node")
        _exec(ssh, (
            "if command -v ufw >/dev/null 2>&1; then "
            "PANEL_IP=$(printf '%s' \"$SSH_CLIENT\" | awk '{print $1}'); "
            f"[ -n \"$PANEL_IP\" ] && ufw allow from \"$PANEL_IP\" to any port {NODE_PORT} proto tcp >/dev/null 2>&1 || true; "
            "fi"
        ))
        with ssh.open_sftp() as sftp:
            with sftp.file("/etc/kawavpn-node.crt", "r") as handle:
                return handle.read().decode("utf-8")
    finally:
        ssh.close()


async def provision_vpn_server(server_id: int, log_callback=None) -> dict:
    async with AsyncSessionLocal() as session:
        server = await session.get(Server, server_id)
        if not server:
            return {"ok": False, "error": "Сервер не найден"}
        snapshot = Server()
        for name in ("ssh_host", "ssh_port", "ssh_user", "ssh_password", "ssh_key"):
            setattr(snapshot, name, getattr(server, name))
        token = server.node_token or secrets.token_urlsafe(36)
        url_host = f"[{server.ssh_host}]" if ":" in server.ssh_host else server.ssh_host
        node_url = f"https://{url_host}:{NODE_PORT}"
        await _save_status(server_id, "installing", "Подключение по SSH...\n")

    try:
        await _append_status(server_id, "Устанавливаю Xray-core и node-agent", log_callback)
        node_cert = await asyncio.to_thread(_install_sync, snapshot, token)
        await _append_status(server_id, "Службы установлены, проверяю API", log_callback)
        client = XrayClient(node_url, node_token=token, node_cert=node_cert)
        for _ in range(20):
            if await client.ping():
                break
            await asyncio.sleep(1.5)
        else:
            raise RuntimeError("Node-agent не ответил после установки")

        inbounds = await client.get_inbounds()
        if not inbounds:
            await client.create_inbound({
                "name": "vless-reality",
                "preset": "vless-reality",
                "port": 443,
                "server_name": "www.microsoft.com",
            })
            await _append_status(server_id, "Создан VLESS Reality inbound на порту 443", log_callback)

        async with AsyncSessionLocal() as session:
            current = await session.get(Server, server_id)
            current.node_url = node_url
            current.node_token = token
            current.node_cert = node_cert
            current.node_path = "/"
            current.inbound_id = 1
            current.install_status = "ready"
            current.install_log = (current.install_log or "") + "Xray node готов\n"
            current.is_online = True
            await session.commit()
        return {"ok": True, "node_url": node_url}
    except Exception as exc:
        logger.exception("Xray node provisioning failed")
        await _save_status(server_id, "error", f"Ошибка установки: {exc}")
        return {"ok": False, "error": str(exc)}


async def provision_ready_server(
    server_id: int, node_url: str, node_token: str, node_cert: str | None = None
) -> dict:
    client = XrayClient(node_url, node_token=node_token, node_cert=node_cert)
    if not await client.ping():
        await _save_status(server_id, "error", "Node-agent недоступен или токен неверен")
        return {"ok": False, "error": "Node-agent недоступен или токен неверен"}
    inbounds = await client.get_inbounds()
    async with AsyncSessionLocal() as session:
        server = await session.get(Server, server_id)
        server.node_url = node_url.rstrip("/")
        server.node_token = node_token
        server.node_cert = node_cert
        server.inbound_id = inbounds[0]["id"] if inbounds else 1
        server.install_status = "ready"
        server.install_log = f"Подключено к Xray node: {node_url}\nИнбаундов: {len(inbounds)}"
        server.is_online = True
        await session.commit()
    return {"ok": True, "node_url": node_url}


async def get_install_status(server_id: int) -> dict:
    async with AsyncSessionLocal() as session:
        server = await session.get(Server, server_id)
        if not server:
            return {"status": "not_found", "log": ""}
        return {"status": server.install_status, "log": server.install_log or ""}
