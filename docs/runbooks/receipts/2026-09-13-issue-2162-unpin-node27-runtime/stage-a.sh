#!/bin/bash
# #2162 target 1 -- unpin the 8 node-27 user units from the frozen copy
# (/home/nwm/NWM-reslice-original-5a86841c) back to the active tree
# (/home/nwm/NWM = a8db554d), deploy the #2032 MVT cache-retention lane, and
# produce every reading the receipt needs. Self-contained: every gate and the
# all-or-nothing rollback run remote-side, so a dropped ssh cannot strand a
# half-moved set. Exit codes: 0 done; 2 precondition; 3 unit-diff gate (rolled
# back); 4 token/env; 5 display gate (rolled back); 6 retention lane.
set -u
export PATH=$HOME/.local/bin:$PATH
export TMPDIR=/home/nwm/tmp

TS=$(date -u +%Y%m%dT%H%M%SZ)
OUT=/home/nwm/tmp/2162/out-$TS
BK=/home/nwm/nhms-unpin-backup-2162-$TS
REPO=/home/nwm/NWM
FROZEN=/home/nwm/NWM-reslice-original-5a86841c
PY=$REPO/.venv/bin/python
USERD=/home/nwm/.config/systemd/user
PIN=60-reslice-pin-original-5a86841c.conf
UNITS="nhms-display-api nhms-node27-autopipe nhms-node27-download nhms-node27-raw-retention nhms-node27-timeseries-retention nhms-node27-timeseries-compression nhms-node27-resource-governance nhms-node27-frontier-alert"
CACHE=/home/nwm/.cache/nhms/mvt
HOLD=/home/nwm/.local/state/nhms-pgdata-pr-2240-capacity-hold
RET_ENV=$REPO/infra/env/node27-mvt-cache-retention.env
RET_LOGS=/home/nwm/node27-mvt-cache-retention-logs
# per-tick log = ${AUTOPIPE_LOG_FILE:-$AUTOPIPE_LOG_ROOT/autopipe.log} (scripts/node27_autopipe_cron.sh:181); resolve from the ingest env, values never echoed
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
# probe URL [HEADER_FILE|""] [extra curl args...] -> "status bytes ttfb(s)"
probe() {
  local url=$1 hf=${2:-}
  shift $(( $# >= 2 ? 2 : 1 ))
  curl -s -o /dev/null ${hf:+-D "$hf"} -w '%{http_code} %{size_download} %{time_starttransfer}' --max-time 120 "$@" "$url"
}
hdr() { grep -i "^$1:" "$2" | head -1 | cut -d' ' -f2- | tr -d '\r'; }

TIMERS_STOPPED=0; PINS_REMOVED=0; TOKEN_WRITTEN=0; RET_INSTALLED=0
restart_timers() {
  if [ "$TIMERS_STOPPED" = 1 ]; then
    systemctl --user start nhms-node27-autopipe.timer nhms-node27-download.timer
    TIMERS_STOPPED=0; say "writer timers started (EXIT trap or step 14)"
  fi
}
trap restart_timers EXIT

# Effective configuration of the 8 units. ExecStart's runtime suffix
# (start_time/pid/status) is stripped; secrets redacted.
capture() {
  for u in $UNITS; do
    echo "## $u"
    systemctl --user show "$u.service" -p FragmentPath -p DropInPaths -p WorkingDirectory -p ExecStartPre -p ExecStart -p ExecStartPost -p ExecStop -p Environment -p EnvironmentFiles \
      | sed -E 's/ ; ignore_errors=.*$//; s/(TOKEN|PASSWORD|SECRET)=[^ ]*/\1=<redacted>/g' | LC_ALL=C sort
  done > "$OUT/units-$1.txt"
}
wait_health() {
  for _ in $(seq 1 30); do curl -sf --max-time 2 "$L/health" >/dev/null 2>&1 && return 0; sleep 1; done
  return 1
}
rollback() {
  say "ROLLBACK ($1)"
  if [ "$RET_INSTALLED" = 1 ]; then
    systemctl --user disable --now nhms-node27-mvt-cache-retention.timer 2>/dev/null
    rm -f "$USERD/nhms-node27-mvt-cache-retention.service" "$USERD/nhms-node27-mvt-cache-retention.timer" "$RET_ENV"
  fi
  if [ "$TOKEN_WRITTEN" = 1 ]; then
    cp -p "$BK$REPO/infra/env/display.env" "$REPO/infra/env/display.env"
    cp -p "$BK$REPO/infra/env/node27-ingest.env" "$REPO/infra/env/node27-ingest.env"
  fi
  if [ "$PINS_REMOVED" = 1 ]; then
    for u in $UNITS; do mkdir -p "$USERD/$u.service.d"; cp -p "$BK$USERD/$u.service.d/$PIN" "$USERD/$u.service.d/$PIN"; done
  fi
  systemctl --user daemon-reload
  if [ -n "${T_RESTART:-}" ]; then
    systemctl --user restart nhms-display-api.service
    if wait_health; then say "rollback: /health ok"; else say "rollback: /health FAILED -- manual attention"; fi
  else
    say "rollback: display never restarted in this window -- pinned process untouched, no restart needed"
  fi
  capture rollback
  say "rollback: display WorkingDirectory=$(systemctl --user show nhms-display-api.service -p WorkingDirectory --value)"
  exit "$2"
}

say "OUT=$OUT BK=$BK"

# ---------------------------------------------------------------- 1.1 baseline
say "== 1.1 baseline"
cd "$REPO" || exit 2
echo "head=$(git rev-parse --short HEAD) branch=$(git rev-parse --abbrev-ref HEAD) porcelain=$(git status --porcelain | wc -l)"
echo "dep-diff-lines=$(git diff a8db554d HEAD -- pyproject.toml uv.lock .python-version | wc -l)"
echo "venv=$("$PY" -V 2>&1)"
for u in $UNITS; do [ -f "$USERD/$u.service.d/$PIN" ] || { say "precondition: pin missing for $u"; exit 2; }; done
[ "$(find "$USERD" -path '*.d/*.conf' | wc -l)" = 8 ] || { say "precondition: drop-in count != 8"; find "$USERD" -path '*.d/*.conf'; exit 2; }
md5sum "$USERD"/*.d/"$PIN" > "$OUT/pins-md5.txt"
capture before
MAIN0=$(systemctl --user show nhms-display-api.service -p MainPID --value)
UVPIDS=$(pgrep -f 'python -m uvicorn apps.api.main:app --host 127.0.0.1 --port 8080' | sort -n | tr '\n' ' ')
echo "display MainPID=$MAIN0 uvicorn_8080_pids=[$UVPIDS] ActiveState=$(systemctl --user is-active nhms-display-api.service) cwd=$(readlink /proc/"$MAIN0"/cwd) cmd=$(tr '\0' ' ' < /proc/"$MAIN0"/cmdline | cut -c1-80)"
# MainPID must be one of the :8080 uvicorn processes and every other one must be its child (workers inherit the listening fd).
OK=0; for p in $UVPIDS; do [ "$p" = "$MAIN0" ] && OK=1; done
[ "$OK" = 1 ] && [ "$MAIN0" != 0 ] || { say "precondition: unit MainPID $MAIN0 is not among :8080 uvicorn pids [$UVPIDS] (detached orphan?)"; exit 2; }
for p in $UVPIDS; do [ "$p" = "$MAIN0" ] && continue; pp=$(awk '/^PPid:/{print $2}' /proc/"$p"/status 2>/dev/null); [ "$pp" = "$MAIN0" ] || { say "precondition: :8080 pid $p (ppid $pp) is not a child of MainPID $MAIN0"; exit 2; }; done
for t in nhms-node27-raw-retention nhms-node27-timeseries-compression nhms-node27-timeseries-retention nhms-node27-resource-governance nhms-node27-download nhms-node27-frontier-alert nhms-node27-autopipe; do echo "timer $t: $(systemctl --user show "$t.timer" -p TimersCalendar -p TimersMonotonic -p NextElapseUSecRealtime --value | tr '\n' ' ')"; done
echo "display env: $(tr '\0' '\n' < /proc/"$MAIN0"/environ | grep -E '^NHMS_MVT_FILE_CACHE_DIR=' ) token_key=$(tr '\0' '\n' < /proc/"$MAIN0"/environ | grep -c '^NHMS_DISPLAY_CACHE_WARM_TOKEN=')"
YD0=$(pgrep -f '^/home/nwm/yd-NWM/.venv/bin/python -m uvicorn' | sort -n | head -1)
echo "yd :8081 pid=$YD0 health=$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 http://127.0.0.1:8081/health)"
L0=$(locks); P0=$(pbfs); echo "cache locks=$L0 pbf=$P0 size=$(du -sh "$CACHE" | cut -f1)"
df -h / /home /data/GHDC | tee "$OUT/df-before.txt"
set -a; . "$REPO/infra/env/display.env"; set +a
psql "$DATABASE_URL" -Atc "select version, applied_at from public.schema_migrations where version >= '000056' order by 1" | tee "$OUT/ledger.txt"
ls -la "$HOLD" > "$OUT/hold-before.txt"; cat "$OUT/hold-before.txt"
echo "hold resume-approved present=$([ -e "$HOLD/resume-approved" ] && echo yes || echo no); 91-fences=$(find "$USERD" -name '91-*' | wc -l)"
for u in nhms-node27-raw-retention nhms-node27-timeseries-compression; do diff "$REPO/infra/systemd/$u.service" "$USERD/$u.service" > "$OUT/unitdiff-$u.txt"; echo "unit diff $u: $(wc -l < "$OUT/unitdiff-$u.txt") lines"; done
echo "local  /api/v1/layers: $(probe "$L/api/v1/layers")"
echo "public /api/v1/layers: $(probe "$PUB/api/v1/layers")"
echo "local  river-network-national/5/25/12 (pre): $(probe "$L/api/v1/tiles/river-network-national/5/25/12.pbf" "$OUT/rn-pre.h") etag=$(hdr etag "$OUT/rn-pre.h") cache=$(hdr x-tile-cache "$OUT/rn-pre.h")"
echo "public offset=999999999 (pre, expect 500): $(probe "$PUB/api/v1/layers?offset=999999999")"
RUN0=$(curl -s --max-time 20 "$L/api/v1/runs?limit=1" | jq -r '.data.items[0].run_id // empty')
echo "run_id sample=$RUN0"
for inst in '9999-12-31T23:59:59-08:00' '0001-01-01T00:00:00+08:00' 'not-an-instant'; do
  echo "public pre hydro-national $inst: $(probe "$PUB/api/v1/tiles/hydro-national/q_down/$inst/4/12/6.pbf")"
  [ -n "$RUN0" ] && echo "public pre hydro/{run} $inst: $(probe "$PUB/api/v1/tiles/hydro/$RUN0/q_down/$inst/7/100/50.pbf")"
done
echo "frontier-alert last: $(systemctl --user show nhms-node27-frontier-alert.service -p ExecMainStartTimestamp -p Result --value | tr '\n' ' ')"

# ------------------------------------------------------------ 1.3 writer fence
say "== 1.3 stop writer timers"
T_STOP=$(ts)
systemctl --user stop nhms-node27-autopipe.timer nhms-node27-download.timer; TIMERS_STOPPED=1
# 30 min cap; on expiry abort BEFORE any change (exit 2, trap restarts the timers).
for i in $(seq 1 180); do
  s=$(systemctl --user is-active nhms-node27-autopipe.service); d=$(systemctl --user is-active nhms-node27-download.service)
  if settled "$s" && settled "$d"; then echo "settled after $((i*10))s: autopipe=$s download=$d"; break; fi
  [ $((i % 6)) -eq 0 ] && echo "   still draining ${i}0s: autopipe=$s download=$d"
  sleep 10
done
settled "$(systemctl --user is-active nhms-node27-autopipe.service)" && settled "$(systemctl --user is-active nhms-node27-download.service)" \
  || { say "writers did not settle in 30 min -- abort before any change (window not opened)"; exit 2; }
echo "autopipe per-tick log: $AUTOPIPE_LOG ($(stat -c %s "$AUTOPIPE_LOG" 2>/dev/null || echo MISSING) bytes)"
AUTOPIPE_LOG_OFF=$(stat -c %s "$AUTOPIPE_LOG" 2>/dev/null || echo 0)
AUTOPIPE_LAST_MONO=$(systemctl --user show nhms-node27-autopipe.service -p ExecMainStartTimestampMonotonic --value)

# ------------------------------------------------------------------ 1.4 backup
say "== 1.4 backup -> $BK"
mkdir -m 0700 "$BK" || exit 2
for u in $UNITS; do cp --parents -p "$USERD/$u.service.d/$PIN" "$BK/" || exit 2; done
cp --parents -p "$REPO/infra/env/display.env" "$REPO/infra/env/node27-ingest.env" "$BK/" || exit 2
for u in $UNITS; do cmp -s "$USERD/$u.service.d/$PIN" "$BK$USERD/$u.service.d/$PIN" || { say "backup mismatch $u"; exit 2; }; done
find "$BK" -type f | sort | tee "$OUT/backup-list.txt"

# ------------------------------------------------------- 1.5 unpin + unit gate
say "== 1.5 remove 8 pins, daemon-reload, mechanical diff"
for u in $UNITS; do rm "$USERD/$u.service.d/$PIN"; rmdir "$USERD/$u.service.d" 2>/dev/null; done
PINS_REMOVED=1
systemctl --user daemon-reload
capture after
sed -E "s#$FROZEN#$REPO#g" "$OUT/units-before.txt" \
  | sed -E '/^Environment=/{ s/ ?(PYTHONPATH|NODE27_[A-Z_]+_(REPO|REPO_ROOT|ENV_FILE))=[^ ]*//g; s/  +/ /g; s/= /=/; s/ $//; }' \
  | sed -E 's/^DropInPaths=.*/DropInPaths=/' > "$OUT/units-expected.txt"
if diff "$OUT/units-expected.txt" "$OUT/units-after.txt" > "$OUT/units-diff.txt"; then
  echo "unit gate: expected == after (8 units, 0 residual lines)"
else
  # Allowed residual, and only this: a directive the pin had reset (ExecStart= /
  # EnvironmentFile=) that the base unit file carries a second copy of, reappearing.
  # Every '>' line must be ExecStart=/EnvironmentFiles= whose path is literally in the
  # unit's FragmentPath file; any '<' line (something lost) or other '>' line fails.
  cat "$OUT/units-diff.txt"
  BAD=0
  # walk the after-file to know which unit each diff line belongs to
  awk -v diff="$OUT/units-diff.txt" '
    BEGIN { while ((getline l < diff) > 0) if (l ~ /^[<>] /) extra[++n] = l }
    /^## / { unit=$2; next }
    { for (i = 1; i <= n; i++) if (substr(extra[i], 3) == $0 && extra[i] ~ /^> /) print unit "\t" $0 }
  ' "$OUT/units-after.txt" > "$OUT/units-residual.txt"
  grep -c '^< ' "$OUT/units-diff.txt" | grep -q '^0$' || BAD=1
  [ "$(grep -c '^> ' "$OUT/units-diff.txt")" = "$(wc -l < "$OUT/units-residual.txt")" ] || BAD=1
  while IFS=$'\t' read -r unit line; do
    frag=$(systemctl --user show "$unit.service" -p FragmentPath --value)
    case "$line" in
      ExecStart=*|ExecStartPre=*|EnvironmentFiles=*)
        path=$(printf '%s' "$line" | sed -E 's/^ExecStart(Pre)?=\{ path=([^ ]+) ;.*$/\2/; s/^EnvironmentFiles=([^ ]+).*$/\1/')
        grep -qF "$path" "$frag" && echo "residual ok: $unit $line (path in $frag)" || { echo "residual BAD: $unit $line"; BAD=1; };;
      *) echo "residual BAD: $unit $line"; BAD=1;;
    esac
  done < "$OUT/units-residual.txt"
  [ "$BAD" = 0 ] && echo "unit gate: residual lines all base-unit-origin ExecStart/EnvironmentFiles" || rollback "unit diff residual not allowed" 3
