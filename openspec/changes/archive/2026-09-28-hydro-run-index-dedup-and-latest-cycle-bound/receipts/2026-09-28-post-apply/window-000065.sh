#!/bin/bash
# Batch DB production window: apply 000065 on node-27 nhms (user-approved 2026-09-28).
set -uo pipefail
export TMPDIR=/home/nwm/tmp; mkdir -p $TMPDIR
LOG=/home/nwm/tmp/batch-db/window-000065.log
exec > >(tee -a "$LOG") 2>&1
ts() { date -u +%Y-%m-%dT%H:%M:%SZ; }
restart_timers() { systemctl --user start nhms-node27-autopipe.timer nhms-node27-download.timer; echo "$(ts) timers: $(systemctl --user is-active nhms-node27-autopipe.timer nhms-node27-download.timer | tr '\n' ' ')"; }
echo "$(ts) step1 stop timers"
systemctl --user stop nhms-node27-autopipe.timer nhms-node27-download.timer
for i in $(seq 1 120); do
  a=$(systemctl --user is-active nhms-node27-autopipe.service); d=$(systemctl --user is-active nhms-node27-download.service)
  [ "$a" != active ] && [ "$a" != activating ] && [ "$d" != active ] && [ "$d" != activating ] && break
  sleep 10
done
echo "$(ts) services: autopipe=$(systemctl --user is-active nhms-node27-autopipe.service) download=$(systemctl --user is-active nhms-node27-download.service)"
cd /home/nwm/NWM || { restart_timers; exit 2; }
echo "$(ts) step2 git"
if [ -n "$(git status --porcelain)" ]; then echo "DIRTY checkout, abort"; git status --porcelain; restart_timers; exit 3; fi
git log --oneline -1
git pull --ff-only || { echo "ff pull failed"; restart_timers; exit 4; }
git log --oneline -1
test -f db/migrations/000065_hydro_run_candidate_index_dedup.sql || { echo "000065 missing"; restart_timers; exit 5; }
echo "$(ts) step3 migrate"
DSN=$(grep '^DATABASE_URL=' infra/env/node27-timeseries-compression-replay.env | cut -d= -f2-)
case "$DSN" in postgresql://nhms:***@127.0.0.1:55432/nhms) : ;; *) echo "DSN unexpected"; restart_timers; exit 6;; esac
for attempt in 1 2 3; do
  echo "$(ts) migrate attempt $attempt"
  DATABASE_URL="$DSN" .venv/bin/python -m packages.common.migrate; rc=$?
  echo "$(ts) migrate rc=$rc"
  [ $rc -eq 0 ] && break
  sleep 5
done
echo "$(ts) step4 restart timers"
restart_timers
exit ${rc:-1}
