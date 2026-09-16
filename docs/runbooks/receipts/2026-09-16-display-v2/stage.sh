#!/bin/bash
# #2017 (display-v2 I15) -- node-27 deployment window + comprehensive receipt data.
#
# What this window changes on the node, and nothing else:
#   1. appends exactly one line to infra/env/display.env: NHMS_PRECIP_MIRROR_ROOT
#      (the layout decision routed to this issue by #2162 target 1's Non-Goals),
#   2. fast-forwards /home/nwm/NWM from f2476c8c to origin/master (docs-only),
#   3. restarts nhms-display-api.service once.
# No unit file, no drop-in, no DB write, no frontend rebuild (the shipped dist is
# byte-identical to a master build -- proven in the #2162 target 2 receipt), no
# `uv run`/`uv sync` anywhere in the active tree.
#
# Cold measurements run with the autopipe/download/frontier-alert timers stopped
# so the cron's own prewarm cannot warm what this script is about to call cold.
# An EXIT trap hands the timers back on every path.
#
# Exit codes: 0 done; 2 precondition (nothing changed); 5 post-restart gate
# (rolled back: env restored, tree left on master, display restarted); 6 an
# observation after the gate failed (not rolled back -- the deployment stands).
set -u
export PATH=$HOME/.local/bin:$PATH
export TMPDIR=/home/nwm/tmp

OLD_SHA=f2476c8c29774d40b300d4a8a13657b5d6d05139
NEW_SHA=7ecc46bed18cc8b6f20030f49aa3bf43a77b6858
MIRROR_ROOT=/home/ghdc/nwm/object-store
MIRROR_KEY=NHMS_PRECIP_MIRROR_ROOT

TS=$(date -u +%Y%m%dT%H%M%SZ)
REPO=/home/nwm/NWM
OUT=/home/nwm/tmp/2017/window-$TS
BK=/home/nwm/nhms-display-v2-backup-2017-$TS
PY=$REPO/.venv/bin/python
ENVF=$REPO/infra/env/display.env
USERD=/home/nwm/.config/systemd/user
CACHE=/home/nwm/.cache/nhms/mvt
UNITS="nhms-display-api nhms-node27-autopipe nhms-node27-download nhms-node27-raw-retention nhms-node27-timeseries-retention nhms-node27-timeseries-compression nhms-node27-resource-governance nhms-node27-frontier-alert nhms-node27-mvt-cache-retention"
STOP_TIMERS="nhms-node27-autopipe.timer nhms-node27-download.timer nhms-node27-frontier-alert.timer"
L=http://127.0.0.1:8080
PUB=https://test.nwm.ac.cn
mkdir -p "$OUT" "$BK"

ts() { date -u +%FT%TZ; }
say() { echo "[$(ts)] $*"; }
# time_starttransfer is the server-side latency; size_download proves a non-empty body.
probe() { curl -s -o /dev/null -w '%{http_code} %{size_download} %{time_starttransfer}' --max-time 300 "$@"; }
capture() {
  for u in $UNITS; do
    echo "## $u"
    systemctl --user show "$u.service" -p FragmentPath -p DropInPaths -p WorkingDirectory -p ExecStartPre -p ExecStart -p ExecStartPost -p ExecStop -p Environment -p EnvironmentFiles \
      | sed -E 's/ ; ignore_errors=.*$//; s/(TOKEN|PASSWORD|SECRET)=[^ ]*/\1=<redacted>/g' | LC_ALL=C sort
  done > "$OUT/units-$1.txt"
}
git_identity() { echo "head=$(git -C "$REPO" rev-parse HEAD) branch=$(git -C "$REPO" rev-parse --abbrev-ref HEAD) porcelain=$(git -C "$REPO" status --porcelain | wc -l)"; }
wait_health() { for _ in $(seq 1 40); do curl -sf --max-time 2 "$L/health" >/dev/null 2>&1 && return 0; sleep 1; done; return 1; }

TIMERS_STOPPED=0; ENV_WRITTEN=0
restart_timers() {
  if [ "$TIMERS_STOPPED" = 1 ]; then
    systemctl --user start $STOP_TIMERS && say "timers restarted: $STOP_TIMERS"
    TIMERS_STOPPED=0
  fi
}
trap restart_timers EXIT

rollback() {
  say "ROLLBACK ($1)"
  if [ "$ENV_WRITTEN" = 1 ]; then
    cp "$BK/home/nwm/NWM/infra/env/display.env" "$ENVF" && say "rollback: display.env restored from backup" || say "rollback: env restore FAILED -- manual attention"
    ENV_WRITTEN=0
  fi
  systemctl --user restart nhms-display-api.service
  if wait_health; then say "rollback: /health ok"; else say "rollback: /health FAILED -- manual attention"; fi
  capture rollback
  say "rollback: $(git_identity)"
  exit "$2"
}

