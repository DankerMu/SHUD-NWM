#!/bin/sh
# Stage A (2026-09-18) stop-gap: #2285 unit drift install + compression bound 4->2.
set -eu
R=/home/nwm/tmp/svc-restore/stage-a
U=$HOME/.config/systemd/user
ENV=/home/nwm/.local/state/issue1895-maintenance-retirement-95481481/config/node27-timeseries-compression.env
mkdir -p "$R/before" "$R/after"
date -u +%FT%TZ > "$R/started_at"
for f in nhms-node27-raw-retention.service nhms-node27-timeseries-retention.timer nhms-node27-timeseries-compression.service; do cp -p "$U/$f" "$R/before/$f"; done
cp -p ~/.local/share/systemd/timers/stamp-nhms-node27-timeseries-retention.timer "$R/before/" 
systemctl --user show nhms-node27-raw-retention.service nhms-node27-timeseries-compression.service nhms-node27-timeseries-retention.service -p Id,ActiveState,Result,ExecMainStatus > "$R/before/state.txt"
# env: backup in the same 0700 dir, change only the bound line, never print values
cp -p "$ENV" "$ENV.bak-bound4-20260918"
sed -i 's/^NODE27_TIMESERIES_COMPRESSION_PER_TICK_BOUND=4$/NODE27_TIMESERIES_COMPRESSION_PER_TICK_BOUND=2/' "$ENV"
grep -c '^NODE27_TIMESERIES_COMPRESSION_PER_TICK_BOUND=2$' "$ENV" > "$R/after/bound2-count"
diff "$ENV.bak-bound4-20260918" "$ENV" | grep -c '^[<>]' > "$R/after/env-changed-lines" || true
stat -c '%a %U' "$ENV" > "$R/after/env-mode"
# budget chain check with the new bound (same command as the unit's ExecStartPre)
/home/nwm/NWM-maintenance-reviewed-95481481/.venv/bin/python -E /home/nwm/NWM-maintenance-reviewed-95481481/scripts/node27_timeseries_budget_preflight.py --compression-env "$ENV" --check > "$R/after/preflight.out" 2>&1; echo $? > "$R/after/preflight.rc"
# #2285: install repo raw-retention.service + retention timer
install -m 0644 /home/nwm/NWM/infra/systemd/nhms-node27-raw-retention.service "$U/"
install -m 0644 /home/nwm/NWM/infra/systemd/nhms-node27-timeseries-retention.timer "$U/"
systemctl --user daemon-reload
for f in nhms-node27-raw-retention.service nhms-node27-timeseries-retention.timer; do diff "$U/$f" /home/nwm/NWM/infra/systemd/$f > "$R/after/diff-$f.txt"; echo $? > "$R/after/diff-$f.rc"; done
systemctl --user reset-failed nhms-node27-timeseries-compression.service nhms-node27-timeseries-retention.service
# restart the retention timer: Persistent catch-up of today's refused tick
systemctl --user restart nhms-node27-timeseries-retention.timer
date -u +%FT%TZ > "$R/finished_at"
echo STAGE_A_OK
