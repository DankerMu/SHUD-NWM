# Node-27 real-DB candidate-state receipt — PR #1880 fixed head

- Node: `node-27`
- Date: 2026-08-28
- Branch: `feat/issue-1579-scheduler-evidence-contracts`
- Tested detached HEAD: `fdad81b69c6ca9610ce1b7f5f424b6f476f930da`
- Expected pushed HEAD: `fdad81b69c6ca9610ce1b7f5f424b6f476f930da`
- Result: PASS (exit code 0)
- Test: `tests/test_orchestration_chain.py::test_integration_candidate_state_reverse_truncation_matches_file_journal`

## Oracle and values

The marked integration test used node-27 PostgreSQL on `127.0.0.1:55432`, a unique temporary CREATEDB role, pytest's unique `nhms_it_<uuid>` throwaway database, real `PsycopgOrchestratorRepository`/`ops.pipeline_job` rows, and real `FileOrchestrationJournalRepository` write/read paths. No mock repository or SQL-result stub served as the oracle.

The passing test asserted on both DB and file-journal paths:

```text
reverse geometry:
  returned pipeline_jobs rows = 5
  pipeline_jobs_total         = 7
  state_truncated             = true
  forecast stage attempt      = 87

friendly geometry:
  returned pipeline_jobs rows = 5
  pipeline_jobs_total         = 7
  state_truncated             = true
  forecast stage attempt      = 87

canonical stage parity on both geometries:
  convert 0, forcing 0, forecast 87, parse 0,
  state_save_qc 0, publish 0, copyback 0
```

Pure-freshness top-`job_limit` job identities and DB/file totals/truncation are asserted equal. These are test assertion values on the passing live run; they are not separately printed result rows.

## Command and result

The secret-bearing one-use DSN is intentionally omitted:

```text
NHMS_RUN_INTEGRATION=1 \
NHMS_INTEGRATION_DATABASE_URL=<temporary-node27-role-and-throwaway-db-base> \
UV_PROJECT_ENVIRONMENT=/home/nwm/NWM/.venv \
uv run --no-sync pytest -q -m integration \
  tests/test_orchestration_chain.py::test_integration_candidate_state_reverse_truncation_matches_file_journal

.                                                                        [100%]
1 passed in 8.51s
```

Raw gitignored receipt on node-27:
`/home/nwm/NWM/.nhms-issue1572-live/pr1880-fdad81b6-node27-real-db.log`

## Cleanup

```text
temporary pr1880_r2 roles: 0
remaining nhms_it_* databases: 0
test worktree: removed
```

The node-27 main checkout and its unrelated untracked evidence were untouched. Node-22 was not used because no sbatch, Slurm gateway/resource scheduling, or SHUD runtime behavior changed.

`NODE27_REAL_DB_FIXED_HEAD_RESULT=PASS`
