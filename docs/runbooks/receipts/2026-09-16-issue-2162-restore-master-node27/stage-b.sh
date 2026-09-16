#!/bin/bash
# #2162 target 2 -- restore /home/nwm/NWM from #1987's reviewed-NEW branch
# (issue1987-reviewed-new-415cbd1e = 415cbd1e) to master at the SHA frozen in the
# GO record, rebuild the frontend bundle out of tree and swap it atomically,
# restart the display API once, gate read surfaces + frontend, re-measure prewarm
# lock counts, hand the writer timers back, observe the first ticks, and prove the
# 9 user units' effective configuration is byte-identical to the pre-window
# capture (the window rewrites the tree and the bundle, never a unit or an env).
# Foreign fences preserved untouched: compression unit bound to #1895's reviewed
# checkout, CAPACITY HOLD dir, yd-* instance, every drop-in (expected: none).
# Exit codes: 0 done; 2 precondition (nothing changed); 3 checkout/build
# (rolled back); 5 display/read/frontend gate (rolled back); 6 RECORD after the
# point of no return (gate passed, a later observation failed; not rolled back).
set -u
export PATH=$HOME/.local/bin:$PATH
export TMPDIR=/home/nwm/tmp

OLD_SHA=415cbd1e9d0eee39ba0dfb623a586b02cbb340f2
OLD_BRANCH=issue1987-reviewed-new-415cbd1e
NEW_SHA=f2476c8c29774d40b300d4a8a13657b5d6d05139   # from the signed GO record on #2162, not from live origin/master
MUST_CONTAIN=e0cfe40b                              # acceptance item 2 (#2032 / PR #2151)

TS=$(date -u +%Y%m%dT%H%M%SZ)
OUT=/home/nwm/tmp/2162/out-b-$TS
BK=/home/nwm/nhms-restore-master-backup-2162-$TS
DIST_NEW=/home/nwm/tmp/2162/dist-new-$TS
REPO=/home/nwm/NWM
PY=$REPO/.venv/bin/python
USERD=/home/nwm/.config/systemd/user
FOREIGN_COMPRESSION_ROOT=/home/nwm/NWM-maintenance-reviewed-95481481
UNITS="nhms-display-api nhms-node27-autopipe nhms-node27-download nhms-node27-raw-retention nhms-node27-timeseries-retention nhms-node27-timeseries-compression nhms-node27-resource-governance nhms-node27-frontier-alert nhms-node27-mvt-cache-retention"
STOP_TIMERS="nhms-node27-autopipe.timer nhms-node27-download.timer nhms-node27-frontier-alert.timer"
CACHE=/home/nwm/.cache/nhms/mvt
HOLD=/home/nwm/.local/state/nhms-pgdata-pr-2240-capacity-hold
_ing=$REPO/infra/env/node27-ingest.env
_lf=$(sed -n 's/^AUTOPIPE_LOG_FILE=//p' "$_ing" | tr -d '"'"'"'"' | tail -1)
_lr=$(sed -n 's/^AUTOPIPE_LOG_ROOT=//p' "$_ing" | tr -d '"'"'"'"' | tail -1)
AUTOPIPE_LOG=${_lf:-${_lr:-/home/nwm/autopipe-logs}/autopipe.log}
L=http://127.0.0.1:8080
PUB=https://test.nwm.ac.cn
mkdir -p "$OUT"

