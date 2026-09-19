#!/bin/bash
set -u
R=/home/nwm/NWM; O=/home/nwm/tmp/svc-restore/stage-b/after; mkdir -p "$O"; cd "$O"
cp /home/nwm/tmp/svc-restore/stage-b/installer.out "$O/installer.out"
C=$(ls -t /var/log/nhms-node27-canonical-retention/raw-retention-*.json | head -1); cp "$C" "$O/"
N=$(ls -t /home/nwm/node27-raw-retention-logs/raw-retention-*.json | head -1); cp "$N" "$O/"
echo "canonical=$(basename $C) nwm=$(basename $N)"
for f in "$C" "$N"; do jq -c '{lanes,status,execution_mode,retention_days,sources,cutoff,counts,copyback_lock_failures,deleted_n:(.deleted|length),failed_n:(.failed|length),lane_skips:[.skipped[]|select(.reason=="lane_not_selected")]}' "$f"; done
echo "retention_days/sources equal: $(jq -s '(.[0].retention_days==.[1].retention_days) and (.[0].sources==.[1].sources)' "$C" "$N")"
echo "canonical deleted keys sample:"; jq -r '.deleted[]|(.key // .path // tostring)' "$C" | head -3
for u in nhms-node27-canonical-retention.service nhms-node27-canonical-retention.timer nhms-node27-system-unit-failure-alert@.service; do
  diff /etc/systemd/system/$u $R/infra/systemd/system/$u > "diff-sys-$u.txt"; echo "diff-sys $u rc=$?"; done
for u in nhms-node27-raw-retention.service nhms-node27-raw-retention.timer nhms-node27-timeseries-retention.service nhms-node27-timeseries-retention.timer nhms-node27-timeseries-compression.service nhms-node27-timeseries-compression.timer nhms-node27-unit-failure-alert@.service; do
  if [ -f ~/.config/systemd/user/$u ]; then diff ~/.config/systemd/user/$u $R/infra/systemd/$u > "diff-user-$u.txt"; echo "diff-user $u rc=$?"; else echo "diff-user $u not-installed"; fi; done
for u in nhms-node27-raw-retention.service nhms-node27-timeseries-retention.timer nhms-node27-timeseries-compression.service; do
  echo "$u $(systemctl --user show $u -p DropInPaths,WorkingDirectory --value | tr '\n' ' ')"; done
systemctl show nhms-node27-canonical-retention.service -p User,Result,ExecMainStatus,DropInPaths,OnFailure > canonical-show.txt; cat canonical-show.txt
systemctl is-active nhms-node27-canonical-retention.timer
systemctl list-timers --all --no-pager 'nhms-node27-*' > timers-system.txt; systemctl --user list-timers --all --no-pager 'nhms-node27-*' > timers-user.txt; cat timers-system.txt timers-user.txt
ls -la /home/ghdc/nwm/object-store/.nhms-copyback-batch.lock
echo "--user --failed:"; systemctl --user --failed --no-pager --no-legend
echo "system failed nhms:"; systemctl --failed --no-pager --no-legend | grep nhms || echo none
echo "alert env:"; systemctl show nhms-node27-system-unit-failure-alert@x.service -p User,SupplementaryGroups,EnvironmentFiles,Environment 2>&1 | sed -E 's/(PASS|TOKEN|SECRET)[^ ]*/\1=<redacted>/g'
id nwm