fi
for e in resource-governance:NODE27_RESOURCE_GOVERNANCE_REPO_ROOT timeseries-compression:NODE27_TIMESERIES_COMPRESSION_REPO_ROOT timeseries-retention:NODE27_TIMESERIES_RETENTION_REPO cold-residency:NODE27_COLD_RESIDENCY_REPO_ROOT; do
  f=${e%%:*}; k=${e##*:}
  grep -q "^$k=$REPO\$" "$REPO/infra/env/node27-$f.env" && echo "env $f: $k=$REPO" || rollback "env $f lacks $k=$REPO" 3
done
echo "remaining drop-ins under $USERD: $(find "$USERD" -path '*.d/*.conf' | wc -l)"

# ------------------------------------------------------------------- 1.6 token
say "== 1.6 NHMS_DISPLAY_CACHE_WARM_TOKEN"
for f in display node27-ingest; do
  [ "$(grep -c '^NHMS_DISPLAY_CACHE_WARM_TOKEN=' "$REPO/infra/env/$f.env")" = 0 ] || rollback "token key already present in $f.env" 4
done
# /usr/local/bin/openssl on node-27 is linked against a libssl it cannot find (attempt 1 rolled back here);
# use the distro binary by absolute path, fall back to the kernel CSPRNG. Which one was used is logged; the value never is.
if TOK=$(/usr/bin/openssl rand -hex 32 2>/dev/null) && [ ${#TOK} -eq 64 ]; then
  echo "token source: /usr/bin/openssl rand -hex 32 ($(/usr/bin/openssl version 2>/dev/null))"
else
  TOK=$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')
  echo "token source: /dev/urandom via od (/usr/bin/openssl unusable)"
fi
[ ${#TOK} -eq 64 ] || rollback "token generation (length ${#TOK})" 4
printf 'NHMS_DISPLAY_CACHE_WARM_TOKEN=%s\n' "$TOK" >> "$REPO/infra/env/display.env"
printf 'NHMS_DISPLAY_CACHE_WARM_TOKEN=%s\n' "$TOK" >> "$REPO/infra/env/node27-ingest.env"
TOKEN_WRITTEN=1
h1=$(grep '^NHMS_DISPLAY_CACHE_WARM_TOKEN=' "$REPO/infra/env/display.env" | sha256sum | cut -c1-64)
h2=$(grep '^NHMS_DISPLAY_CACHE_WARM_TOKEN=' "$REPO/infra/env/node27-ingest.env" | sha256sum | cut -c1-64)
[ "$h1" = "$h2" ] || rollback "token lines differ" 4
echo "token: written to both, lines equal, modes=$(stat -c %a "$REPO/infra/env/display.env") $(stat -c %a "$REPO/infra/env/node27-ingest.env")"
unset TOK

# ---------------------------------------------------- 1.7 retention lane files
say "== 1.7 install retention env + units"
install -m 0600 "$REPO/infra/env/node27-mvt-cache-retention.example" "$RET_ENV"
install -m 0644 "$REPO/infra/systemd/nhms-node27-mvt-cache-retention.service" "$USERD/nhms-node27-mvt-cache-retention.service"
install -m 0644 "$REPO/infra/systemd/nhms-node27-mvt-cache-retention.timer" "$USERD/nhms-node27-mvt-cache-retention.timer"
RET_INSTALLED=1
systemctl --user daemon-reload
[ ! -L "$RET_ENV" ] && [ "$(stat -c %a "$RET_ENV")" = 600 ] && grep -q "^NHMS_MVT_FILE_CACHE_DIR=$CACHE\$" "$RET_ENV" \
  && [ "$(grep -cE '^NODE27_MVT_CACHE_RETENTION_(ENABLED|PLAN_ONLY)=' "$RET_ENV")" = 0 ] \
  && echo "retention env: regular file, 600, NHMS_MVT_FILE_CACHE_DIR=$CACHE, ENABLED/PLAN_ONLY lines commented" || rollback "retention env preconditions" 6

# --------------------------------------------------------- 1.8 display restart
say "== 1.8 restart display (systemd branch of start-display-api.sh, by hand)"
T_RESTART=$(ts)
systemctl --user restart nhms-display-api.service
if wait_health; then echo "/health ok at $(ts)"; else rollback "no /health within 30s" 5; fi
MAIN=$(systemctl --user show nhms-display-api.service -p MainPID --value)
CWD=$(readlink /proc/"$MAIN"/cwd); CMD=$(tr '\0' ' ' < /proc/"$MAIN"/cmdline)
echo "display MainPID=$MAIN cwd=$CWD cmd=$CMD"
[ "$CWD" = "$REPO" ] || rollback "display cwd $CWD" 5
case "$CMD" in "$REPO/.venv/bin/python -m uvicorn apps.api.main:app"*) ;; *) rollback "display cmdline not the active tree" 5;; esac
echo "display env: $(tr '\0' '\n' < /proc/"$MAIN"/environ | grep -E '^NHMS_MVT_FILE_CACHE_DIR=') token_key=$(tr '\0' '\n' < /proc/"$MAIN"/environ | grep -c '^NHMS_DISPLAY_CACHE_WARM_TOKEN=')"
tr '\0' '\n' < /proc/"$MAIN"/environ | grep -q "^NHMS_MVT_FILE_CACHE_DIR=$CACHE\$" || rollback "cache dir differs in process" 5
tr '\0' '\n' < /proc/"$MAIN"/environ | grep -q '^NHMS_DISPLAY_CACHE_WARM_TOKEN=' || rollback "token not in process env" 5
SMOKE=$(curl -s --max-time 10 "$L/api/v1/models?limit=1")
BASIN=$(printf '%s' "$SMOKE" | jq -r '.data.items[0].basin_id // empty')
[ -n "$BASIN" ] && [ "$BASIN" != null ] && echo "smoke: basin_id=$BASIN" || rollback "basin_id smoke" 5
YD1=$(pgrep -f '^/home/nwm/yd-NWM/.venv/bin/python -m uvicorn' | sort -n | head -1)
echo "yd :8081 pid=$YD1 (before $YD0) health=$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 http://127.0.0.1:8081/health)"
[ "$YD1" = "$YD0" ] || say "WARNING: yd pid changed"

# ---------------------------------------------------------- 1.9 read surfaces
say "== 1.9 read-surface gate on :8080"
FAIL=0
r=$(probe "$L/api/v1/layers"); echo "/api/v1/layers: $r"; [ "${r%% *}" = 200 ] || FAIL=1
r=$(probe "$L/api/v1/runs"); echo "/api/v1/runs: $r"; [ "${r%% *}" = 200 ] || FAIL=1
CYC_HTTP=$(curl -s --max-time 60 -o "$OUT/cycles-gfs.json" -w '%{http_code}' "$L/api/v1/layers/discharge/cycles?source=gfs")
CYC=$(jq -r '.data.cycles[0].cycle_time // empty' "$OUT/cycles-gfs.json"); VT=$(jq -r '.data.cycles[0].valid_time_start // empty' "$OUT/cycles-gfs.json")
SRC=gfs
if [ -z "$CYC" ]; then
  CYC_HTTP=$(curl -s --max-time 60 -o "$OUT/cycles-ifs.json" -w '%{http_code}' "$L/api/v1/layers/discharge/cycles?source=ifs")
  CYC=$(jq -r '.data.cycles[0].cycle_time // empty' "$OUT/cycles-ifs.json"); VT=$(jq -r '.data.cycles[0].valid_time_start // empty' "$OUT/cycles-ifs.json"); SRC=ifs
fi
echo "cycles: http=$CYC_HTTP source=$SRC cycle=$CYC vt=$VT default_cycle=$(jq -r '.data.default_cycle' "$OUT/cycles-$SRC.json")"
[ "$CYC_HTTP" = 200 ] || FAIL=1
[ -z "$VT" ] && VT=$(date -u +%Y-%m-%dT%H:00:00Z)
r=$(probe "$L/api/v1/tiles/river-network-national/5/25/12.pbf" "$OUT/rn-post.h"); echo "river-network-national/5/25/12: $r etag=$(hdr etag "$OUT/rn-post.h") cache=$(hdr x-tile-cache "$OUT/rn-post.h")"; [ "${r%% *}" = 200 ] || FAIL=1
r=$(probe "$L/api/v1/tiles/hydro-national/q_down/$VT/4/12/6.pbf" "$OUT/hn-post.h"); echo "hydro-national/q_down/$VT/4/12/6: $r cache=$(hdr x-tile-cache "$OUT/hn-post.h")"; case "${r%% *}" in 200|424) ;; *) FAIL=1;; esac
if [ -n "$CYC" ]; then
  r=$(probe "$L/api/v1/tiles/hydro-national/$SRC/$CYC/q_down/$VT/4/12/6.pbf" "$OUT/hnc-post.h"); echo "hydro-national/$SRC/$CYC/q_down/$VT/4/12/6: $r cache=$(hdr x-tile-cache "$OUT/hnc-post.h")"; case "${r%% *}" in 200|424) ;; *) FAIL=1;; esac
