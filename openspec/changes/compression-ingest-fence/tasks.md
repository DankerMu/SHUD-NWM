# Tasks — compression-ingest-fence (#2713)

Fixture level: expanded. Risk packs: db-concurrency, production-ops. design.md is authoritative.

- [ ] 1 D0 facts: confirm targets and writers, whether store.py runs on node-27, and the caller-managed path. The orchestrator measures D0.4 (FK lock snapshot) and D0.5 (longest parser transaction) on node-27. Record all of them in the PR.
- [x] 2 Add the fence module, using the two-int4 keyspace, with unit tests.
- [x] 3 Runner:
  - the fence-wait knob, validated fail-closed and bound keyword-only;
  - the fence wait charged against the compress timeout;
  - `FenceContended` caught before the generic except;
  - receipt schema 2.2 (`deferred_contended`, `deferred_contended_count`, `fence_wait_elapsed_ms`, outcome `deferred` exiting 0);
  - the env example, the schema example, the pins;
  - a MODIFIED delta for the hypertable-compression budget-chain requirement.
- [x] 3b Retention: exclusive fence before `drop_chunks`; contention maps to the existing `lock-contention(55P03)`; unit and real-DB tests (design D2b).
- [x] 4 Writers: the try-fence is the first statement of each write transaction. `IngestFenceBusy` carries its own reason code, the parser does not call `mark_run_failed`, and autopipe classifies it as skipped. Test the status transitions.
- [x] 5 Structural wire-site invariant for fenced writers.
- [x] 6 Real-DB tests, marked integration and timescaledb_210: river red (or documented attempts), river green, forcing-shape regression.
- [x] 7 Runbook section, lifecycle-lock docstring, selector `PATH_TEST_RULES`.
- [x] 8 `uv run ruff check .`, the touched unit suites, `tests/test_select_ci_tests.py`, the hard-gate CLI exits 0, and `openspec validate --strict`.
- [ ] 9 Orchestrator, on node-27: real-DB pytest (`-m "integration and timescaledb_210"` for the new tests, plus the touched DB suites) in an isolated worktree.
- [ ] 10 Orchestrator, after merge: node-27 `git pull --ff-only`. Run a manual compression tick while ingest is live, re-compress `_hyper_9_213_chunk` and `_hyper_9_206_chunk`, confirm there is no `deadlock detected` in the PG log, and record the receipt.

## Evidence Floor
- Real-DB red/green on node-27, or documented red attempts.
- Unit and structural suites green; ruff clean; hard gate rc 0.
- The post-merge node-27 receipt: both chunks compressed, no 40P01, and ingest runs deferred, not failed, during the window.