say "OUT=$OUT BK=$BK"

# ------------------------------------------------------------ 1 baseline
say "== 1 baseline + preconditions (nothing changed before step 3)"
cd "$REPO" || exit 2
git fetch -q origin || { say "precondition: git fetch failed"; exit 2; }
echo "before: $(git_identity)"
[ "$(git rev-parse HEAD)" = "$OLD_SHA" ] || { say "precondition: HEAD != $OLD_SHA"; exit 2; }
[ "$(git rev-parse --abbrev-ref HEAD)" = master ] || { say "precondition: not on master"; exit 2; }
[ "$(git status --porcelain | wc -l)" = 0 ] || { say "precondition: porcelain not empty"; git status --porcelain; exit 2; }
[ "$(git rev-parse origin/master)" = "$NEW_SHA" ] || { say "precondition: origin/master=$(git rev-parse origin/master) != expected $NEW_SHA"; exit 2; }
NONDOC=$(git diff --name-only "$OLD_SHA" "$NEW_SHA" | grep -cv '^docs/')
echo "pull-diff: files=$(git diff --name-only "$OLD_SHA" "$NEW_SHA" | wc -l) non-docs=$NONDOC"
[ "$NONDOC" = 0 ] || { say "precondition: the pull is not docs-only; a frontend/venv decision is required"; exit 2; }
[ "$(find "$USERD" -path '*.d/*.conf' | wc -l)" = 0 ] || { say "precondition: drop-ins present"; exit 2; }
grep -q "^${MIRROR_KEY}=" "$ENVF" && { say "precondition: $MIRROR_KEY already set; re-decide"; exit 2; }
[ -d "$MIRROR_ROOT/canonical" ] || { say "precondition: mirror root has no canonical/"; exit 2; }
capture before
systemctl --user list-timers --all > "$OUT/timers-before.txt" 2>&1
df -h / /home /data/GHDC > "$OUT/df-before.txt" 2>&1
{
  echo "mirror_root=$MIRROR_ROOT"
  for s in gfs IFS; do
    echo "source=$s cycles=$(ls "$MIRROR_ROOT/canonical/$s" 2>/dev/null | grep -cE '^[0-9]{10}$') oldest=$(ls "$MIRROR_ROOT/canonical/$s" | grep -E '^[0-9]{10}$' | head -1) newest=$(ls "$MIRROR_ROOT/canonical/$s" | grep -E '^[0-9]{10}$' | tail -1)"
  done
  echo "png_cache_files=$(find "$CACHE/precip" -type f -name '*.png' 2>/dev/null | wc -l)"
  echo "pbf_cache_files=$(find "$CACHE" -type f -name '*.pbf' 2>/dev/null | wc -l)"
} > "$OUT/inventory-before.txt"
cat "$OUT/inventory-before.txt"

# ------------------------------------------------------------ 2 stop timers
say "== 2 stop writer/prewarm timers (trap restores)"
systemctl --user stop $STOP_TIMERS && TIMERS_STOPPED=1 || { say "precondition: timer stop failed"; exit 2; }
for t in $STOP_TIMERS; do echo "$t active=$(systemctl --user is-active "$t")"; done

# ------------------------------------------------------------ 3 backup + env write
say "== 3 backup display.env, then append exactly one line"
( cd / && cp --parents "${ENVF#/}" "$BK/" ) || { say "backup failed"; exit 2; }
[ -f "$BK/home/nwm/NWM/infra/env/display.env" ] || { say "backup missing"; exit 2; }
BEFORE_LINES=$(wc -l < "$ENVF")
printf '\n# %s (#2017 7.2): node-27 view of the NFS canonical tree node-22 mirrors into.\n# Same tree as NODE27_RAW_RETENTION_OBJECT_STORE_ROOT; services/precip/mirror.py appends canonical/<S>/<K>/.\n%s=%s\n' "$(date -u +%F)" "$MIRROR_KEY" "$MIRROR_ROOT" >> "$ENVF" || { say "env append failed"; exit 2; }
ENV_WRITTEN=1
AFTER_LINES=$(wc -l < "$ENVF")
echo "display.env lines $BEFORE_LINES -> $AFTER_LINES; ${MIRROR_KEY} occurrences=$(grep -c "^${MIRROR_KEY}=" "$ENVF")"
[ "$(grep -c "^${MIRROR_KEY}=" "$ENVF")" = 1 ] || rollback "env key not exactly once" 5
diff <(sed -E 's/=.*$//' "$BK/home/nwm/NWM/infra/env/display.env") <(sed -E 's/=.*$//' "$ENVF") > "$OUT/envdiff-keys.txt"
echo "--- key-only diff (values never printed) ---"; cat "$OUT/envdiff-keys.txt"

