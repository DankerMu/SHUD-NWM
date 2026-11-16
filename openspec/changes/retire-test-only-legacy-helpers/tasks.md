# Tasks: retire-test-only-legacy-helpers (#3148)

## Risk packs

- [x] Legacy compatibility — selected: repository and store methods sit on governed compatibility lists
  (`chain_compat_static.py`, the facade inventories). → tasks 1.2, 2.3.
- [x] Public API / CLI / script entry — selected: `packages/common` store/protocol methods; the operator script
  `scripts/node22_manual_retry_failed_runs.py`; the API route `apps/api/routes/pipeline.py`. → tasks 1.1, 2.4, 2.5.
- [x] Error handling — selected: store failure-path helpers (`reclaim_reservation`, `expire_stale_retries`). Any
  failure path that is still live must stay covered. → tasks 1.3, 2.2.
- [x] Documentation — selected: inventories, living docs and `openspec/specs/**` that name a retired symbol. → tasks
  1.2, 1.4, 2.7.
- [x] Concurrency — selected: `reserve_job` and `reclaim_reservation` back the shared `_StoreRepo` fake. That fake
  drives the live reserve/bind/reclaim protocol and the Barrier idempotency tests, so concurrency coverage must
  survive. → tasks 1.3, 2.2.
- [ ] Not selected — File IO, Schema, Auth, Resource limits, Release, Config. Deleting helpers changes none of
  them.

## 1. Implementation

- [x] 1.1 Give every row of #3148 a verdict, after a fresh reference sweep that applies the #3147 dynamic-reach checks.
  The PR records the table from design.md (symbol | production refs | test refs | protocol/compat listing | verdict |
  tests removed or narrowed).
- [x] 1.2 Keep any symbol that a Protocol, an ABC, a governed compatibility list or a Requirement in
  `openspec/specs/**` requires, and record why. If a removal condition is met and you choose to retire the symbol, update the list or inventory, or add a
  MODIFIED or REMOVED delta for that capability, in the same change.
- [x] 1.3 Retire the remaining rows. Delete a test only when every assertion in it targets a retired symbol.
  Otherwise narrow it, and list it as a deviation. A symbol that a shared test helper or fake uses to drive live
  production code is kept, or its implementation is moved verbatim into the test helper and recorded as a deviation.
  Example: `tests/gateway_reconcile_helpers.py` `_StoreRepo` uses `reserve_job` and `reclaim_reservation`.
- [x] 1.4 Update the inventories and living docs that name a retired symbol. Historical records stay unchanged.
- [x] 1.5 Update the selector tables (`scripts/select_ci_tests*.py`) if a deleted test file is named there.

## 2. Tests / verification (each expected to pass)

- [x] 2.1 `uv run ruff check .` is clean.
- [ ] 2.2 Affected suites. The file set is the union of:
  - `rg -l '\b<sym>\b' tests` for every symbol touched;
  - every importer of `tests/gateway_reconcile_helpers.py`;
  - `tests/test_gateway_reconcile_idempotency_barrier.py` and the reservation lifecycle tests.

  Run the set locally and in a node-27 temp worktree against the real DB, and list the files in the PR.
- [x] 2.3 The compatibility and facade guards pass:
  - `uv run pytest -q "tests/test_entropy_audit_report_contract.py::test_compatibility_facade_guard_current_repo_passes_with_inventories"`;
  - every test that reads `chain_compat_static` (`rg -l chain_compat_static tests`).
- [ ] 2.4 The selector-chosen set for the diff, plus `tests/test_select_ci_tests.py`, passes locally and in a node-27
  temp worktree (backend oracle; real DB).
- [x] 2.5 An import smoke of every touched module, with `services.orchestrator.scheduler` imported first, then
  `apps.api.main` and `scripts.node22_manual_retry_failed_runs`.
- [x] 2.6 `uv run python scripts/governance/audit_repo_entropy.py --mode hard-gate --format json` exits 0, with
  `hard_gate_status == "pass"` and `failing_count == 0`.
- [x] 2.7 A grep for every retired symbol across living docs, `openspec/specs/`, the selector tables, `infra/`, `.github/` and
  `pyproject.toml` returns zero hits. A symbol retired through a MODIFIED or REMOVED delta in this change is exempt
  for its `openspec/specs/` hits, which change at archive time.

## Deviations

- Owner decision (2026-11-16): two **retire** verdicts are descoped to #3155. Their files exceed the 1000-line
  large-file-guard, which blocks even deletion-only commits, and the owner chose not to widen the guard's exclude list.
  - `_parse_comment_sacct_rows` and the cascaded `_parse_comment_sacct_matches` are in
    `services/orchestrator/reconcile.py`.
  - `prove_named_entry_absent_durable` is in `packages/common/safe_fs.py`.

  Both symbols and their tests stay unchanged in this change.
