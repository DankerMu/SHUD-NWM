#!/bin/bash
# Disposable-DB pytest for the batch-DB branch (tasks 3.1/3.2). tests/conftest.py's
# throwaway_database_url creates and drops a uniquely named database per test on the
# node-27 cluster; production `nhms` is never connected to by the tests.
set -uo pipefail
export TMPDIR=/home/nwm/tmp; mkdir -p $TMPDIR
WT="${WT:-/home/nwm/tmp/batch-db/wt}"
cd "$WT" || exit 2
export PYTHONPATH="$WT"
set -a; . /home/nwm/NWM/infra/env/node27-timeseries-compression-replay.env; set +a
case "${DATABASE_URL:-}" in postgresql://nhms:*@*) : ;; *) echo "DSN_ROLE_UNEXPECTED"; exit 2 ;; esac
export NHMS_INTEGRATION_DATABASE_URL="$DATABASE_URL"; unset DATABASE_URL
export NHMS_RUN_INTEGRATION=1
echo "head=$(git -C "$WT" rev-parse HEAD) status:"; git -C "$WT" status --short
/home/nwm/NWM/.venv/bin/python -B -m pytest -q -p no:cacheprovider -rs "$@"
echo "pytest_rc=$?"
