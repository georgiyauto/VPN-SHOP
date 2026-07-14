#!/usr/bin/env bash
set -euo pipefail

GRN='\033[0;32m'; YEL='\033[1;33m'
CYN='\033[0;36m'; RST='\033[0m'
ok()  { echo -e "${GRN}✔ $*${RST}"; }
inf() { echo -e "${CYN}→ $*${RST}"; }
warn(){ echo -e "${YEL}⚠ $*${RST}"; }

if [[ -f /app/.env ]]; then
    set -a; source /app/.env; set +a
fi

# ВАЖНО: создаём логи с правами 777 ДО всего остального (пока root)
mkdir -p /app/logs /app/backups /tmp/pg_logs
chmod 777 /app/logs /app/backups /tmp/pg_logs

PG_VERSION=$(ls /usr/lib/postgresql/ | sort -V | tail -1)
export PG_VERSION
inf "PostgreSQL version: $PG_VERSION"

PG_DATA="/var/lib/postgresql/data"
mkdir -p "$PG_DATA"
chown -R postgres:postgres "$PG_DATA"
chmod 700 "$PG_DATA"

if [[ ! -f "$PG_DATA/PG_VERSION" ]]; then
    inf "Initialising PostgreSQL cluster..."
    gosu postgres /usr/lib/postgresql/$PG_VERSION/bin/initdb \
        -D "$PG_DATA" --encoding=UTF8 --locale=C \
        --username=postgres -A trust \
        > /tmp/pg_logs/pg_init.log 2>&1
    cp /tmp/pg_logs/pg_init.log /app/logs/pg_init.log 2>/dev/null || true
    echo "host all all 127.0.0.1/32 md5" >> "$PG_DATA/pg_hba.conf"
    ok "PostgreSQL cluster initialised"
fi

inf "Starting PostgreSQL for DB setup..."
gosu postgres /usr/lib/postgresql/$PG_VERSION/bin/pg_ctl \
    -D "$PG_DATA" -w start -l /tmp/pg_logs/pg_setup.log

gosu postgres psql -tc "SELECT 1 FROM pg_roles WHERE rolname='${POSTGRES_USER:-vpnbot}'" | grep -q 1 || \
    gosu postgres psql -c "CREATE USER ${POSTGRES_USER:-vpnbot} WITH PASSWORD '${POSTGRES_PASSWORD:-vpnbot_secret}';"
gosu postgres psql -tc "SELECT 1 FROM pg_database WHERE datname='${POSTGRES_DB:-vpnbot}'" | grep -q 1 || \
    gosu postgres psql -c "CREATE DATABASE ${POSTGRES_DB:-vpnbot} OWNER ${POSTGRES_USER:-vpnbot};"
gosu postgres psql -c "ALTER USER ${POSTGRES_USER:-vpnbot} WITH PASSWORD '${POSTGRES_PASSWORD:-vpnbot_secret}';" >/dev/null 2>&1 || true
ok "PostgreSQL database ready"

gosu postgres /usr/lib/postgresql/$PG_VERSION/bin/pg_ctl -D "$PG_DATA" -w stop

inf "Running DB migrations..."
gosu postgres /usr/lib/postgresql/$PG_VERSION/bin/pg_ctl \
    -D "$PG_DATA" -w start -l /tmp/pg_logs/pg_setup.log
cd /app
python -c "
import asyncio
from db.database import init_db
asyncio.run(init_db())
print('Tables OK')
" 2>&1 | tail -5 || true
gosu postgres /usr/lib/postgresql/$PG_VERSION/bin/pg_ctl -D "$PG_DATA" -w stop
ok "DB tables ready"

DOMAIN="${BOT_DOMAIN:-}"
DOMAIN="${DOMAIN#https://}"
DOMAIN="${DOMAIN#http://}"
DOMAIN="${DOMAIN%%/*}"
DOMAIN="${DOMAIN%%:*}"   # убираем порт если есть (например kawa-vpn.ddns.net:8433)

PANEL_PORT="${PANEL_PORT:-1414}"
SUB_PORT="${SUB_PORT:-8433}"