else
  echo "hydro-national/{source}/{cycle}: N/A (no cycle in 12-day window for gfs/ifs)"
fi
[ "$FAIL" = 0 ] || rollback "read-surface gate" 5
echo "read-surface gate: zero 500"

# -------------------------------------------------------- 1.10 prewarm locks
say "== 1.10 prewarm lock counts (writers still stopped)"
L1=$(locks); P1=$(pbfs); echo "before prewarm: locks=$L1 pbf=$P1"
T_PW0=$(date +%s)
NHMS_DISPLAY_CACHE_WARM_TOKEN="$(grep '^NHMS_DISPLAY_CACHE_WARM_TOKEN=' "$REPO/infra/env/node27-ingest.env" | cut -d= -f2-)" \
  "$PY" "$REPO/scripts/node27_mvt_prewarm.py" --zooms 3,4,5 --workers 8 > "$OUT/prewarm.json" 2> "$OUT/prewarm.err"
PW_RC=$?
echo "prewarm rc=$PW_RC elapsed=$(( $(date +%s) - T_PW0 ))s token_unset_warnings=$(grep -c 'CACHE_WARM_TOKEN unset' "$OUT/prewarm.err")"
jq -c '{requests_total, failed_count, cache_hits, deadline_skipped, elapsed_seconds, tile_failed: (.failed_count - ([.per_source[].png_failed] | add // 0)), png_out_of_contract: ([.per_source[].png_out_of_contract] | add // 0), per_source: (.per_source | map_values({cycle, discharge_requests, png_ok, png_failed, error}))}' "$OUT/prewarm.json"
jq -r '.failures[] | "  failure: status=\(.status) error=\(.error // "-") code=\(.error_code // "-") reason=\(.error_reason // "-") kind=\(if (.url|test("/precip/")) then "png" else "tile" end)"' "$OUT/prewarm.json" | sort | uniq -c | sort -rn | head -10
echo "tile failures by class (from truncated failures[], exact only when failed_count<=20): $(jq -r '[.failures[] | select(.url|test("/precip/")|not) | (if .status == 0 then "exception:" + (.error // "?") else "http:" + (.status|tostring) end)] | group_by(.) | map("\(.[0])=\(length)") | join(" ")' "$OUT/prewarm.json")"
L2=$(locks); P2=$(pbfs); echo "after prewarm: locks=$L2 pbf=$P2 (delta locks=$((L2-L1)) pbf=$((P2-P1)))"
[ "$L2" -le "$L1" ] && echo "lock gate: after <= before" || say "WARNING lock gate: after > before"
r=$(probe "$L/api/v1/tiles/river-network-national/5/25/12.pbf" "$OUT/rn-post2.h"); echo "river-network-national/5/25/12 (2nd): $r cache=$(hdr x-tile-cache "$OUT/rn-post2.h")"

