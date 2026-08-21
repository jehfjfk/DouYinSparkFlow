#!/usr/bin/env bash
set -euo pipefail

APP_ROOT=${APP_ROOT:-/opt/DouYinSparkFlow}
cd "$APP_ROOT"
# Some ECS network paths terminate GitHub's HTTP/2 stream early. Keep the
# repository update on HTTP/1.1 and retry the fetch before touching services.
git config http.version HTTP/1.1
updated=false
for attempt in 1 2 3; do
  if git -c http.version=HTTP/1.1 fetch --prune origin main; then
    git merge --ff-only FETCH_HEAD
    updated=true
    break
  fi
  echo "GitHub fetch failed (attempt ${attempt}/3); retrying..." >&2
  sleep 3
done
if [ "$updated" != true ]; then
  echo "GitHub fetch failed after 3 attempts" >&2
  exit 1
fi
"$APP_ROOT/.venv/bin/pip" install -r requirements.txt
systemctl restart sparkflow-web nginx
systemctl is-active --quiet sparkflow-web nginx
ready=false
for attempt in $(seq 1 15); do
  if curl -fsS -o /dev/null http://127.0.0.1:8765/; then
    ready=true
    break
  fi
  sleep 2
done
if [ "$ready" != true ]; then
  systemctl --no-pager --full status sparkflow-web || true
  journalctl -u sparkflow-web -n 80 --no-pager || true
  exit 1
fi
curl -fsS -o /dev/null http://127.0.0.1/
echo "SparkFlow web updated"
