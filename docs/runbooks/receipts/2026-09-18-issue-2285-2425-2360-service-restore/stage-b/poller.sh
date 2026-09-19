#!/bin/bash
B=/home/nwm/tmp/svc-restore/stage-b/after/compression-attempt2
while [ "$(systemctl --user show nhms-node27-timeseries-compression.service -p ActiveState --value)" = activating ]; do
  docker exec -i nhms-db psql -U nhms -d nhms -X -q -At -F'|' -P pager=off < /home/nwm/tmp/svc-restore/poll.sql >> "$B/poll.txt" 2>&1
  sleep 60
done
systemctl --user show nhms-node27-timeseries-compression.service -p Result,ExecMainStatus,ExecMainStartTimestamp,ExecMainExitTimestamp > "$B/final.txt"