# --------------------------------------------------------- 1.11 plan-only run
say "== 1.11 plan-only first run"
NODE27_MVT_CACHE_RETENTION_PLAN_ONLY=true bash "$REPO/scripts/node27_mvt_cache_retention_once.sh"; PO_RC=$?
PO_JSON=$(ls -t "$RET_LOGS"/mvt-cache-retention-*.json 2>/dev/null | head -1)
echo "plan-only rc=$PO_RC summary=$PO_JSON"
FINAL_RC=0
if [ "$PO_RC" != 0 ] || [ -z "$PO_JSON" ]; then say "RECORD: plan-only run rc=$PO_RC summary=${PO_JSON:-none} (past point of no return; not rolling back)"; FINAL_RC=6; fi
jq -c '{status, execution_mode, counts, precip_root_untouched, precip_paths: ([.planned[], .skipped[], .failed[] | (if type == "string" then . else (.path // "") end) | select(test("(^|/)precip/"))] | length), retention_days, cutoff}' "$PO_JSON"
[ -n "$PO_JSON" ] && { [ "$(jq -r .execution_mode "$PO_JSON")" = plan_only ] || { say "RECORD: plan-only execution_mode != plan_only"; FINAL_RC=6; }; cp "$PO_JSON" "$OUT/plan-only.json"; }

