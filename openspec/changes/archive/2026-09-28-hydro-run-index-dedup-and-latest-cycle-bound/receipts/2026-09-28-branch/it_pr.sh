#!/bin/bash
# PR-head disposable-DB pytest (fix pass 1, P2): a clean detached worktree of
# origin/feat/issue-2626-2634-2630-hydro-run-index-and-latest-cycle-bound at 03646206b.
# tests/conftest.py throwaway_database_url creates and drops a uniquely named database
# per test on the node-27 cluster; production `nhms` is never connected to by the tests.
set -uo pipefail
export TMPDIR=/home/nwm/tmp; mkdir -p "$TMPDIR"
WT=/home/nwm/tmp/batch-db/wt-pr
EXPECT=03646206bf2ddfa30ff74bb03943cdb052e991dc
FILES=(tests/test_schema_ledger_convergence_integration.py tests/test_migrations.py tests/test_latest_cycle_discovery_integration.py tests/test_latest_cycle_discovery_shape.py tests/test_real_database_integration.py tests/test_river_ts_read_path_surrogate_keys_integration.py tests/test_river_ts_text_identity_cleanup.py tests/test_forecast_series_run_identity_pushdown_integration.py tests/test_river_timeseries_stats_index_choice_integration.py tests/test_node27_write_roles.py tests/test_hydro_status_set_parity.py)
echo "# PR #2688 head rerun on node-27 disposable DBs, $(date -u +%FT%TZ)"
echo "# Fix pass 1 items 1-3 edit only comments/docs (design.md, receipts, the 000065 header,"
echo "# forecast_store.py comments); SQL statements and code are unchanged, so this PR-head run"
echo "# stays representative for the fix-pass tree."
cd "$WT" || { echo "NO_WORKTREE"; exit 2; }
head=$(git rev-parse HEAD); echo "head=$head"
[ "$head" = "$EXPECT" ] || { echo "HEAD_MISMATCH"; exit 2; }
st=$(git status --porcelain); echo "git status --porcelain: [${st}]"
[ -z "$st" ] || { echo "DIRTY_WORKTREE"; exit 2; }
export PYTHONPATH="$WT"
set -a; . /home/nwm/NWM/infra/env/node27-timeseries-compression-replay.env; set +a
case "${DATABASE_URL:-}" in postgresql://nhms:*@*) : ;; *) echo "DSN_ROLE_UNEXPECTED"; exit 2 ;; esac
export NHMS_INTEGRATION_DATABASE_URL="$DATABASE_URL"; unset DATABASE_URL
export NHMS_RUN_INTEGRATION=1
echo "env: NHMS_RUN_INTEGRATION=1 NHMS_INTEGRATION_DATABASE_URL=<replay env DSN, redacted> PYTHONPATH=$WT TMPDIR=$TMPDIR"
ARGV=(/home/nwm/NWM/.venv/bin/python -B -m pytest -q -p no:cacheprovider -rs "${FILES[@]}")
echo "argv: ${ARGV[*]}"
"${ARGV[@]}"
echo "pytest_rc=$?"
