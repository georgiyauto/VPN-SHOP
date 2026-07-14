FROM python:3.11-slim

WORKDIR /app

# System deps: supervisor + certbot + nginx + postgres + redis
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libpq-dev \
    supervisor \
    nginx \
    certbot \
    python3-certbot-nginx \
    curl unzip ca-certificates \
    redis-server \
    postgresql \
    postgresql-client \
    gosu \
    && rm -rf /var/lib/apt/lists/*

# Pinned official plain Xray-core with the publisher-provided SHA-256.
ARG XRAY_VERSION=v26.3.27
ARG XRAY_LINUX64_SHA256=23cd9af937744d97776ee35ecad4972cf4b2109d1e0fe6be9930467608f7c8ae
RUN curl -fsSL "https://github.com/XTLS/Xray-core/releases/download/${XRAY_VERSION}/Xray-linux-64.zip" -o /tmp/xray.zip \
    && echo "${XRAY_LINUX64_SHA256}  /tmp/xray.zip" | sha256sum -c - \
    && install -d -m 0755 /usr/local/share/xray \
    && unzip -j /tmp/xray.zip xray -d /usr/local/bin \
    && unzip -j /tmp/xray.zip geoip.dat geosite.dat -d /usr/local/share/xray \
    && chmod 0755 /usr/local/bin/xray \
    && rm -f /tmp/xray.zip \
    && install -d -m 0750 /etc/xray /var/lib/kawavpn-xray

# Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Remove default nginx site
RUN rm -f /etc/nginx/sites-enabled/default

# Copy supervisord config to system location
RUN mkdir -p /etc/supervisor/conf.d
COPY supervisord.conf /etc/supervisor/conf.d/vpnbot.conf
COPY supervisord.conf /app/supervisord.conf

# Ensure default supervisord.conf includes conf.d/*.conf
RUN grep -q 'conf.d' /etc/supervisor/supervisord.conf 2>/dev/null || \
    printf '\n[include]\nfiles = /etc/supervisor/conf.d/*.conf\n' >> /etc/supervisor/supervisord.conf

# Entrypoint
COPY docker-entrypoint.sh /docker-entrypoint.sh
RUN chmod +x /docker-entrypoint.sh

EXPOSE 80 443/tcp 443/udp 1414 8433 8090

ENTRYPOINT ["/docker-entrypoint.sh"]
