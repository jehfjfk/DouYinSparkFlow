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
  echo "GitHub fetch failed; using the source archive fallback..." >&2
  archive_url=${GITHUB_ARCHIVE_URL:-https://codeload.github.com/jehfjfk/DouYinSparkFlow/tar.gz/refs/heads/main}
  staging=$(mktemp -d)
  trap 'rm -rf "$staging"' EXIT
  curl --http1.1 --fail --location --retry 3 --retry-all-errors "$archive_url" -o "$staging/source.tar.gz"
  mkdir -p "$staging/source"
  tar -xzf "$staging/source.tar.gz" -C "$staging/source" --strip-components=1
  # The archive excludes local secrets and runtime state; preserve those files
  # explicitly in case a future archive includes additional generated files.
  cp -a "$staging/source/." "$APP_ROOT/"
  echo "Updated project files from the GitHub source archive." >&2
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
