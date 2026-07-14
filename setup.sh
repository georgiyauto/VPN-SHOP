#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_PATH="$0"
SCRIPT_DIR="${SCRIPT_PATH%/*}"
[ "$SCRIPT_DIR" = "$SCRIPT_PATH" ] && SCRIPT_DIR="."
ROOT_DIR="$(cd "$SCRIPT_DIR"; pwd)"
cd "$ROOT_DIR"

CLEAN=0
NON_INTERACTIVE=0
DOMAIN="${BOT_DOMAIN:-}"
BOT_TOKEN_VALUE="${BOT_TOKEN:-}"
ADMIN_IDS_VALUE="${ADMIN_IDS:-}"

usage() {
  cat <<EOF
Usage: sudo ./setup.sh [options]

Options:
  --clean              Remove the existing PostgreSQL/Xray Docker volumes.
  --non-interactive    Read configuration from environment variables.
  --domain HOST        Public domain or server IP used in VPN links.
  -h, --help           Show this help.

Environment for non-interactive mode:
  BOT_DOMAIN, BOT_TOKEN, ADMIN_IDS, ADMIN_USERNAME, ADMIN_PASSWORD,
  LETSENCRYPT_EMAIL, PANEL_PORT, SUB_PORT
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --clean) CLEAN=1 ;;
    --non-interactive) NON_INTERACTIVE=1 ;;
    --domain)
      [ "$#" -ge 2 ] || { echo "--domain requires a value" >&2; exit 2; }
      DOMAIN="$2"
      shift
      ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage; exit 2 ;;
  esac
  shift
done

if [ "$(id -u)" -ne 0 ]; then
  echo "Run as root: sudo ./setup.sh" >&2
  exit 1
fi

if [ ! -f docker-compose.yml ] || [ ! -f Dockerfile ]; then
  echo "Run setup.sh from the project directory." >&2
  exit 1
fi

if [ "$NON_INTERACTIVE" -eq 0 ]; then
  read -r -p "Public domain or server IP: " DOMAIN
  read -r -p "Telegram bot token (can be empty for setup wizard): " BOT_TOKEN_VALUE
  read -r -p "Telegram admin IDs, comma-separated: " ADMIN_IDS_VALUE
fi

DOMAIN="${DOMAIN#https://}"
DOMAIN="${DOMAIN#http://}"
DOMAIN="${DOMAIN%%/*}"
[ -n "$DOMAIN" ] || { echo "A public domain or IP is required." >&2; exit 1; }
case "$DOMAIN" in
  *[!0-9.]*) ADMIN_SCHEME="https" ;;
  *) ADMIN_SCHEME="http" ;;
esac

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq ca-certificates curl openssl

if ! command -v docker >/dev/null 2>&1; then
  curl -fsSL https://get.docker.com -o /tmp/get-docker.sh
  sh /tmp/get-docker.sh
  rm -f /tmp/get-docker.sh
fi
if ! docker compose version >/dev/null 2>&1; then
  apt-get install -y -qq docker-compose-plugin
fi
docker compose version >/dev/null

PG_PASS="$(openssl rand -hex 24)"
REDIS_PASS="$(openssl rand -hex 24)"
SECRET_KEY="$(openssl rand -hex 32)"
XRAY_TOKEN="$(openssl rand -hex 32)"
ADMIN_USER="${ADMIN_USERNAME:-admin}"
if [ -n "${ADMIN_PASSWORD:-}" ]; then
  ADMIN_PASS="$ADMIN_PASSWORD"
else
  ADMIN_PASS="$(openssl rand -base64 24 | tr -d '/+=' | head -c 24)"
fi
PANEL_PORT_VALUE="${PANEL_PORT:-1414}"
SUB_PORT_VALUE="${SUB_PORT:-8433}"
LE_EMAIL="${LETSENCRYPT_EMAIL:-admin@${DOMAIN}}"