# --------------------------------------------------- 1.12 enable timer + 1.13
say "== 1.12 enable timer"
systemctl --user enable --now nhms-node27-mvt-cache-retention.timer
systemctl --user list-timers --all --no-pager | grep -E 'NEXT|mvt-cache-retention'
say "== 1.13 operator-started production tick"
systemctl --user start nhms-node27-mvt-cache-retention.service; PT_RC=$?
PT_JSON=$(ls -t "$RET_LOGS"/mvt-cache-retention-*.json | head -1)
echo "production tick systemctl rc=$PT_RC unit=$(systemctl --user is-active nhms-node27-mvt-cache-retention.service) result=$(systemctl --user show nhms-node27-mvt-cache-retention.service -p Result --value) summary=$PT_JSON"
[ -n "$PT_JSON" ] && [ "$PT_JSON" != "$PO_JSON" ] || { say "RECORD: production tick wrote no new summary"; FINAL_RC=6; }
echo "retention timer LastTriggerUSec=$(systemctl --user show nhms-node27-mvt-cache-retention.timer -p LastTriggerUSec --value) (empty => the summary above came from the operator start, not the timer)"
[ -n "$PT_JSON" ] && jq -e '(.execution_mode == "production_execute") and ((.finished_at | fromdateiso8601) > (now - 26*3600)) and (.failed | length == 0)' "$PT_JSON" > /dev/null; HC=$?
echo "health criterion rc=$HC"
[ -n "$PT_JSON" ] && jq -c '{status, execution_mode, counts, skipped_by_reason: ([.skipped[] | (if type == "string" then "?" else (.reason // "?") end)] | group_by(.) | map({(.[0]): length}) | add // {})}' "$PT_JSON"
[ -n "$PT_JSON" ] && cp "$PT_JSON" "$OUT/production-tick.json"
tail -3 "$RET_LOGS/mvt-cache-retention.log"