if [[ -z "$DOMAIN" || "$DOMAIN" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    USE_SSL=false
    inf "No domain — HTTP-only mode on ports ${PANEL_PORT}/${SUB_PORT}"
else
    USE_SSL=true
    inf "Domain: $DOMAIN — SSL on ports ${PANEL_PORT}/${SUB_PORT}"
fi

mkdir -p /var/www/certbot

if [[ "$USE_SSL" == "true" ]]; then
    cat > /etc/nginx/sites-available/vpnbot << NGINX_TMP
server {
    listen 80;
    server_name ${DOMAIN};
    location /.well-known/acme-challenge/ { root /var/www/certbot; }
    location / { return 200 'ok'; }
}
NGINX_TMP
    ln -sf /etc/nginx/sites-available/vpnbot /etc/nginx/sites-enabled/vpnbot

    if ss -tlnp 2>/dev/null | grep -q ':80 '; then
        CERTBOT_MODE="standalone"
    else
        CERTBOT_MODE="nginx"
        nginx -t && nginx
    fi

    CERT_EMAIL="${LETSENCRYPT_EMAIL:-admin@${DOMAIN}}"
    inf "Obtaining SSL certificate for $DOMAIN..."

    if [[ "$CERTBOT_MODE" == "nginx" ]]; then
        CERTBOT_CMD="certbot certonly --nginx --non-interactive --agree-tos --email $CERT_EMAIL -d $DOMAIN"
    else
        nginx -s stop 2>/dev/null || true
        sleep 1
        CERTBOT_CMD="certbot certonly --standalone --non-interactive --agree-tos --email $CERT_EMAIL -d $DOMAIN"
    fi

    if $CERTBOT_CMD 2>&1 | tee /app/logs/certbot_init.log; then
        ok "SSL certificate obtained"
        nginx -s stop 2>/dev/null || true
        sleep 1

        cat > /etc/nginx/sites-available/vpnbot << NGINX_SSL
server {
    listen ${SUB_PORT} ssl http2;
    server_name ${DOMAIN};
    ssl_certificate     /etc/letsencrypt/live/${DOMAIN}/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/${DOMAIN}/privkey.pem;
    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_ciphers         HIGH:!aNULL:!MD5;
    add_header Strict-Transport-Security "max-age=31536000" always;
    location /sub/       { proxy_pass http://127.0.0.1:8000; proxy_set_header Host \$host; proxy_set_header X-Real-IP \$remote_addr; proxy_set_header X-Forwarded-Proto https; }
    location /heleket/   { proxy_pass http://127.0.0.1:8000; proxy_set_header Host \$host; }
    location /sbp/       { proxy_pass http://127.0.0.1:8000; proxy_set_header Host \$host; proxy_set_header X-Real-IP \$remote_addr; }
    location /cryptopay/ { proxy_pass http://127.0.0.1:8000; proxy_set_header Host \$host; }
    location /setup      { proxy_pass http://127.0.0.1:8002; proxy_set_header Host \$host; proxy_set_header X-Real-IP \$remote_addr; proxy_set_header X-Forwarded-Proto https; }
    location /health     { proxy_pass http://127.0.0.1:8000; }
}
server {
    listen ${PANEL_PORT} ssl http2;
    server_name ${DOMAIN};
    ssl_certificate     /etc/letsencrypt/live/${DOMAIN}/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/${DOMAIN}/privkey.pem;
    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_ciphers         HIGH:!aNULL:!MD5;
    add_header Strict-Transport-Security "max-age=31536000" always;
    location /admin { proxy_pass http://127.0.0.1:8001; proxy_set_header Host \$host; proxy_set_header X-Real-IP \$remote_addr; proxy_set_header X-Forwarded-Proto https; }
    location /setup { proxy_pass http://127.0.0.1:8002; proxy_set_header Host \$host; proxy_set_header X-Real-IP \$remote_addr; }
    location /api/web/ { proxy_pass http://127.0.0.1:8002; proxy_set_header Host \$host; proxy_set_header X-Real-IP \$remote_addr; proxy_set_header X-Forwarded-Proto https; }
    location /api/miniapp/ { proxy_pass http://127.0.0.1:8002; proxy_set_header Host \$host; proxy_set_header X-Real-IP \$remote_addr; }
    location /api/  { proxy_pass http://127.0.0.1:8001; proxy_set_header Host \$host; proxy_set_header X-Real-IP \$remote_addr; }
    location /      { proxy_pass http://127.0.0.1:8002; proxy_set_header Host \$host; proxy_set_header X-Real-IP \$remote_addr; proxy_set_header X-Forwarded-Proto https; }
}
NGINX_SSL
        ok "Nginx HTTPS config on ports ${PANEL_PORT}/${SUB_PORT}"
    else
        warn "SSL cert failed — falling back to HTTP on custom ports"
        USE_SSL=false
    fi
fi

if [[ "$USE_SSL" == "false" ]]; then
    cat > /etc/nginx/sites-available/vpnbot << NGINX_HTTP
server {
    listen ${SUB_PORT};
    server_name _;
    location /sub/       { proxy_pass http://127.0.0.1:8000; proxy_set_header Host \$host; proxy_set_header X-Real-IP \$remote_addr; }
    location /heleket/   { proxy_pass http://127.0.0.1:8000; proxy_set_header Host \$host; }
    location /sbp/       { proxy_pass http://127.0.0.1:8000; proxy_set_header Host \$host; proxy_set_header X-Real-IP \$remote_addr; }
    location /cryptopay/ { proxy_pass http://127.0.0.1:8000; proxy_set_header Host \$host; }
    location /setup      { proxy_pass http://127.0.0.1:8002; proxy_set_header Host \$host; proxy_set_header X-Real-IP \$remote_addr; proxy_set_header X-Forwarded-Proto https; }
    location /health     { proxy_pass http://127.0.0.1:8000; }
}
server {
    listen ${PANEL_PORT};
    server_name _;
    location /admin { proxy_pass http://127.0.0.1:8001; proxy_set_header Host \$host; proxy_set_header X-Real-IP \$remote_addr; }
    location /setup { proxy_pass http://127.0.0.1:8002; proxy_set_header Host \$host; }
    location /api/web/ { proxy_pass http://127.0.0.1:8002; proxy_set_header Host \$host; proxy_set_header X-Real-IP \$remote_addr; }
    location /api/miniapp/ { proxy_pass http://127.0.0.1:8002; proxy_set_header Host \$host; proxy_set_header X-Real-IP \$remote_addr; }
    location /api/  { proxy_pass http://127.0.0.1:8001; proxy_set_header Host \$host; }
    location /      { proxy_pass http://127.0.0.1:8002; proxy_set_header Host \$host; proxy_set_header X-Real-IP \$remote_addr; }
}
NGINX_HTTP
fi

ln -sf /etc/nginx/sites-available/vpnbot /etc/nginx/sites-enabled/vpnbot

if [[ "$USE_SSL" == "true" ]]; then
    SETUP_URL="https://${DOMAIN}:${PANEL_PORT}/setup"
    PANEL_URL="https://${DOMAIN}:${PANEL_PORT}/admin"
    SUB_URL="https://${DOMAIN}:${SUB_PORT}/sub/"
else
    IP=$(curl -4 -s --max-time 6 ifconfig.me 2>/dev/null || hostname -I | awk '{print $1}')
    SETUP_URL="http://${IP}:${PANEL_PORT}/setup"
    PANEL_URL="http://${IP}:${PANEL_PORT}/admin"
    SUB_URL="http://${IP}:${SUB_PORT}/sub/"
fi

echo "$SETUP_URL" > /app/logs/setup_url.txt
echo "$PANEL_URL" > /app/logs/node_url.txt
echo "$SUB_URL"   > /app/logs/sub_url.txt

inf "Setup URL:  $SETUP_URL"
inf "Node URL:  $PANEL_URL"
inf "Sub URL:    $SUB_URL"

ok "All pre-checks done — starting supervisor..."
exec /usr/bin/supervisord -n -c /app/supervisord.conf
