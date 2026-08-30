Fixture level: expanded
Repair intensity: high
Project profile: NHMS
Issue: #1914
Upstream suggested level: absent

## 1. Contract and implementation

- [x] 1.1 Change `SQL Migration Dry Run` to select `integration and not timescaledb_210`, preserving its service, DSN, job gate, root checkout, fail-closed policy, and all ordinary integration rows.
- [x] 1.2 Extend the structured workflow contract tests so bare integration, missing integration, missing exclusion, and over-broad exclusion mutations go red while the exact live workflow passes.
- [x] 1.3 Keep `timescaledb_210` registered but out of conftest auto-skip and `GATING_MARKER_NAMES`; prove owning targeted suites still expose their non-gated assertions.
- [x] 1.4 Make the #1892 isolated probe integration test assert status/original error before successful cleanup proof, without changing `cleanup_owned`, `_all_passed`, `parse_probe_report`, or unowned-container behavior.
- [x] 1.5 Update the CI routing runbook with the generic SQL lane versus explicit node-27 `timescaledb_210` command and the rule that Docker presence alone is not authorization.

## 2. Required evidence

- [x] 2.1 Batched red proof against pre-change source demonstrates the current workflow-contract gap and misleading pre-create failure assertion; post-change focused tests pass.
- [x] 2.2 `uv run pytest -q tests/test_integration_gate.py tests/test_select_ci_tests.py tests/test_probe_compressed_chunk_cold_tablespace.py tests/test_probe_compressed_chunk_cold_tablespace_cleanup.py -m 'not timescaledb_210'` passes without Docker mutation.
- [x] 2.3 A CI-equivalent collection/execution proof shows ordinary `integration` items remain selected and all `integration and timescaledb_210` items are deselected; PR #1907's sibling marker shape is covered by capability rather than filename.
- [x] 2.4 `uv run ruff check` for touched Python, `openspec validate node27-isolated-probe-opt-in --strict --no-interactive`, `git diff --check`, and the repository default backend test row pass.
- [x] 2.5 Node-27 frozen-head receipt explicitly runs `-m timescaledb_210`, proves disposable probe PASS and cleanup identity, leaves active `nhms-db` and checkout unchanged, and uses no production business DB mutation.

## 3. Risk and invariant fixture

Selected risk packs: Public API / CLI / script entry; Config / project setup; File IO / path safety / overwrite; Schema / columns / units / field names; Concurrency / shared state / ordering; Legacy compatibility / examples; Error handling / rollback / partial outputs; Documentation / migration notes; PostGIS / TimescaleDB domain behavior.
Not selected: Auth / permissions / secrets (no credential boundary); Resource limits / discovery (no bound change); Release / packaging (no dependency); geospatial, forcing, SHUD, Slurm, provider, manifest/QC, published-artifact packs (no such behavior).

Seams under test: structured `real-db-integration` workflow parser; pytest marker truth table; targeted selector AST anchor; isolated probe public test/report parser; cleanup owner unit tests; node-27 explicit marker lane.
Must preserve: ordinary integration CI, targeted selection, `timescaledb_210` non-auto-skip status, live-identity refusal, cleanup ownership/PASS proof, PR #1907 sibling compatibility.
Non-goals: probe engine changes, cleanup implementation changes, live DB/container mutation, Slurm changes, global `timescaledb_210` auto-skip.

Invariant Matrix:
- Governing invariant: generic CI never executes node-27-only disposable-cluster tests, while explicit node-27 execution and truthful ownership-bound cleanup evidence remain intact.
- Source of truth: pytest markers, workflow marker expression, `OwnedResources.created_*`, status/error and cleanup PASS fields.
- Producers: CI workflow, explicit node-27 command, probe report.
- Validators/preflight: workflow parser tests, pytest marker selection, selector AST guard, live identity refusal.
- Storage/query: disposable container/work root only; no business DB.
- Public/downstream: required SQL check, targeted unit lane, node-27 receipt, blocked PRs #1901/#1907.
- Failure/stale/side effects: absent `nhms-db`, inspect failure, name conflict, pre-create failure, marker drift, created-resource cleanup.
- Regression rows: generic ordinary integration executes; generic node-27 marker deselects; targeted owner assertions remain; node-27 explicit marker executes; setup error precedes cleanup assertion; successful PASS still requires complete cleanup proof.

Boundary checklist: workflow command/step metadata; marker registration and selector anchors; probe status/error and cleanup consumers; owned resource mutation; docs/receipt; open sibling PR #1907; unchanged integration/database lanes.