# ------------------------------------------------------------ 4 fast-forward
say "== 4 git pull --ff-only"
git pull --ff-only 2>&1 | tee "$OUT/pull.log"
[ "$(git rev-parse HEAD)" = "$NEW_SHA" ] || rollback "HEAD != NEW_SHA after pull" 5
echo "after pull: $(git_identity)"

# ------------------------------------------------------------ 5 restart + gate
say "== 5 restart display + gate"
T_RESTART=$(date -u +%s)
systemctl --user restart nhms-display-api.service || rollback "restart failed" 5
wait_health || rollback "/health never came back" 5
echo "restart_seconds=$(( $(date -u +%s) - T_RESTART )) MainPID=$(systemctl --user show nhms-display-api.service -p MainPID --value)"
curl -s "$L/api/v1/runtime/config" | tr ',' '\n' | grep -iE 'service_role|display_readonly' | head -5
GFS_CYCLE=$(curl -s "$L/api/v1/layers/discharge/cycles?source=gfs" | tr '{' '\n' | sed -n 's/.*"cycle_time":"\([^"]*\)".*/\1/p' | head -1)
IFS_CYCLE=$(curl -s "$L/api/v1/layers/discharge/cycles?source=ifs" | tr '{' '\n' | sed -n 's/.*"cycle_time":"\([^"]*\)".*/\1/p' | head -1)
echo "latest cycles: gfs=$GFS_CYCLE ifs=$IFS_CYCLE"
[ -n "$GFS_CYCLE" ] && [ -n "$IFS_CYCLE" ] || rollback "no cycles listed" 5
for s in gfs ifs; do
  c=$GFS_CYCLE; [ "$s" = ifs ] && c=$IFS_CYCLE
  r=$(probe "$L/api/v1/precip/$s/$c/index")
  echo "gate precip index $s $c -> $r"
  case "$r" in 200\ *) ;; *) rollback "precip index $s not 200 ($r)" 5;; esac
done
echo "$GFS_CYCLE" > "$OUT/gfs-cycle.txt"; echo "$IFS_CYCLE" > "$OUT/ifs-cycle.txt"
say "GATE PASSED -- deployment stands from here; later failures are recorded, not rolled back"

# ------------------------------------------------------------ 6 cold read surface
say "== 6 cold measurements (timers still stopped)"
# The refresh header is only privileged when its value equals NHMS_DISPLAY_CACHE_WARM_TOKEN
# (#2079). Read it from the env file in a subshell; it is never echoed or stored.
WARM=$(sed -n 's/^NHMS_DISPLAY_CACHE_WARM_TOKEN=//p' "$ENVF" | tr -d '"' | tail -1)
[ -n "$WARM" ] && echo "warm token: configured (value not printed)" || echo "warm token: NOT configured -- refresh header will be ignored"

say "-- 6.1 /api/v1/layers cold p95 (n=12, refresh header, serial)"
: > "$OUT/layers-cold.txt"
for i in $(seq 1 12); do
  curl -s -o /dev/null -w '%{time_starttransfer}\n' --max-time 120 -H "x-nhms-cache-warm: $WARM" "$L/api/v1/layers" >> "$OUT/layers-cold.txt"
done
sort -n "$OUT/layers-cold.txt" -o "$OUT/layers-cold.txt"
N=$(wc -l < "$OUT/layers-cold.txt")
P95_IDX=$(( (N * 95 + 99) / 100 ))
echo "layers cold n=$N p95=$(sed -n "${P95_IDX}p" "$OUT/layers-cold.txt") median=$(sed -n "$(( (N+1)/2 ))p" "$OUT/layers-cold.txt") min=$(head -1 "$OUT/layers-cold.txt") max=$(tail -1 "$OUT/layers-cold.txt")" | tee "$OUT/layers-p95.txt"
echo "layers precip entry present: $(curl -s "$L/api/v1/layers" | grep -c '"layer_id":"precip"')"

