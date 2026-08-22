#!/usr/bin/env bash
set -euo pipefail

# Install a self-contained ECS service. The web dashboard is headless until a
# user starts a scan, so it does not need Xvfb just to stay available.
APP_ROOT=${APP_ROOT:-/opt/DouYinSparkFlow}
APP_USER=${APP_USER:-sparkflow}
PYTHON=${PYTHON:-$APP_ROOT/.venv/bin/python}
SERVICE_NAME=sparkflow-web

if [ "$(id -u)" -ne 0 ]; then
  echo "Run this installer as root." >&2
  exit 1
fi
test -f "$APP_ROOT/web_app.py" || { echo "Missing $APP_ROOT/web_app.py" >&2; exit 1; }
test -x "$PYTHON" || { echo "Missing executable $PYTHON" >&2; exit 1; }

if ! id "$APP_USER" >/dev/null 2>&1; then
  useradd --system --create-home --home-dir "/home/$APP_USER" --shell /usr/sbin/nologin "$APP_USER"
fi
install -d -o "$APP_USER" -g "$APP_USER" -m 750 "$APP_ROOT/logs"
if [ ! -f "$APP_ROOT/.web-users.json" ]; then
  printf '{"users":[]}\n' > "$APP_ROOT/.web-users.json"
fi
chown "$APP_USER:$APP_USER" "$APP_ROOT/.web-users.json" "$APP_ROOT/logs"
chmod 600 "$APP_ROOT/.web-users.json"
if [ -f "$APP_ROOT/.env" ]; then
  chown "$APP_USER:$APP_USER" "$APP_ROOT/.env"
  chmod 600 "$APP_ROOT/.env"
fi

cat > "/etc/systemd/system/$SERVICE_NAME.service" <<UNIT
[Unit]
Description=DouYin Spark Flow Web Dashboard
Wants=network-online.target
After=network-online.target
StartLimitIntervalSec=0

[Service]
Type=simple
User=$APP_USER
Group=$APP_USER
WorkingDirectory=$APP_ROOT
Environment=HOME=/home/$APP_USER
Environment=PYTHONUNBUFFERED=1
Environment=SPARKFLOW_HEADLESS=1
Environment=PLAYWRIGHT_BROWSERS_PATH=$APP_ROOT/chrome
EnvironmentFile=-$APP_ROOT/.env
ExecStart=$PYTHON -u $APP_ROOT/web_app.py --host 127.0.0.1 --port 8765
Restart=always
RestartSec=3
KillSignal=SIGINT
TimeoutStopSec=15
StandardOutput=append:$APP_ROOT/logs/web-service.log
StandardError=append:$APP_ROOT/logs/web-service.log

[Install]
WantedBy=multi-user.target
UNIT
chmod 644 "/etc/systemd/system/$SERVICE_NAME.service"

if ! command -v nginx >/dev/null 2>&1; then
  apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install -y nginx
fi
cat > /etc/nginx/sites-available/sparkflow-web <<'NGINX'
server {
    listen 80 default_server;
    listen [::]:80 default_server;
    server_name _;

    location / {
        proxy_pass http://127.0.0.1:8765;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 300s;
        proxy_send_timeout 300s;
    }
}
NGINX
# Keep this instance's dashboard as the only enabled site. Older deployments
# may have renamed old files with a .sparkflow-disabled suffix; nginx still
# includes those files, so content-based filtering is not sufficient.
install -d -m 755 /etc/nginx/sites-disabled
for enabled in /etc/nginx/sites-enabled/*; do
  [ -e "$enabled" ] || [ -L "$enabled" ] || continue
  [ "$enabled" = "/etc/nginx/sites-enabled/sparkflow-web" ] && continue
  name=$(basename "$enabled")
  mv -f "$enabled" "/etc/nginx/sites-disabled/${name}.$$.disabled"
done
ln -sfn /etc/nginx/sites-available/sparkflow-web /etc/nginx/sites-enabled/sparkflow-web

systemctl daemon-reload
nginx -t
systemctl enable --now "$SERVICE_NAME"
systemctl restart "$SERVICE_NAME"
systemctl enable --now nginx
systemctl restart nginx

ready=false
for _ in $(seq 1 20); do
  if env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY curl --noproxy '*' -fsS http://127.0.0.1:8765/api/healthz >/dev/null; then
    ready=true
    break
  fi
  sleep 1
done
if [ "$ready" != true ]; then
  systemctl --no-pager --full status "$SERVICE_NAME" || true
  journalctl -u "$SERVICE_NAME" -n 100 --no-pager || true
  exit 1
fi
env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY curl --noproxy '*' -fsS http://127.0.0.1/api/healthz >/dev/null
echo "SparkFlow ECS web service is ready on port 80."
