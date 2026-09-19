#!/bin/bash
# Pre-merge live plan-only check of NODE27_RAW_RETENTION_LANES from the scratch worktree.
set -u
W=/home/nwm/tmp/wt-2425
O=/home/nwm/tmp/svc-restore/planonly
mkdir -p "$O"
git -C "$W" rev-parse HEAD > "$O/head"
for lanes in "raw,precip-cache" "canonical" "raw,canon"; do
  tag=$(echo "$lanes" | tr ',' '_')
  env NODE27_RAW_RETENTION_REPO="$W" NODE27_RAW_RETENTION_ENV_FILE=/home/nwm/NWM/infra/env/node27-raw-retention.env NODE27_RAW_RETENTION_PLAN_ONLY=true NODE27_RAW_RETENTION_LANES="$lanes" \
      NODE27_RAW_RETENTION_SUMMARY_PATH="$O/summary-$tag.json" NODE27_RAW_RETENTION_LOG_FILE="$O/run-$tag.log" \
      NODE27_RAW_RETENTION_BOOTSTRAP_LOG="$O/bootstrap-$tag.log" \
      "$W/scripts/node27_raw_retention_once.sh"
  echo "$lanes rc=$?" >> "$O/rc.txt"
done
