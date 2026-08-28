# Node-27 real-DB candidate-state receipt — PR #1880

- Node: `node-27`
- Date: 2026-08-28
- Branch: `feat/issue-1579-scheduler-evidence-contracts`
- Tested detached HEAD: `5356f011f3b5869455bc4090c2c999353d043ae6`
- Expected pushed HEAD: `5356f011f3b5869455bc4090c2c999353d043ae6`
- Main checkout left untouched: yes (`fix/hhe-rivseg-mapping` carried unrelated untracked evidence)
- Test worktree: `/home/nwm/NWM-pr1880`, created detached from the exact pushed SHA
- Result: PASS (exit code 0)

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
