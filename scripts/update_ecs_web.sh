#!/usr/bin/env bash
set -euo pipefail

APP_ROOT=${APP_ROOT:-/opt/DouYinSparkFlow}
cd "$APP_ROOT"
git pull --ff-only origin main
"$APP_ROOT/.venv/bin/pip" install -r requirements.txt
systemctl restart sparkflow-web
systemctl is-active --quiet sparkflow-web
curl -fsS -o /dev/null http://127.0.0.1:8765/
echo "SparkFlow web updated"
