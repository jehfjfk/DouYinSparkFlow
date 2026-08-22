#!/usr/bin/env bash
set -euo pipefail

# Repair an ECS that still has the legacy Xvfb unit or a half-updated checkout.
# Secrets and runtime state stay in place; only tracked project files are replaced.
APP_ROOT=${APP_ROOT:-/opt/DouYinSparkFlow}
REPOSITORY=${SPARKFLOW_REPOSITORY:-jehfjfk/DouYinSparkFlow}
REF=${SPARKFLOW_REF:-main}

if [ "$(id -u)" -ne 0 ]; then
  echo "请在 ECS Workbench 以 root 执行。" >&2
  exit 1
fi
test -d "$APP_ROOT" || { echo "目录不存在：$APP_ROOT" >&2; exit 1; }

staging=$(mktemp -d)
cleanup() { rm -rf "$staging"; }
trap cleanup EXIT
archive="https://codeload.github.com/${REPOSITORY}/tar.gz/refs/heads/${REF}"
curl --http1.1 --fail --location --retry 5 --retry-all-errors "$archive" -o "$staging/source.tar.gz"
mkdir "$staging/source"
tar -xzf "$staging/source.tar.gz" -C "$staging/source" --strip-components=1
test -f "$staging/source/web_app.py" || { echo "源码归档不完整。" >&2; exit 1; }

systemctl stop sparkflow-web 2>/dev/null || true
cp -a "$staging/source/." "$APP_ROOT/"
test -f "$APP_ROOT/scripts/install_ecs_web_service.sh"
bash "$APP_ROOT/scripts/install_ecs_web_service.sh"

systemctl is-active --quiet sparkflow-web
systemctl is-active --quiet nginx
curl --noproxy '*' --fail --silent --show-error http://127.0.0.1:8765/api/healthz >/dev/null
curl --noproxy '*' --fail --silent --show-error http://127.0.0.1/api/healthz >/dev/null
echo "SparkFlow ECS web service repaired and healthy."
