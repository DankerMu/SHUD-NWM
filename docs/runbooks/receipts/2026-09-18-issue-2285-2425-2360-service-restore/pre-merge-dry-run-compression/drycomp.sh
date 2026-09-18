#!/bin/bash
# Pre-merge dry-run of the PR compression selection from the scratch worktree.
set -u
W=/home/nwm/tmp/wt-2425
O=/home/nwm/tmp/svc-restore/drycomp
FENCE_ENV=/home/nwm/.local/state/issue1895-maintenance-retirement-95481481/config/node27-timeseries-compression.env
mkdir -p "$O"; chmod 700 "$O"
T="$O/env"
( umask 077; sed "s#^NODE27_TIMESERIES_COMPRESSION_REPO_ROOT=.*#NODE27_TIMESERIES_COMPRESSION_REPO_ROOT=$W#" "$FENCE_ENV" > "$T" )
grep -c "^NODE27_TIMESERIES_COMPRESSION_REPO_ROOT=$W\$" "$T" > "$O/repo_root_count"
NODE27_TIMESERIES_COMPRESSION_ENV_FILE="$T" NODE27_TIMESERIES_COMPRESSION_REPO_ROOT="$W" \
  "$W/scripts/node27_timeseries_compression_once.sh" --receipt-path "$O/dry-receipt.json" --lock-path "$O/dry.lock" > "$O/stdout" 2> "$O/stderr"
echo $? > "$O/rc"
rm -f "$T"
git -C "$W" rev-parse HEAD > "$O/head"
