#!/usr/bin/env sh
set -eu

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
work_root=$(mktemp -d "${TMPDIR:-/tmp}/idea-smoke.XXXXXX")
server_pid=""

cleanup() {
    if [ -n "$server_pid" ]; then
        kill "$server_pid" 2>/dev/null || true
        wait "$server_pid" 2>/dev/null || true
    fi
    rm -rf -- "$work_root"
}
trap cleanup EXIT INT TERM

cd "$repo_root"
uv run idea serve --no-open --port 8791 --work-root "$work_root" >"$work_root/stdout.log" 2>"$work_root/stderr.log" &
server_pid=$!

attempt=0
while [ "$attempt" -lt 100 ]; do
    if ! kill -0 "$server_pid" 2>/dev/null; then
        echo "idea serve exited early" >&2
        exit 1
    fi
    if uv run python -c "import json,urllib.request; data=json.load(urllib.request.urlopen('http://127.0.0.1:8791/healthz', timeout=1)); assert data == {'ok': True}" 2>/dev/null; then
        break
    fi
    attempt=$((attempt + 1))
    sleep 0.2
done
if [ "$attempt" -ge 100 ]; then
    echo "GET /healthz did not become ready within 20 seconds" >&2
    exit 1
fi
uv run python -c "import urllib.request; response=urllib.request.urlopen('http://127.0.0.1:8791/', timeout=5); assert response.status == 200 and b'Drop a mix' in response.read()"
echo "smoke serve passed: /healthz=200 ok=true; /=200 contains 'Drop a mix'"