# ---------------------------------------------- 1.14 writers back + first tick
say "== 1.14 start writer timers"
T_START=$(ts); restart_timers
echo "window: stop=$T_STOP restart_display=$T_RESTART start=$T_START"
say "waiting for first natural autopipe tick from $REPO (<= 30 min)"
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
echo "first tick ExecStart: $(systemctl --user show nhms-node27-autopipe.service -p ExecStart --value | sed -E 's/ ; ignore_errors=.*$//')"
tail -c +$((AUTOPIPE_LOG_OFF + 1)) "$AUTOPIPE_LOG" > "$OUT/autopipe-tick.log"
echo "tick log lines=$(wc -l < "$OUT/autopipe-tick.log") seed_failed_import=$(grep -c 'seed_failed stage=import' "$OUT/autopipe-tick.log") token_unset=$(grep -c 'CACHE_WARM_TOKEN unset' "$OUT/autopipe-tick.log") prewarm_rc_lines=$(grep -c 'MVT prewarm rc=' "$OUT/autopipe-tick.log")"
grep -E 'autopipe: (start|done|phase=|MVT prewarm rc=|national MVT prewarm)' "$OUT/autopipe-tick.log" | tail -12
grep -E '^\{"base_url"' "$OUT/autopipe-tick.log" | tail -1 > "$OUT/tick-prewarm.json"
[ -s "$OUT/tick-prewarm.json" ] && jq -c '{requests_total, failed_count, cache_hits, tile_failed: (.failed_count - ([.per_source[].png_failed] | add // 0)), png_failed: ([.per_source[].png_failed] | add // 0)}' "$OUT/tick-prewarm.json"
L3=$(locks); P3=$(pbfs); echo "after first tick: locks=$L3 pbf=$P3"
T_START_EPOCH=$(date -u -d "$T_START" +%s)
for u in nhms-node27-download nhms-node27-frontier-alert; do
  st=$(systemctl --user show "$u.service" -p ExecMainStartTimestamp --value); se=$(date -d "$st" +%s 2>/dev/null || echo 0)
  if [ "$se" -gt "$T_START_EPOCH" ]; then echo "$u first run after unpin: OBSERVED start=$st $(systemctl --user show "$u.service" -p Result -p ExecStart --value | sed -E 's/ ; ignore_errors=.*$//' | tr '\n' ' ')"; else echo "$u first run after unpin: NOT OBSERVED in window (last start=$st <= T_START=$T_START) -> 1.17"; fi