say "-- 6.2 per-source valid-times clamp (first item vs cycle)"
: > "$OUT/valid-times.txt"
for s in gfs ifs; do
  c=$GFS_CYCLE; [ "$s" = ifs ] && c=$IFS_CYCLE
  body=$(curl -s -H "x-nhms-cache-warm: $WARM" "$L/api/v1/layers/discharge/valid-times?source=$s&cycle=$c")
  first=$(echo "$body" | sed -n 's/.*"valid_times":\["\([^"]*\)".*/\1/p')
  count=$(echo "$body" | grep -o '"[0-9]\{4\}-[0-9][0-9]-[0-9][0-9]T[0-9:]*Z"' | wc -l)
  echo "source=$s cycle=$c first_valid_time=$first valid_times_available=$count" >> "$OUT/valid-times.txt"
done
cat "$OUT/valid-times.txt"

say "-- 6.3 precip index + PNG cold/hot/304 (both sources, lead 0 and lead 24h)"
: > "$OUT/precip-probe.txt"
for s in gfs ifs; do
  c=$GFS_CYCLE; [ "$s" = ifs ] && c=$IFS_CYCLE
  echo "== $s $c" >> "$OUT/precip-probe.txt"
  echo "index cold: $(probe "$L/api/v1/precip/$s/$c/index")" >> "$OUT/precip-probe.txt"
  echo "index hot : $(probe "$L/api/v1/precip/$s/$c/index")" >> "$OUT/precip-probe.txt"
  curl -s "$L/api/v1/precip/$s/$c/index" > "$OUT/precip-index-$s.json"
  vt0=$(sed -n 's/.*"valid_times":\["\([^"]*\)".*/\1/p' "$OUT/precip-index-$s.json")
  vt24=$(grep -o '"[0-9]\{4\}-[0-9][0-9]-[0-9][0-9]T[0-9:]*Z"' "$OUT/precip-index-$s.json" | sed -n '9p' | tr -d '"')
  echo "valid_times_count=$(grep -o '"[0-9]\{4\}-[0-9][0-9]-[0-9][0-9]T[0-9:]*Z"' "$OUT/precip-index-$s.json" | wc -l) first=$vt0 lead24=$vt24" >> "$OUT/precip-probe.txt"
  for vt in "$vt0" "$vt24"; do
    [ -n "$vt" ] || continue
    hf=$OUT/png-$s-$vt.hdr
    echo "png cold $vt: $(probe -D "$hf" "$L/api/v1/precip/$s/$c/$vt.png") cache=$(grep -i '^x-tile-cache:' "$hf" | tr -d '\r' | cut -d' ' -f2)" >> "$OUT/precip-probe.txt"
    etag=$(grep -i '^etag:' "$hf" | tr -d '\r' | cut -d' ' -f2-)
    echo "png hot  $vt: $(probe "$L/api/v1/precip/$s/$c/$vt.png")" >> "$OUT/precip-probe.txt"
    echo "png 304  $vt: $(probe -H "If-None-Match: $etag" "$L/api/v1/precip/$s/$c/$vt.png")" >> "$OUT/precip-probe.txt"
  done
done
cat "$OUT/precip-probe.txt"

say "-- 6.4 river-network-national tile cold/hot per zoom (cache-busting is not possible; cold = first touch this window)"
: > "$OUT/river-tiles.txt"
for zxy in "3/5/2" "4/12/6" "5/24/12" "6/50/24" "7/100/49"; do
  z=${zxy%%/*}; rest=${zxy#*/}; x=${rest%%/*}; y=${rest##*/}
  echo "z$z/$x/$y cold: $(probe "$L/api/v1/tiles/river-network-national/$z/$x/$y.pbf") hot: $(probe "$L/api/v1/tiles/river-network-national/$z/$x/$y.pbf")" >> "$OUT/river-tiles.txt"
done
cat "$OUT/river-tiles.txt"

say "-- 6.5 hydro-national discharge tile cold/hot (both sources, z4)"
: > "$OUT/discharge-tiles.txt"
for s in gfs ifs; do
  c=$GFS_CYCLE; [ "$s" = ifs ] && c=$IFS_CYCLE
  vt=$(sed -n "s/^source=$s .*first_valid_time=\([^ ]*\).*/\1/p" "$OUT/valid-times.txt")
  [ -n "$vt" ] || continue
  u="$L/api/v1/tiles/hydro-national/$s/$c/q_down/$vt/4/12/6.pbf"
  echo "$s z4/12/6 $vt cold: $(probe "$u") hot: $(probe "$u")" >> "$OUT/discharge-tiles.txt"
done
cat "$OUT/discharge-tiles.txt"

say "-- 6.6 public entry (nginx) spot check"
{
  echo "/            $(probe "$PUB/")"
  echo "/ops         $(probe "$PUB/ops")"
  echo "/api/v1/layers $(probe "$PUB/api/v1/layers")"
  echo "precip index $(probe "$PUB/api/v1/precip/gfs/$GFS_CYCLE/index")"
} > "$OUT/public-probe.txt"
cat "$OUT/public-probe.txt"

