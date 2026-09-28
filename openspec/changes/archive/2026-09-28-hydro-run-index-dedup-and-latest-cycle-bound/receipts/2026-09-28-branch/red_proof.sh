#!/bin/bash
# Red-before proof: run the branch's new/changed tests against origin/master SOURCE, in the
# branch worktree, by temporarily swapping in master's forecast_store.py (run A) and removing
# 000065 (run B). Restores both and checks the tree equals the branch patch afterwards.
set -uo pipefail
WT=/home/nwm/tmp/batch-db/wt
SAVE=/home/nwm/tmp/batch-db/red-save; mkdir -p $SAVE
cd $WT || exit 2
before=$(git diff | sha256sum)
cp packages/common/forecast_store.py $SAVE/forecast_store.py
cp db/migrations/000065_hydro_run_candidate_index_dedup.sql $SAVE/
echo "=== RUN A: packages/common/forecast_store.py = origin/master"
git show origin/master:packages/common/forecast_store.py > packages/common/forecast_store.py
git diff --stat origin/master -- packages/common/forecast_store.py | tail -1
/home/nwm/tmp/batch-db/it_branch.sh tests/test_latest_cycle_discovery_shape.py tests/test_river_ts_stats_harness_offline.py \
  tests/test_river_ts_text_identity_cleanup.py tests/test_latest_cycle_discovery_integration.py -p no:randomly 2>&1 \
  > /home/nwm/tmp/batch-db/red-proof-A.full 2>&1; grep -E "^(FAILED|ERROR) |passed|failed|pytest_rc" /home/nwm/tmp/batch-db/red-proof-A.full
cp $SAVE/forecast_store.py packages/common/forecast_store.py
echo "=== RUN B: db/migrations/000065 absent (origin/master ledger)"
rm db/migrations/000065_hydro_run_candidate_index_dedup.sql
/home/nwm/tmp/batch-db/it_branch.sh tests/test_migrations.py tests/test_schema_ledger_convergence_integration.py 2>&1 \
  > /home/nwm/tmp/batch-db/red-proof-B.full 2>&1; grep -E "^(FAILED|ERROR) |passed|failed|pytest_rc" /home/nwm/tmp/batch-db/red-proof-B.full
cp $SAVE/000065_hydro_run_candidate_index_dedup.sql db/migrations/
after=$(git diff | sha256sum)
[ "$before" = "$after" ] && echo "RESTORED: tree equals the branch patch" || echo "RESTORE_MISMATCH"
git status --short
