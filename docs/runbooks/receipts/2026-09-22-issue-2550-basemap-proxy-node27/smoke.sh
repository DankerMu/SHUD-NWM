#!/usr/bin/env bash
# #2550 loopback smoke: PR branch in an isolated worktree, own port + scratch cache.
set -uo pipefail
export PATH=$HOME/.local/bin:$PATH
WT=$HOME/NWM-pr2551; OUT=$HOME/tmp/pr2551-smoke; PORT=18023
mkdir -p "$OUT"; CACHE=$OUT/cache; rm -rf "$CACHE"
cd /home/nwm/NWM
git fetch -q origin feat/issue-2550-tianditu-basemap-proxy || exit 2
[ -d "$WT" ] || git worktree add -q --detach "$WT" FETCH_HEAD || exit 2
cd "$WT" && echo "sha=$(git rev-parse --short HEAD)"
set -a; . /home/nwm/NWM/infra/env/display.env; set +a
export NHMS_MVT_FILE_CACHE_DIR=$CACHE
/home/nwm/NWM/.venv/bin/python -m uvicorn apps.api.main:app --host 127.0.0.1 --port $PORT --workers 1 >"$OUT/uvicorn.log" 2>&1 &
PID=$!
for i in $(seq 60); do curl -s -o /dev/null "http://127.0.0.1:$PORT/health" && break; sleep 1; done
probe() { printf '%s -> ' "$1"; curl -s -D - -o "$OUT/body" "http://127.0.0.1:$PORT$1" | grep -iE '^(HTTP|cache-control|retry-after|x-tile-cache|content-type)' | tr -d '\r' | tr '\n' ' '; echo; }
probe /api/v1/basemap/tianditu/vec/1/1/0
echo "body: $(head -c 300 "$OUT/body" | grep -v $'\x89PNG' )"
probe /api/v1/basemap/tianditu/vec/1/1/0
probe /api/v1/basemap/tianditu/cva/2/1/1
probe /api/v1/basemap/tianditu/osm/1/1/0
probe /api/v1/basemap/tianditu/vec/1/5/0
echo "cached files: $(find "$CACHE" -type f 2>/dev/null | wc -l)"
kill $PID; wait $PID 2>/dev/null
grep -ciE "traceback|error" "$OUT/uvicorn.log" | sed 's/^/uvicorn_log_error_lines=/'
cd /home/nwm/NWM && git worktree remove --force "$WT" && echo worktree_removed