done

# ------------------------------------ 1.14b resource-governance (read-only audit)
say "== 1.14b operator-started resource-governance audit (read-only lane)"
systemctl --user start nhms-node27-resource-governance.service; RG_RC=$?
echo "resource-governance start rc=$RG_RC $(systemctl --user show nhms-node27-resource-governance.service -p ExecMainStartTimestamp -p Result -p ExecStart --value | sed -E 's/ ; ignore_errors=.*$//' | tr '\n' ' ')"
journalctl --user -u nhms-node27-resource-governance.service --no-pager -n 6 | grep -v '^--' | tail -6

# ------------------------------------------------------- 1.15 public re-tests
say "== 1.15 public re-tests"
RUN1=$(curl -s --max-time 20 "$L/api/v1/runs?limit=1" | jq -r '.data.items[0].run_id // empty')
for inst in '9999-12-31T23:59:59-08:00' '0001-01-01T00:00:00+08:00' 'not-an-instant'; do
  r=$(curl -s --max-time 30 -o "$OUT/pub-hn.body" -w '%{http_code}' "$PUB/api/v1/tiles/hydro-national/q_down/$inst/4/12/6.pbf")
  echo "public hydro-national $inst: $r $(jq -r '.error.code // empty' "$OUT/pub-hn.body" 2>/dev/null)"
  if [ -n "$RUN1" ]; then r=$(curl -s --max-time 30 -o "$OUT/pub-h.body" -w '%{http_code}' "$PUB/api/v1/tiles/hydro/$RUN1/q_down/$inst/7/100/50.pbf"); echo "public hydro/{run} $inst: $r $(jq -r '.error.code // empty' "$OUT/pub-h.body" 2>/dev/null)"; fi
