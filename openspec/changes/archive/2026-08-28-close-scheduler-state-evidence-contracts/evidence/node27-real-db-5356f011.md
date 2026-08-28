# Node-27 real-DB candidate-state receipt — PR #1880

- Node: `node-27`
- Date: 2026-08-28
- Branch: `feat/issue-1579-scheduler-evidence-contracts`
- Tested detached HEAD: `5356f011f3b5869455bc4090c2c999353d043ae6`
- Expected pushed HEAD: `5356f011f3b5869455bc4090c2c999353d043ae6`
- Main checkout left untouched: yes (`fix/hhe-rivseg-mapping` carried unrelated untracked evidence)
- Test worktree: `/home/nwm/NWM-pr1880`, created detached from the exact pushed SHA
- Result: PASS (exit code 0)

## Observed/asserted values (test assertions on the passing live run)

The following are the exact values the executed test asserted on the live run
(reverse geometry first, then friendly geometry; both against the real
`PsycopgOrchestratorRepository` and the real
`FileOrchestrationJournalRepository`):

Reverse geometry (old `*_forecast_retry_87` row at the OLD end of the window):

```text
returned pipeline_jobs rows : 5     (== job_limit; hard returned-row bound)
pipeline_jobs_total         : 7     (1 retry + 6 publish rows; true admitted count)
state_truncated             : true  (7 > 5)
forecast stage attempt      : 87    (from the retry-suffixed row outside the window)
```

Friendly geometry (retry row is the NEWEST row, in-window, still > job_limit rows):

```text
returned pipeline_jobs rows : 5     (== job_limit)
pipeline_jobs_total         : 7     (6 publish + 1 retry)
state_truncated             : true  (both paths truncate)
forecast stage attempt      : 87
```

Canonical downstream-stage attempt parity — every stage, DB and file-journal
paths both, on BOTH geometries:

```text
convert 0, forcing 0, forecast 87, parse 0, state_save_qc 0, publish 0, copyback 0
```

The stage attempts above are the deterministic projection values the test
asserted per stage via `_state_retry_attempt(..., stage=...)` for every
`DOWNSTREAM_RESTART_STAGES` member; they are not separately printed log values.
Returned-row identity (pure-freshness top-`job_limit`, no row rescue) and
DB-vs-file-journal equality of totals/truncation/IDs are asserted by the same
pass.

## Oracle identity

The selected test was:

```text
tests/test_orchestration_chain.py::test_integration_candidate_state_reverse_truncation_matches_file_journal
```

It is marked `integration` and ran with:

- node-27 PostgreSQL/TimescaleDB/PostGIS on `127.0.0.1:55432`;
- a uniquely named, temporary login/CREATEDB role used only for this receipt;
- pytest's `integration_database_url` fixture creating and dropping a unique `nhms_it_<uuid>` database;
- real `PsycopgOrchestratorRepository` reads and real `ops.pipeline_job` inserts;
- real `FileOrchestrationJournalRepository.append_historical_pipeline_job` writes and `candidate_state` reads;
- reverse and friendly `job_limit` geometries, exact total/truncation, hard returned-row bound, pure-freshness row identity, and all canonical downstream-stage attempt parity.

No `CapturingRepository`, SQL-result stub, production database test writes, node-22 database, or mock repository served as the oracle.

## Command and result

The secret-bearing temporary DSN is intentionally omitted. The executed test command was:

```text
NHMS_RUN_INTEGRATION=1 \
NHMS_INTEGRATION_DATABASE_URL=<temporary-node27-role-and-throwaway-db-base> \
UV_PROJECT_ENVIRONMENT=/home/nwm/NWM/.venv \
uv run --no-sync pytest -q -m integration \
  tests/test_orchestration_chain.py::test_integration_candidate_state_reverse_truncation_matches_file_journal

.                                                                        [100%]
1 passed in 6.28s
```

Raw local-to-node receipt (gitignored):
`/home/nwm/NWM/.nhms-issue1572-live/pr1880-5356f011-node27-real-db.log`

## Setup diagnostics and cleanup

Two pre-test setup attempts failed before creating a throwaway database or running the test: first, the container initialization password no longer matched the persisted role password; second, a host `openssl` binary had incompatible shared-library versions. Neither attempt exercised application code. The final run used a kernel UUID for the one-use role password and did not modify production credentials or host configuration.

Post-run checks:

```text
temporary pr1880 integration roles: 0
remaining nhms_it_* databases: 0
test worktree status: clean
```

Node-22 was not used because this change does not modify sbatch, Slurm gateway/resource scheduling, or SHUD runtime behavior.

`NODE27_REAL_DB_RESULT=PASS`