ts() { date -u +%FT%TZ; }
say() { echo "[$(ts)] $*"; }
settled() { case "$1" in inactive|failed) return 0;; *) return 1;; esac; }
locks() { find "$CACHE/.locks" -type f -name '*.lock' 2>/dev/null | wc -l; }
pbfs() { find "$CACHE" -type f -name '*.pbf' 2>/dev/null | wc -l; }
probe() {
  local url=$1 hf=${2:-}
  shift $(( $# >= 2 ? 2 : 1 ))
  curl -s -o /dev/null ${hf:+-D "$hf"} -w '%{http_code} %{size_download} %{time_starttransfer}' --max-time 120 "$@" "$url"
}
hdr() { grep -i "^$1:" "$2" | head -1 | cut -d' ' -f2- | tr -d '\r'; }
# asset references of an index.html body (hashed vite bundles), sorted
assets_of() { grep -oE 'assets/[A-Za-z0-9_.-]+\.(js|css)' "$1" | sort -u; }

TIMERS_STOPPED=0; CHECKED_OUT=0; DIST_SWAPPED=0
restart_timers() {
  if [ "$TIMERS_STOPPED" = 1 ]; then
    systemctl --user start $STOP_TIMERS
    TIMERS_STOPPED=0; say "timers started: $STOP_TIMERS (EXIT trap or step 2.10)"
  fi
}
trap restart_timers EXIT

capture() {
  for u in $UNITS; do
    echo "## $u"
    systemctl --user show "$u.service" -p FragmentPath -p DropInPaths -p WorkingDirectory -p ExecStartPre -p ExecStart -p ExecStartPost -p ExecStop -p Environment -p EnvironmentFiles \
      | sed -E 's/ ; ignore_errors=.*$//; s/(TOKEN|PASSWORD|SECRET)=[^ ]*/\1=<redacted>/g' | LC_ALL=C sort
  done > "$OUT/units-$1.txt"
}
git_identity() {
  echo "head=$(git -C "$REPO" rev-parse HEAD) branch=$(git -C "$REPO" rev-parse --abbrev-ref HEAD) upstream=$(git -C "$REPO" rev-parse --abbrev-ref '@{u}' 2>/dev/null || echo none) porcelain=$(git -C "$REPO" status --porcelain | wc -l) dist_index_mtime=$(stat -c %y "$REPO/apps/frontend/dist/index.html" 2>/dev/null | cut -c1-19) dist_assets=$(assets_of "$REPO/apps/frontend/dist/index.html" 2>/dev/null | tr '\n' ',')"
}
wait_health() {
  for _ in $(seq 1 30); do curl -sf --max-time 2 "$L/health" >/dev/null 2>&1 && return 0; sleep 1; done
  return 1
}
rollback() {
  say "ROLLBACK ($1)"
  if [ "$DIST_SWAPPED" = 1 ]; then
    mv "$REPO/apps/frontend/dist" "$BK/frontend-dist-new-rejected" && mv "$BK/frontend-dist-$OLD_SHORT" "$REPO/apps/frontend/dist" \
      && say "rollback: old dist restored" || say "rollback: dist restore FAILED -- manual attention"
    DIST_SWAPPED=0
  fi
  if [ "$CHECKED_OUT" = 1 ]; then
    git -C "$REPO" checkout -q "$OLD_BRANCH" && [ "$(git -C "$REPO" rev-parse HEAD)" = "$OLD_SHA" ] \
      && say "rollback: tree back on $OLD_BRANCH @ $OLD_SHA" || say "rollback: checkout $OLD_BRANCH FAILED -- manual attention"
    CHECKED_OUT=0
  fi
  systemctl --user daemon-reload
  if [ -n "${T_RESTART:-}" ]; then
    systemctl --user restart nhms-display-api.service
    if wait_health; then say "rollback: /health ok"; else say "rollback: /health FAILED -- manual attention"; fi
  else
    say "rollback: display never restarted in this window -- running process untouched"
  fi
  capture rollback
  say "rollback: $(git_identity)"
  exit "$2"
}

OLD_SHORT=${OLD_SHA:0:8}
say "OUT=$OUT BK=$BK DIST_NEW=$DIST_NEW"

# ---------------------------------------------------------------- 2.1 baseline
say "== 2.1 baseline + preconditions (nothing changed before 2.4)"
cd "$REPO" || exit 2
git fetch -q origin || { say "precondition: git fetch failed"; exit 2; }
echo "before: $(git_identity)"
[ "$(git rev-parse HEAD)" = "$OLD_SHA" ] || { say "precondition: HEAD != OLD_SHA"; exit 2; }
[ "$(git rev-parse --abbrev-ref HEAD)" = "$OLD_BRANCH" ] || { say "precondition: branch != $OLD_BRANCH"; exit 2; }
[ "$(git status --porcelain | wc -l)" = 0 ] || { say "precondition: porcelain not empty"; git status --porcelain; exit 2; }
[ "$(git rev-parse origin/master)" = "$NEW_SHA" ] || { say "precondition: origin/master=$(git rev-parse origin/master) != GO-record NEW_SHA=$NEW_SHA (master moved; re-decide, do not take it silently)"; exit 2; }
git merge-base --is-ancestor master origin/master || { say "precondition: local master is not an ancestor of origin/master"; exit 2; }
git merge-base --is-ancestor "$OLD_SHA" "$NEW_SHA" || { say "precondition: OLD_SHA not an ancestor of NEW_SHA"; exit 2; }
git merge-base --is-ancestor "$MUST_CONTAIN" "$NEW_SHA" || { say "precondition: NEW_SHA lacks $MUST_CONTAIN"; exit 2; }
echo "behind/ahead: $(git rev-list --count HEAD..origin/master) $(git rev-list --count origin/master..HEAD)"
echo "dep-diff-lines(pyproject uv.lock .python-version)=$(git diff "$OLD_SHA" "$NEW_SHA" -- pyproject.toml uv.lock .python-version | wc -l) venv=$("$PY" -V 2>&1)"
[ "$(git diff "$OLD_SHA" "$NEW_SHA" -- pyproject.toml uv.lock .python-version | wc -l)" = 0 ] || { say "precondition: python deps changed; venv reuse not proven"; exit 2; }
echo "frontend-dep-diff-lines(package.json pnpm-lock.yaml)=$(git diff "$OLD_SHA" "$NEW_SHA" -- apps/frontend/package.json apps/frontend/pnpm-lock.yaml | wc -l) frontend-files-changed=$(git diff --name-only "$OLD_SHA" "$NEW_SHA" -- apps/frontend | wc -l)"
[ "$(git diff "$OLD_SHA" "$NEW_SHA" -- apps/frontend/package.json apps/frontend/pnpm-lock.yaml | wc -l)" = 0 ] || { say "precondition: frontend deps changed; node_modules reuse not proven"; exit 2; }
echo "migrations-diff-lines=$(git diff --name-only "$OLD_SHA" "$NEW_SHA" -- db/migrations | wc -l) roles-diff-lines=$(git diff --name-only "$OLD_SHA" "$NEW_SHA" -- db/roles | wc -l)"
echo "toolchain: node=$(node -v) pnpm=$(cd apps/frontend && corepack pnpm -v 2>/dev/null | tail -1) packageManager=$(grep -o '"packageManager": *"[^"]*"' apps/frontend/package.json)"
[ -d apps/frontend/node_modules ] || { say "precondition: node_modules missing"; exit 2; }
[ "$(find "$USERD" -path '*.d/*.conf' | wc -l)" = 0 ] || { say "precondition: drop-ins present"; find "$USERD" -path '*.d/*.conf'; exit 2; }
[ "$(systemctl --user show nhms-node27-timeseries-compression-replay.service -p LoadState --value)" = not-found ] && echo "replay unit: not-found (absent-approved per #1895 R5.2)" || { say "precondition: replay unit present"; exit 2; }
lslocks 2>/dev/null | grep -qi 'nhms-node27-timeseries-lifecycle' && { say "precondition: lifecycle flock held"; lslocks | grep -i lifecycle; exit 2; } || echo "lifecycle lock: no holder"
capture before
MAIN0=$(systemctl --user show nhms-display-api.service -p MainPID --value)
UVPIDS=$(pgrep -f 'python -m uvicorn apps.api.main:app --host 127.0.0.1 --port 8080' | sort -n | tr '\n' ' ')
echo "display MainPID=$MAIN0 uvicorn_8080_pids=[$UVPIDS] ActiveState=$(systemctl --user is-active nhms-display-api.service) cwd=$(readlink /proc/"$MAIN0"/cwd) cmd=$(tr '\0' ' ' < /proc/"$MAIN0"/cmdline | cut -c1-80)"
OK=0; for p in $UVPIDS; do [ "$p" = "$MAIN0" ] && OK=1; done
[ "$OK" = 1 ] && [ "$MAIN0" != 0 ] || { say "precondition: unit MainPID $MAIN0 not among :8080 uvicorn pids [$UVPIDS]"; exit 2; }
for p in $UVPIDS; do [ "$p" = "$MAIN0" ] && continue; pp=$(awk '/^PPid:/{print $2}' /proc/"$p"/status 2>/dev/null); [ "$pp" = "$MAIN0" ] || { say "precondition: :8080 pid $p (ppid $pp) not a child of MainPID $MAIN0"; exit 2; }; done
[ "$(readlink /proc/"$MAIN0"/cwd)" = "$REPO" ] || { say "precondition: display cwd not $REPO"; exit 2; }
echo "display env: $(tr '\0' '\n' < /proc/"$MAIN0"/environ | grep -E '^NHMS_MVT_FILE_CACHE_DIR=') token_key=$(tr '\0' '\n' < /proc/"$MAIN0"/environ | grep -c '^NHMS_DISPLAY_CACHE_WARM_TOKEN=')"
echo "display.env keys lacking vs master example: $(comm -13 <(grep -oE '^[A-Z_]+=' infra/env/display.env | tr -d = | sort) <(git show "$NEW_SHA":infra/env/display.example | grep -oE '^[A-Z_]+=' | tr -d = | sort) | tr '\n' ' ')"
systemctl --user list-timers --all --no-pager | tee "$OUT/timers-before.txt" | grep -E 'NEXT|nhms-'
YD0=$(pgrep -f '^/home/nwm/yd-NWM/.venv/bin/python -m uvicorn' | sort -n | head -1)
echo "yd :8081 pid=$YD0 health=$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 http://127.0.0.1:8081/health)"
L0=$(locks); P0=$(pbfs); echo "cache locks=$L0 pbf=$P0 size=$(du -sh "$CACHE" | cut -f1)"
df -h / /home /data/GHDC | tee "$OUT/df-before.txt"
set -a; . "$REPO/infra/env/display.env"; set +a
psql "$DATABASE_URL" -Atc "select version, applied_at from public.schema_migrations where version >= '000056' order by 1" | tee "$OUT/ledger.txt"
ls -la "$HOLD" > "$OUT/hold-before.txt"; cat "$OUT/hold-before.txt"
echo "91-fences=$(find "$USERD" -name '91-*' | wc -l)"
for u in $UNITS; do
  for k in service timer; do
    f=infra/systemd/$u.$k
    if git cat-file -e "$NEW_SHA:$f" 2>/dev/null && [ -f "$USERD/$u.$k" ]; then
      git show "$NEW_SHA:$f" | diff - "$USERD/$u.$k" > "$OUT/unitdiff-$u.$k.txt"; echo "unit-vs-master $u.$k: $(wc -l < "$OUT/unitdiff-$u.$k.txt") lines"
    fi
  done
done
cp -p apps/frontend/dist/index.html "$OUT/index-before.html"; echo "dist before: $(stat -c %y apps/frontend/dist/index.html | cut -c1-19) assets=[$(assets_of apps/frontend/dist/index.html | tr '\n' ' ')] geo=$(ls apps/frontend/dist/geo | wc -l)"
curl -s --max-time 10 -o "$OUT/root-local-before.html" -w 'local  / : %{http_code}\n' "$L/"; echo "  served assets=[$(assets_of "$OUT/root-local-before.html" | tr '\n' ' ')]"
curl -s --max-time 20 -o "$OUT/root-public-before.html" -w 'public / : %{http_code}\n' "$PUB/"; echo "  served assets=[$(assets_of "$OUT/root-public-before.html" | tr '\n' ' ')]"
echo "local  /api/v1/layers: $(probe "$L/api/v1/layers")"
echo "public /api/v1/layers: $(probe "$PUB/api/v1/layers")"
echo "local  river-network-national/5/25/12 (pre): $(probe "$L/api/v1/tiles/river-network-national/5/25/12.pbf" "$OUT/rn-pre.h") etag=$(hdr etag "$OUT/rn-pre.h") cache=$(hdr x-tile-cache "$OUT/rn-pre.h")"
RUN0=$(curl -s --max-time 20 "$L/api/v1/runs?limit=1" | jq -r '.data.items[0].run_id // empty'); echo "run_id sample=$RUN0"
for inst in '9999-12-31T23:59:59-08:00' '0001-01-01T00:00:00+08:00' 'not-an-instant'; do
  echo "public pre hydro-national $inst: $(probe "$PUB/api/v1/tiles/hydro-national/q_down/$inst/4/12/6.pbf")"
done
echo "public pre offset=999999999: $(probe "$PUB/api/v1/layers?offset=999999999")"
echo "raw-retention last: $(systemctl --user show nhms-node27-raw-retention.service -p ExecMainStartTimestamp -p Result --value | tr '\n' ' ')"

# ------------------------------------------------------------ 2.2 writer fence
say "== 2.2 stop in-window timers ($STOP_TIMERS)"
T_STOP=$(ts)
systemctl --user stop $STOP_TIMERS; TIMERS_STOPPED=1
for i in $(seq 1 180); do
  s=$(systemctl --user is-active nhms-node27-autopipe.service); d=$(systemctl --user is-active nhms-node27-download.service); f=$(systemctl --user is-active nhms-node27-frontier-alert.service)
  if settled "$s" && settled "$d" && settled "$f"; then echo "settled after $((i*10))s: autopipe=$s download=$d frontier=$f"; break; fi
  [ $((i % 6)) -eq 0 ] && echo "   still draining ${i}0s: autopipe=$s download=$d frontier=$f"
  sleep 10
done
settled "$(systemctl --user is-active nhms-node27-autopipe.service)" && settled "$(systemctl --user is-active nhms-node27-download.service)" && settled "$(systemctl --user is-active nhms-node27-frontier-alert.service)" \
  || { say "services did not settle in 30 min -- abort before any change"; exit 2; }
OTHER=$(pgrep -u nwm -f "$REPO/" | while read -r p; do [ "$p" = "$MAIN0" ] && continue; pp=$(awk '/^PPid:/{print $2}' /proc/"$p"/status 2>/dev/null); [ "$pp" = "$MAIN0" ] && continue; echo "$p:$(tr '\0' ' ' < /proc/"$p"/cmdline | cut -c1-60)"; done | grep -v "$0" | tr '\n' ' ')
echo "other processes referencing $REPO/ (excluding display tree + this script): [${OTHER:-none}]"
AUTOPIPE_LOG_OFF=$(stat -c %s "$AUTOPIPE_LOG" 2>/dev/null || echo 0)
AUTOPIPE_LAST_MONO=$(systemctl --user show nhms-node27-autopipe.service -p ExecMainStartTimestampMonotonic --value)
echo "autopipe per-tick log: $AUTOPIPE_LOG ($AUTOPIPE_LOG_OFF bytes)"

# ------------------------------------------------------------------ 2.3 backup
say "== 2.3 backup -> $BK"
mkdir -m 0700 "$BK" || exit 2
git_identity > "$BK/git-identity-before.txt"
for f in "$REPO"/infra/env/*.env; do cp --parents -p "$f" "$BK/" || exit 2; done
for f in "$USERD"/nhms-*.service "$USERD"/nhms-*.timer; do [ -f "$f" ] && { cp --parents -p "$f" "$BK/" || exit 2; }; done
find "$BK" -type f | sort | tee "$OUT/backup-list.txt" | wc -l

# ---------------------------------------------------------------- 2.4 checkout
say "== 2.4 checkout master @ NEW_SHA (single tree rewrite; display keeps serving old modules until 2.7)"
T_CHECKOUT=$(ts)
git checkout -q -B master origin/master || rollback "git checkout -B master origin/master" 3
CHECKED_OUT=1
[ "$(git rev-parse HEAD)" = "$NEW_SHA" ] || rollback "HEAD != NEW_SHA after checkout" 3
[ "$(git rev-parse --abbrev-ref '@{u}')" = origin/master ] || rollback "upstream != origin/master" 3
PULL=$(git pull --ff-only 2>&1); echo "git pull --ff-only: $PULL"; case "$PULL" in *"Already up to date"*) ;; *) rollback "pull not a no-op" 3;; esac
[ "$(git status --porcelain | wc -l)" = 0 ] || { git status --porcelain; rollback "porcelain not empty after checkout" 3; }
git merge-base --is-ancestor "$MUST_CONTAIN" HEAD || rollback "HEAD lacks $MUST_CONTAIN" 3
echo "after checkout: $(git_identity)"

# ----------------------------------------------------------- 2.5 frontend build
say "== 2.5 vite build out of tree -> $DIST_NEW"
T_BUILD0=$(date +%s)
( cd apps/frontend && corepack pnpm exec vite build --outDir "$DIST_NEW" ) > "$OUT/frontend-build.log" 2>&1; B_RC=$?
echo "vite build rc=$B_RC elapsed=$(( $(date +%s) - T_BUILD0 ))s log_lines=$(wc -l < "$OUT/frontend-build.log")"
tail -5 "$OUT/frontend-build.log"
[ "$B_RC" = 0 ] || rollback "vite build rc=$B_RC" 3
[ -f "$DIST_NEW/index.html" ] && [ "$(ls "$DIST_NEW/geo" 2>/dev/null | wc -l)" = "$(git ls-files apps/frontend/public/geo | wc -l)" ] || rollback "built dist incomplete" 3
[ "$(git status --porcelain | wc -l)" = 0 ] || { git status --porcelain; rollback "porcelain dirtied by build" 3; }
NEW_ASSETS=$(assets_of "$DIST_NEW/index.html" | tr '\n' ' '); OLD_ASSETS=$(assets_of "$OUT/index-before.html" | tr '\n' ' ')
echo "built assets=[$NEW_ASSETS] old=[$OLD_ASSETS] changed=$([ "$NEW_ASSETS" != "$OLD_ASSETS" ] && echo yes || echo no)"
for a in $(assets_of "$DIST_NEW/index.html"); do [ -f "$DIST_NEW/$a" ] || rollback "built index references missing $a" 3; done
du -sh "$DIST_NEW" | cut -f1

# -------------------------------------------------------------- 2.6 dist swap
say "== 2.6 atomic dist swap"
mv "$REPO/apps/frontend/dist" "$BK/frontend-dist-$OLD_SHORT" || rollback "move old dist" 3
mv "$DIST_NEW" "$REPO/apps/frontend/dist" || { mv "$BK/frontend-dist-$OLD_SHORT" "$REPO/apps/frontend/dist"; rollback "move new dist" 3; }
DIST_SWAPPED=1
[ "$(git status --porcelain | wc -l)" = 0 ] || rollback "porcelain not empty after swap" 3
echo "swapped: $(git_identity)"

# --------------------------------------------------------- 2.7 display restart
say "== 2.7 restart display (systemd unit, by hand)"
systemctl --user daemon-reload
T_RESTART=$(ts)
systemctl --user restart nhms-display-api.service
if wait_health; then echo "/health ok at $(ts)"; else rollback "no /health within 30s" 5; fi
MAIN=$(systemctl --user show nhms-display-api.service -p MainPID --value)
CWD=$(readlink /proc/"$MAIN"/cwd); CMD=$(tr '\0' ' ' < /proc/"$MAIN"/cmdline)
echo "display MainPID=$MAIN (before $MAIN0) cwd=$CWD cmd=$CMD"
[ "$MAIN" != "$MAIN0" ] && [ "$CWD" = "$REPO" ] || rollback "display pid/cwd" 5
case "$CMD" in "$REPO/.venv/bin/python -m uvicorn apps.api.main:app"*) ;; *) rollback "display cmdline not the active tree" 5;; esac
tr '\0' '\n' < /proc/"$MAIN"/environ | grep -q "^NHMS_MVT_FILE_CACHE_DIR=$CACHE\$" || rollback "cache dir differs in process" 5
tr '\0' '\n' < /proc/"$MAIN"/environ | grep -q '^NHMS_DISPLAY_CACHE_WARM_TOKEN=' || rollback "token not in process env" 5
SMOKE=$(curl -s --max-time 10 "$L/api/v1/models?limit=1"); BASIN=$(printf '%s' "$SMOKE" | jq -r '.data.items[0].basin_id // empty')
[ -n "$BASIN" ] && [ "$BASIN" != null ] && echo "smoke: basin_id=$BASIN" || rollback "basin_id smoke" 5
YD1=$(pgrep -f '^/home/nwm/yd-NWM/.venv/bin/python -m uvicorn' | sort -n | head -1)
echo "yd :8081 pid=$YD1 (before $YD0) health=$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 http://127.0.0.1:8081/health)"
[ "$YD1" = "$YD0" ] || say "WARNING: yd pid changed"

# ------------------------------------------------- 2.8 read-surface + frontend gate
say "== 2.8 read-surface gate on :8080 + frontend gate"
FAIL=0
r=$(probe "$L/api/v1/layers"); echo "/api/v1/layers: $r"; [ "${r%% *}" = 200 ] || FAIL=1
r=$(probe "$L/api/v1/runs"); echo "/api/v1/runs: $r"; [ "${r%% *}" = 200 ] || FAIL=1
r=$(probe "$L/api/v1/models?limit=1"); echo "/api/v1/models: $r"; [ "${r%% *}" = 200 ] || FAIL=1
CYC_HTTP=$(curl -s --max-time 60 -o "$OUT/cycles-gfs.json" -w '%{http_code}' "$L/api/v1/layers/discharge/cycles?source=gfs")
CYC=$(jq -r '.data.cycles[0].cycle_time // empty' "$OUT/cycles-gfs.json"); VT=$(jq -r '.data.cycles[0].valid_time_start // empty' "$OUT/cycles-gfs.json"); SRC=gfs
if [ -z "$CYC" ]; then
  CYC_HTTP=$(curl -s --max-time 60 -o "$OUT/cycles-ifs.json" -w '%{http_code}' "$L/api/v1/layers/discharge/cycles?source=ifs")
  CYC=$(jq -r '.data.cycles[0].cycle_time // empty' "$OUT/cycles-ifs.json"); VT=$(jq -r '.data.cycles[0].valid_time_start // empty' "$OUT/cycles-ifs.json"); SRC=ifs
fi
echo "cycles: http=$CYC_HTTP source=$SRC cycle=$CYC vt=$VT"; [ "$CYC_HTTP" = 200 ] || FAIL=1
[ -z "$VT" ] && VT=$(date -u +%Y-%m-%dT%H:00:00Z)
r=$(probe "$L/api/v1/tiles/river-network-national/5/25/12.pbf" "$OUT/rn-post.h"); echo "river-network-national/5/25/12: $r etag=$(hdr etag "$OUT/rn-post.h") cache=$(hdr x-tile-cache "$OUT/rn-post.h")"; [ "${r%% *}" = 200 ] || FAIL=1
r=$(probe "$L/api/v1/tiles/hydro-national/q_down/$VT/4/12/6.pbf" "$OUT/hn-post.h"); echo "hydro-national/q_down/$VT/4/12/6: $r cache=$(hdr x-tile-cache "$OUT/hn-post.h")"; case "${r%% *}" in 200|424) ;; *) FAIL=1;; esac
if [ -n "$CYC" ]; then r=$(probe "$L/api/v1/tiles/hydro-national/$SRC/$CYC/q_down/$VT/4/12/6.pbf" "$OUT/hnc-post.h"); echo "hydro-national/$SRC/$CYC/q_down/$VT/4/12/6: $r cache=$(hdr x-tile-cache "$OUT/hnc-post.h")"; case "${r%% *}" in 200|424) ;; *) FAIL=1;; esac; fi
if [ -n "$RUN0" ]; then r=$(probe "$L/api/v1/tiles/hydro/$RUN0/q_down/$VT/7/100/50.pbf"); echo "hydro/$RUN0/q_down/$VT/7/100/50: $r"; case "${r%% *}" in 200|404|422|424) ;; *) FAIL=1;; esac; fi
r=$(probe "$L/api/v1/layers?offset=999999999"); echo "local offset=999999999: $r"; case "${r%% *}" in 200|422) ;; *) FAIL=1;; esac
# frontend: / serves the new index, every referenced asset resolves, /ops resolves
curl -s --max-time 10 -o "$OUT/root-local-after.html" -w 'local  / : %{http_code}\n' "$L/" | tee "$OUT/root-local-after.code"; grep -q ' 200$' "$OUT/root-local-after.code" || FAIL=1
SERVED=$(assets_of "$OUT/root-local-after.html" | tr '\n' ' '); if [ "$SERVED" = "$NEW_ASSETS" ]; then echo "  served assets=[$SERVED] == built: yes"; else echo "  served assets=[$SERVED] != built [$NEW_ASSETS]"; FAIL=1; fi
for a in $(assets_of "$OUT/root-local-after.html"); do r=$(probe "$L/$a"); echo "  $a: $r"; [ "${r%% *}" = 200 ] || FAIL=1; done
r=$(probe "$L/ops"); echo "local /ops: $r"; [ "${r%% *}" = 200 ] || FAIL=1
r=$(probe "$L/geo/national-basin-river.geojson"); echo "local /geo/national-basin-river.geojson: $r"; [ "${r%% *}" = 200 ] || FAIL=1
[ "$FAIL" = 0 ] || rollback "read-surface/frontend gate" 5
echo "read-surface + frontend gate: zero 500, bundle served == built"
# ------------------------------------------------------------ point of no return
FINAL_RC=0

# -------------------------------------------------------- 2.9 prewarm locks
say "== 2.9 prewarm lock counts (writers still stopped)"
L1=$(locks); P1=$(pbfs); echo "before prewarm: locks=$L1 pbf=$P1"
T_PW0=$(date +%s)
NHMS_DISPLAY_CACHE_WARM_TOKEN="$(grep '^NHMS_DISPLAY_CACHE_WARM_TOKEN=' "$REPO/infra/env/node27-ingest.env" | cut -d= -f2-)" \
  "$PY" "$REPO/scripts/node27_mvt_prewarm.py" --zooms 3,4,5 --workers 8 > "$OUT/prewarm.json" 2> "$OUT/prewarm.err"
PW_RC=$?
echo "prewarm rc=$PW_RC elapsed=$(( $(date +%s) - T_PW0 ))s token_unset_warnings=$(grep -c 'CACHE_WARM_TOKEN unset' "$OUT/prewarm.err")"
jq -c '{requests_total, failed_count, cache_hits, deadline_skipped, elapsed_seconds, tile_failed: (.failed_count - ([.per_source[].png_failed] | add // 0)), png_out_of_contract: ([.per_source[].png_out_of_contract] | add // 0), per_source: (.per_source | map_values({cycle, discharge_requests, png_ok, png_failed, error}))}' "$OUT/prewarm.json"
jq -r '.failures[] | "  failure: status=\(.status) error=\(.error // "-") code=\(.error_code // "-") reason=\(.error_reason // "-") kind=\(if (.url|test("/precip/")) then "png" else "tile" end)"' "$OUT/prewarm.json" | sort | uniq -c | sort -rn | head -10
echo "tile failures by class: $(jq -r '[.failures[] | select(.url|test("/precip/")|not) | (if .status == 0 then "exception:" + (.error // "?") else "http:" + (.status|tostring) end)] | group_by(.) | map("\(.[0])=\(length)") | join(" ")' "$OUT/prewarm.json")"
L2=$(locks); P2=$(pbfs); echo "after prewarm: locks=$L2 pbf=$P2 (delta locks=$((L2-L1)) pbf=$((P2-P1)))"
[ "$L2" -le "$L1" ] && echo "lock gate: after <= before" || { say "RECORD lock gate: after > before"; FINAL_RC=6; }
r=$(probe "$L/api/v1/tiles/river-network-national/5/25/12.pbf" "$OUT/rn-post2.h"); echo "river-network-national/5/25/12 (2nd): $r cache=$(hdr x-tile-cache "$OUT/rn-post2.h")"

# ---------------------------------------------- 2.10 timers back + first ticks
say "== 2.10 start timers"
T_START=$(ts); restart_timers
echo "window: stop=$T_STOP checkout=$T_CHECKOUT restart_display=$T_RESTART start=$T_START"
say "waiting for first natural autopipe tick from $REPO @ master (<= 30 min)"
TICK=none
for i in $(seq 1 180); do
  mono=$(systemctl --user show nhms-node27-autopipe.service -p ExecMainStartTimestampMonotonic --value)
  st=$(systemctl --user is-active nhms-node27-autopipe.service)
  grown=$(( $(stat -c %s "$AUTOPIPE_LOG" 2>/dev/null || echo 0) - AUTOPIPE_LOG_OFF ))
  if [ "${mono:-0}" -gt "${AUTOPIPE_LAST_MONO:-0}" ] && settled "$st" && [ "$grown" -gt 0 ]; then TICK=done; break; fi
  [ $((i % 30)) -eq 0 ] && echo "   waiting ${i}0s: state=$st started=$([ "${mono:-0}" -gt "${AUTOPIPE_LAST_MONO:-0}" ] && echo yes || echo no) log_grown=$grown"
  sleep 10
done
echo "first tick: $TICK $(systemctl --user show nhms-node27-autopipe.service -p ExecMainStartTimestamp -p ExecMainExitTimestamp -p Result --no-pager | tr '\n' ' ')"
[ "$TICK" = done ] && [ "$(systemctl --user show nhms-node27-autopipe.service -p Result --value)" = success ] || { say "RECORD: first autopipe tick not observed as success"; FINAL_RC=6; }
tail -c +$((AUTOPIPE_LOG_OFF + 1)) "$AUTOPIPE_LOG" > "$OUT/autopipe-tick.log"
echo "tick log lines=$(wc -l < "$OUT/autopipe-tick.log") seed_failed_import=$(grep -c 'seed_failed stage=import' "$OUT/autopipe-tick.log") token_unset=$(grep -c 'CACHE_WARM_TOKEN unset' "$OUT/autopipe-tick.log") prewarm_rc_lines=$(grep -c 'MVT prewarm rc=' "$OUT/autopipe-tick.log")"
grep -E 'autopipe: (start|done|phase=|MVT prewarm rc=|national MVT prewarm)' "$OUT/autopipe-tick.log" | tail -12
grep -E '^\{"base_url"' "$OUT/autopipe-tick.log" | tail -1 > "$OUT/tick-prewarm.json"
[ -s "$OUT/tick-prewarm.json" ] && jq -c '{requests_total, failed_count, cache_hits, tile_failed: (.failed_count - ([.per_source[].png_failed] | add // 0)), png_failed: ([.per_source[].png_failed] | add // 0)}' "$OUT/tick-prewarm.json"
L3=$(locks); P3=$(pbfs); echo "after first tick: locks=$L3 pbf=$P3"
T_START_EPOCH=$(date -u -d "$T_START" +%s)
for u in nhms-node27-download nhms-node27-frontier-alert; do
  st=$(systemctl --user show "$u.service" -p ExecMainStartTimestamp --value); se=$(date -d "$st" +%s 2>/dev/null || echo 0)
  if [ "$se" -ge "$T_START_EPOCH" ]; then echo "$u first run after restore: OBSERVED start=$st $(systemctl --user show "$u.service" -p Result -p ExecStart --value | sed -E 's/ ; ignore_errors=.*$//' | tr '\n' ' ')"; else echo "$u first run after restore: NOT OBSERVED in window (last start=$st < T_START=$T_START)"; fi
done

# ------------------------------------------------------- 2.11 public re-tests
say "== 2.11 public re-tests"
curl -s --max-time 20 -o "$OUT/root-public-after.html" -w 'public / : %{http_code}\n' "$PUB/"; echo "  served assets=[$(assets_of "$OUT/root-public-after.html" | tr '\n' ' ')] == built: $([ "$(assets_of "$OUT/root-public-after.html" | tr '\n' ' ')" = "$NEW_ASSETS" ] && echo yes || echo no)"
for a in $(assets_of "$OUT/root-public-after.html"); do echo "  public $a: $(probe "$PUB/$a")"; done
echo "public /api/v1/layers: $(probe "$PUB/api/v1/layers")"
echo "public /ops: $(probe "$PUB/ops")"
RUN1=$(curl -s --max-time 20 "$L/api/v1/runs?limit=1" | jq -r '.data.items[0].run_id // empty')
for inst in '9999-12-31T23:59:59-08:00' '0001-01-01T00:00:00+08:00' 'not-an-instant'; do
  r=$(curl -s --max-time 30 -o "$OUT/pub-hn.body" -w '%{http_code}' "$PUB/api/v1/tiles/hydro-national/q_down/$inst/4/12/6.pbf"); echo "public hydro-national $inst: $r $(jq -r '.error.code // empty' "$OUT/pub-hn.body" 2>/dev/null)"
  if [ -n "$RUN1" ]; then r=$(curl -s --max-time 30 -o "$OUT/pub-h.body" -w '%{http_code}' "$PUB/api/v1/tiles/hydro/$RUN1/q_down/$inst/7/100/50.pbf"); echo "public hydro/{run} $inst: $r $(jq -r '.error.code // empty' "$OUT/pub-h.body" 2>/dev/null)"; fi
done
r=$(curl -s --max-time 30 -o "$OUT/pub-off.body" -w '%{http_code}' "$PUB/api/v1/layers?offset=999999999"); echo "public offset=999999999: $r data_len=$(jq -r '.data | length' "$OUT/pub-off.body" 2>/dev/null)"
for _ in 1 2 3; do probe "$L/api/v1/runs" >/dev/null; done
echo "warm /api/v1/runs ttfb x3: $(for _ in 1 2 3; do probe "$L/api/v1/runs" | cut -d' ' -f3; done | tr '\n' ' ')"

# ------------------------------------------------------------- 2.12 wrap-up
say "== 2.12 final state"
df -h / /home /data/GHDC | tee "$OUT/df-after.txt"
capture final
if diff "$OUT/units-before.txt" "$OUT/units-final.txt" > "$OUT/units-diff.txt"; then echo "unit invariant: final == before (9 units, 0 lines)"; else cat "$OUT/units-diff.txt"; say "RECORD: unit capture changed during window"; FINAL_RC=6; fi
grep -q "WorkingDirectory=$FOREIGN_COMPRESSION_ROOT" "$OUT/units-final.txt" && echo "foreign fence: compression still bound to $FOREIGN_COMPRESSION_ROOT" || { say "RECORD: compression binding changed"; FINAL_RC=6; }
ls -la "$HOLD" > "$OUT/hold-after.txt"; diff "$OUT/hold-before.txt" "$OUT/hold-after.txt" > /dev/null && echo "capacity-hold dir: unchanged" || { echo "capacity-hold dir: CHANGED"; FINAL_RC=6; }
diff "$OUT/df-before.txt" "$OUT/df-after.txt" > /dev/null && echo "df: identical" || { echo "df: changed"; diff "$OUT/df-before.txt" "$OUT/df-after.txt"; }
echo "drop-ins remaining: $(find "$USERD" -path '*.d/*.conf' | wc -l)"
for f in "$REPO"/infra/env/*.env "$USERD"/nhms-*.service "$USERD"/nhms-*.timer; do [ -f "$f" ] && { cmp -s "$f" "$BK$f" || echo "CHANGED vs backup: $f"; }; done; echo "env/unit files vs backup: compared"
git_identity | tee "$BK/git-identity-after.txt"
echo "old branch retained: $(git rev-parse --verify -q "$OLD_BRANCH") (origin/issue1987-frozen-new-415cbd1e untouched: $(git rev-parse --verify -q origin/issue1987-frozen-new-415cbd1e))"
systemctl --user list-timers --all --no-pager | tee "$OUT/timers-after.txt" | grep -E 'NEXT|nhms-'
systemctl --user --failed --no-pager | grep -E 'nhms-(display|node27)' || echo "no failed nhms-display/node27 units"
say "DONE rc=$FINAL_RC"
exit "$FINAL_RC"