cat > .env <<EOF
BOT_TOKEN=${BOT_TOKEN_VALUE}
ADMIN_IDS=${ADMIN_IDS_VALUE}
BOT_USERNAME=${BOT_USERNAME:-}
POSTGRES_DB=vpnbot
POSTGRES_USER=vpnbot
POSTGRES_PASSWORD=${PG_PASS}
DATABASE_URL=postgresql+asyncpg://vpnbot:${PG_PASS}@127.0.0.1:5432/vpnbot
REDIS_URL=redis://:${REDIS_PASS}@127.0.0.1:6379/0
REDIS_PASSWORD=${REDIS_PASS}
BOT_DOMAIN=${DOMAIN}
LETSENCRYPT_EMAIL=${LE_EMAIL}
PANEL_PORT=${PANEL_PORT_VALUE}
SUB_PORT=${SUB_PORT_VALUE}
ADMIN_USERNAME=${ADMIN_USER}
ADMIN_PASSWORD=${ADMIN_PASS}
SECRET_KEY=${SECRET_KEY}
XRAY_NODE_TOKEN=${XRAY_TOKEN}
XRAY_PUBLIC_HOST=${DOMAIN}
XRAY_CONFIG_PATH=/etc/xray/config.json
XRAY_STATE_PATH=/var/lib/kawavpn-xray/state.json
XRAY_RESTART_COMMAND=supervisorctl restart xray
HELEKET_API_KEY=
HELEKET_SHOP_ID=
HELEKET_SECRET=
CRYPTOPAY_TOKEN=
CARD_LINK_URL=
STATUS_CHANNEL_ID=
SUPPORT_CHAT_ID=
SUPPORT_USERNAME=
MINIAPP_AUTH_SKIP=0
GOOGLE_CLIENT_ID=
EOF
chmod 600 .env
mkdir -p backups logs

if [ "$CLEAN" -eq 1 ]; then
  echo "Removing the old application database and Xray state..."
  docker compose down -v --remove-orphans || true
else
  docker compose down --remove-orphans || true
fi

docker compose build --pull
docker compose up -d

echo "Waiting for Xray node..."
for _ in $(seq 1 60); do
  if curl -fsS -H "Authorization: Bearer ${XRAY_TOKEN}" http://127.0.0.1:8090/health >/dev/null 2>&1; then
    break
  fi
  sleep 2
done

if ! curl -fsS -H "Authorization: Bearer ${XRAY_TOKEN}" http://127.0.0.1:8090/health >/dev/null; then
  docker compose logs --tail=120
  echo "Xray node failed its health check." >&2
  exit 1
fi

INBOUND_COUNT="$(curl -fsS -H "Authorization: Bearer ${XRAY_TOKEN}" http://127.0.0.1:8090/api/inbounds | python3 -c 'import json,sys; print(len(json.load(sys.stdin)))')"
if [ "$INBOUND_COUNT" -eq 0 ]; then
  curl -fsS -X POST \
    -H "Authorization: Bearer ${XRAY_TOKEN}" \
    -H "Content-Type: application/json" \
    -d '{"name":"vless-reality","preset":"vless-reality","port":443,"server_name":"www.microsoft.com"}' \
    http://127.0.0.1:8090/api/inbounds >/dev/null
fi

docker compose exec -T vpnbot /usr/local/bin/xray run -test -config /etc/xray/config.json
docker compose exec -T vpnbot curl -fsS -u "${ADMIN_USER}:${ADMIN_PASS}" http://127.0.0.1:8001/api/servers >/dev/null

cat <<EOF

Installation complete.
Admin panel: ${ADMIN_SCHEME}://${DOMAIN}:${PANEL_PORT_VALUE}/admin
Username:    ${ADMIN_USER}
Password:    ${ADMIN_PASS}
Xray:        ${DOMAIN}:443 (VLESS Reality)

Credentials are stored in ${ROOT_DIR}/.env (mode 600).
EOF