done
r=$(curl -s --max-time 30 -o "$OUT/pub-off.body" -w '%{http_code}' "$PUB/api/v1/layers?offset=999999999"); echo "public offset=999999999: $r data_len=$(jq -r '.data | length' "$OUT/pub-off.body" 2>/dev/null)"
for _ in 1 2 3; do probe "$L/api/v1/runs" >/dev/null; done
echo "#2078 warm /api/v1/runs ttfb x3: $(for _ in 1 2 3; do probe "$L/api/v1/runs" | cut -d' ' -f3; done | tr '\n' ' ')"
for i in $(seq 1 255); do curl -s -o /dev/null --max-time 10 "$L/api/v1/runs?basin_id=junk-$i&limit=1"; done
echo "#2078 after 255 junk /api/v1/runs ttfb x3: $(for _ in 1 2 3; do probe "$L/api/v1/runs" | cut -d' ' -f3; done | tr '\n' ' ')"
echo "#2079 no header ttfb x3: $(for _ in 1 2 3; do probe "$L/api/v1/runs" | cut -d' ' -f3; done | tr '\n' ' ')"
echo "#2079 external refresh header ttfb x3: $(for _ in 1 2 3; do probe "$L/api/v1/runs" "" -H 'x-nhms-cache-warm: refresh' | cut -d' ' -f3; done | tr '\n' ' ')"
echo "#2079 correct token ttfb x3: $(for _ in 1 2 3; do probe "$L/api/v1/runs" "" -H "x-nhms-cache-warm: $(grep '^NHMS_DISPLAY_CACHE_WARM_TOKEN=' "$REPO/infra/env/display.env" | cut -d= -f2-)" | cut -d' ' -f3; done | tr '\n' ' ')"

# ------------------------------------------------------------- 1.16 wrap-up
say "== 1.16 final state"
df -h / /home /data/GHDC | tee "$OUT/df-after.txt"
capture final
ls -la "$HOLD" > "$OUT/hold-after.txt"; diff "$OUT/hold-before.txt" "$OUT/hold-after.txt" > /dev/null && echo "capacity-hold dir: unchanged" || echo "capacity-hold dir: CHANGED (see hold-*.txt)"
echo "drop-ins remaining: $(find "$USERD" -path '*.d/*.conf' | wc -l); frozen copy HEAD=$(git -C "$FROZEN" rev-parse --short HEAD) porcelain=$(git -C "$FROZEN" status --porcelain | wc -l)"
systemctl --user list-timers --all --no-pager | grep -E 'NEXT|nhms-node27-(autopipe|download|mvt-cache-retention)'
systemctl --user --failed --no-pager | grep -E 'nhms-(display|node27)' || echo "no failed nhms-display/node27 units beyond pre-existing"
say "DONE rc=${FINAL_RC:-0}"
exit "${FINAL_RC:-0}"
