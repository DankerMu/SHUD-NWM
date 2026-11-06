#!/bin/bash
# Quiet-window re-measure for #2486: wait for the autopipe backlog to drain, then
# re-run the archived api0918.py unchanged. READ ONLY; DSN via env only.
set -uo pipefail
cd /home/nwm/tmp/1987
for i in $(seq 1 90); do
  s=$(systemctl --user is-active nhms-node27-autopipe.service)
  l=$(awk '{print $1}' /proc/loadavg)
  echo "$(date +%H:%M:%S) autopipe=$s load1=$l"
  if [ "$s" != "activating" ] && [ "$s" != "active" ]; then
    # hold until the 1-min load also settles, so we measure a genuinely quiet box
    if awk "BEGIN{exit !($l < 2.0)}"; then echo "QUIET"; break; fi
  fi
  sleep 20
done
cp -n api-0918.json api-0918-contended.json
set -a; . /home/nwm/NWM/infra/env/display.env; set +a
export PATH=$HOME/.local/bin:$PATH
echo "=== re-measure start $(date +%H:%M:%S) load=$(awk '{print $1}' /proc/loadavg) ==="
/home/nwm/NWM/.venv/bin/python api0918.py
echo "=== re-measure end $(date +%H:%M:%S) load=$(awk '{print $1}' /proc/loadavg) ==="
mv api-0918.json api-0918-quiet.json
mv api-0918-contended.json api-0918.json
echo DONE
