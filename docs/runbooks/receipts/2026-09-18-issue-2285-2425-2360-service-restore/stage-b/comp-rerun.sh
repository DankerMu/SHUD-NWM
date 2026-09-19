#!/bin/bash
set -u
A=/home/nwm/tmp/svc-restore/stage-b/after/compression-attempt1; B=/home/nwm/tmp/svc-restore/stage-b/after/compression-attempt2
mkdir -p "$A" "$B"
F=/home/nwm/tmp/svc-restore/dblog-0055-0205.txt
journalctl --user -u nhms-node27-timeseries-compression.service --since "2026-09-19 08:57" --no-pager -o short-iso > "$A/journal.txt"
grep -E "\[(253564|253565|253566|253567|253568|254492)\]|application_name=i9-" "$F" | grep -vE "connection (received|authenticated)" | cut -c1-220 > "$A/timeline.txt"
grep -oE "application_name=[^ ]+" "$F" | sort | uniq -c | sort -rn > "$A/app-counts.txt"
grep -vE "connection (received|authenticated|authorized)|disconnection" "$F" | cut -c1-300 > "$A/db-nonroutine.txt"
psq() { docker exec -i nhms-db psql -U nhms -d nhms -X -q -P pager=off "$@"; }
echo "SELECT now(), chunk_name, range_end, is_compressed FROM timescaledb_information.chunks WHERE hypertable_name='river_timeseries' ORDER BY range_end DESC;" | psq > "$A/catalog-after.txt"
# preflight like the unit, then start
[ "$(git -C /home/nwm/NWM status --porcelain | wc -l)" = 0 ] || { echo DIRTY; exit 1; }
git -C /home/nwm/NWM rev-parse HEAD > "$B/head.txt"
systemctl --user start --no-block nhms-node27-timeseries-compression.service
date -u +%FT%TZ > "$B/started.txt"
# detached poller
{ setsid nohup bash -c '
B='"$B"'
sleep 5
while [ "$(systemctl --user show nhms-node27-timeseries-compression.service -p ActiveState --value)" = activating ]; do
  docker exec -i nhms-db psql -U nhms -d nhms -X -q -At -F"|" -P pager=off -c "SELECT now()::timestamp(0), a.pid, a.wait_event_type, a.wait_event, now()-a.query_start, pg_blocking_pids(a.pid), (SELECT string_agg(b.application_name||\":\"||coalesce((now()-b.xact_start)::text,\"-\"), \",\") FROM pg_stat_activity b WHERE b.pid = ANY(pg_blocking_pids(a.pid))), left(a.query,70) FROM pg_stat_activity a WHERE a.application_name=\$\$nhms-ts-compression\$\$ AND a.state<>\$\$idle\$\$" >> "$B/poll.txt" 2>&1
  sleep 60
done
systemctl --user show nhms-node27-timeseries-compression.service -p Result,ExecMainStatus,ExecMainStartTimestamp,ExecMainExitTimestamp > "$B/final.txt"
' > "$B/poller.out" 2>&1 & }
echo started $(cat "$B/started.txt") head $(cat "$B/head.txt")
