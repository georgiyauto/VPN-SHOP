# KawaVPN Control

Telegram VPN service and admin panel backed by plain
[Xray-core](https://github.com/XTLS/Xray-core). The project does not install or
depend on a third-party Xray panel.

## Architecture

- **Admin panel** manages users, plans, payments, nodes and Xray inbounds.
- **Xray node-agent** owns the JSON config on each node. Every candidate config
  is checked with `xray run -test` before an atomic file swap and restart.
- Remote node APIs use HTTPS with a pinned self-signed certificate; the local
  node API is bound to host loopback only.
- **Xray-core** is the only data-plane process. The default inbound is VLESS
  Reality on TCP/443.
- **PostgreSQL** stores application data; **Redis** stores queues/FSM state.
- **Subscription service** builds `vless://` links from live node profiles.

The node model follows the useful Remnawave concept of a control plane and
separate Xray nodes, while remaining a small project-specific implementation.

## Requirements

- Ubuntu 22.04/24.04 or Debian 12
- root or sudo access
- at least 2 CPU, 2 GB RAM and 10 GB free disk
- a domain name or public IPv4 address
- TCP ports `1414`, `8433`, `443`; UDP `443`

## Install

```bash
chmod +x setup.sh
sudo ./setup.sh
```

The installer adds Docker when needed, generates all database/Redis/admin/node
secrets, builds the project, installs Xray in the image, creates the first
VLESS Reality inbound and performs health/config checks.

Non-interactive example:

```bash
sudo BOT_DOMAIN=vpn.example.com \
  BOT_TOKEN=123456:telegram-token \
  ADMIN_IDS=123456789 \
  ADMIN_PASSWORD='use-a-long-password' \
  ./setup.sh --non-interactive
```

## Fresh database

The source archive contains no database, `.env`, backup or customer data. To
explicitly erase an existing Docker deployment and recreate PostgreSQL and
Xray state from scratch:

```bash
sudo ./setup.sh --clean
```

`--clean` permanently removes the application database and Xray configuration
volumes. Without it, setup keeps existing volumes.

## Inbounds

Open `http://YOUR_HOST:1414/admin`, then select **Инбаунды**:

1. Select a ready node.
2. Choose `VLESS Reality` or `VLESS WebSocket`.
3. Set a unique tag and port.
4. For Reality, set SNI and target; for WebSocket, set Host and path.
5. Click **Проверить и создать**.

The node rejects duplicate ports/tags and invalid Xray JSON. Users are added to
the primary inbound automatically when subscriptions are activated.

## Add a node

In **Ноды**, choose one of two paths:

- **Install over SSH**: provide IP, SSH account and password/private key. The
  panel installs official Xray-core, the node-agent, systemd unit and a default
  VLESS Reality inbound.
- **Attach ready node**: provide `https://NODE_IP:8090` and its Bearer token.
  For a self-signed HTTPS node, also paste its CA/server certificate.

SSH provisioning restricts port `8090` to the panel's source IP through UFW.
The token and pinned certificate are stored in PostgreSQL and are never
returned by list APIs.

## Operations

```bash
docker compose ps
docker compose logs -f vpnbot
docker compose exec vpnbot supervisorctl status
docker compose exec vpnbot xray run -test -config /etc/xray/config.json
curl -H "Authorization: Bearer $XRAY_NODE_TOKEN" http://127.0.0.1:8090/health
```

Important files:

- `setup.sh`: complete host installation and verification
- `xray_node/main.py`: authenticated Xray config manager
- `bot/services/xray_client.py`: panel-to-node API client
- `admin_panel/main.py`: control-plane API
- `admin_panel/templates/dashboard.html`: operator UI
- `docker-compose.yml`: persistent PostgreSQL, TLS and Xray volumes

## Security notes

- Keep `.env` mode `600`; do not commit it.
- Restrict `8090/tcp` by firewall to the control-plane IP.
- Keep certificate pinning enabled for self-signed remote nodes.
- Use SSH keys for remote provisioning.
- Back up PostgreSQL and `/etc/xray` before upgrades.
- Rotate the node token if it is exposed, then update the node record.