# ------------------------------------------------------------ 7 prewarm
say "== 7 prewarm (cron invocation verbatim)"
echo "AUTOPIPE_MVT_PREWARM_WORKERS=${AUTOPIPE_MVT_PREWARM_WORKERS:-<unset, cron falls back to 8>} nproc=$(nproc)"
PW_START=$(date -u +%s)
"$PY" "$REPO/scripts/node27_mvt_prewarm.py" --zooms 3,4,5 --workers "${AUTOPIPE_MVT_PREWARM_WORKERS:-8}" > "$OUT/prewarm.json" 2> "$OUT/prewarm.err"
PW_RC=$?
echo "prewarm rc=$PW_RC wall_seconds=$(( $(date -u +%s) - PW_START ))"
tail -3 "$OUT/prewarm.err" 2>/dev/null
head -c 2000 "$OUT/prewarm.json"; echo

# ------------------------------------------------------------ 8 capacity + keep watermark
say "== 8 capacity, cache inventory, keep watermark"
df -h / /home /data/GHDC > "$OUT/df-after.txt" 2>&1
cat "$OUT/df-after.txt"
echo "cache volume: $(df -h "$CACHE" | tail -1)"
{
  echo "png_cache_files=$(find "$CACHE/precip" -type f -name '*.png' 2>/dev/null | wc -l)"
  echo "pbf_cache_files=$(find "$CACHE" -type f -name '*.pbf' 2>/dev/null | wc -l)"
  echo "cache_du=$(du -sh "$CACHE" 2>/dev/null | cut -f1) precip_du=$(du -sh "$CACHE/precip" 2>/dev/null | cut -f1)"
  for s in gfs IFS; do
    echo "png_dirs_$s=$(ls "$CACHE/precip/$s" 2>/dev/null | wc -l) mirror_cycles_$s=$(ls "$MIRROR_ROOT/canonical/$s" | grep -cE '^[0-9]{10}$')"
    for d in $(ls "$CACHE/precip/$s" 2>/dev/null); do
      [ -d "$MIRROR_ROOT/canonical/$s/$d" ] || echo "ORPHAN_PNG_DIR $s/$d (no mirrored cycle)"
    done
  done
} > "$OUT/inventory-after.txt"
cat "$OUT/inventory-after.txt"

# keep watermark inequality, read-only, per source. DSN comes from display.env and is never printed.
DB=$(sed -n 's/^DATABASE_URL=//p' "$ENVF" | tr -d '"' | tail -1)
RET_DAYS=$(sed -n 's/^NODE27_RAW_RETENTION_DAYS=//p' "$REPO/infra/env/node27-raw-retention.env" | tail -1)
{
  echo "retention_days=$RET_DAYS"
  echo "display_watermark=$(psql "$DB" -At -c "SELECT MAX(cycle_time) FROM hydro.hydro_run WHERE run_type='forecast' AND status IN ('succeeded','parsed','published') AND cycle_time IS NOT NULL;")"
  for s in gfs ifs; do
    S=$s; [ "$s" = ifs ] && S=IFS
    oldest=$(ls "$MIRROR_ROOT/canonical/$S" | grep -E '^[0-9]{10}$' | head -1)
    echo "source=$s oldest_mirrored_cycle=$oldest api_oldest_listed=$(curl -s "$L/api/v1/layers/discharge/cycles?source=$s" | tr '{' '\n' | sed -n 's/.*"cycle_time":"\([^"]*\)".*/\1/p' | tail -1)"
  done
} > "$OUT/keep-watermark.txt" 2>&1
cat "$OUT/keep-watermark.txt"

# ------------------------------------------------------------ 9 timers back + final capture
say "== 9 timers back, final unit capture"
restart_timers
systemctl --user list-timers --all > "$OUT/timers-after.txt" 2>&1
capture final
if diff -u "$OUT/units-before.txt" "$OUT/units-final.txt" > "$OUT/units-diff.txt"; then
  say "9-unit effective configuration: IDENTICAL before == final"
else
  say "9-unit capture DIFFERS -- see units-diff.txt"; head -40 "$OUT/units-diff.txt"
fi
systemctl --user list-units --state=failed --no-legend > "$OUT/failed-units.txt" 2>&1
echo "failed units: $(wc -l < "$OUT/failed-units.txt")"; cat "$OUT/failed-units.txt"
say "DONE OUT=$OUT BK=$BK $(git_identity)"
