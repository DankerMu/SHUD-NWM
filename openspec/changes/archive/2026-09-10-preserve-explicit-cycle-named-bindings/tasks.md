## 1. Tests-first binding contract

- [x] 1.1 Add focused red tests proving the shipping named mapping is preserved through recorder output and `execute_explain`, with arbitrary insertion order, exact unique referenced-key equality, predicate-name-to-canonical-value checks, and repeated `issue_time` references sharing one key.
- [x] 1.2 Directly test one pure captured-query validator with synthetic selected-cycles, mixed/malformed styles, missing/extra named keys, wrong predicate key/value, positional count mismatch, wrong equality ordinal, wrong container type, and missing/wrong run/model/timeseries-segment bindings; every case must fail before live SQL and no test modifies the named-only shipping owner.
- [x] 1.3 Add synthetic positional compatibility and digest tests proving positional count/ordinal semantics, mapping order is insignificant, sequence order is significant, mutation aliases are absent, and semantic changes reject or change the digest.

## 2. Runtime repair

- [x] 2.1 Preserve a defensive named mapping or positional tuple in `_RecordingCursor`; add one pure validator that resolves named required predicates to mapping keys/values, treats repeated pyformat names as one key, and applies count/ordinal checks only to positional sequences; call it from `record_explicit_cycle_curve`.
- [x] 2.2 Canonicalize both supported containers deterministically for `query_digest` without replacing the runtime container.
- [x] 2.3 Update `execute_explain`/`make_sql_probe` to pass mappings as mappings and sequences as tuples while preserving current typed failures, readonly setup, and timeout bounds.

## 3. Downstream and selector closure

- [x] 3.1 Keep lane freeze, performance/publication receipt shape, CLI full-probes wiring, and forecast-store SQL unchanged; update only stale tests that asserted positional syntax rather than explicit-cycle semantics.
- [x] 3.2 Ensure every changed production owner directly selects assertion-bearing #2227 tests, with exact-membership and target-removal selector mutants; no collect-only or coincidental union is accepted.

## 4. Evidence floor

- [x] 4.1 `uv run --no-sync pytest -q tests/test_issue2227_explicit_cycle_named_binding.py` passes with the new behavior and negative matrix.
- [x] 4.2 `uv run --no-sync pytest -q tests/test_issue1895_readiness_performance.py tests/test_issue1895_readiness_performance_live.py tests/test_issue1895_readiness_performance_live_cli.py tests/test_issue1895_readiness_performance_publication.py` changes from the exact clean-master baseline `23 failed, 31 passed` to all passing.
- [x] 4.3 The shipping selector for the final diff, `tests/test_select_ci_tests.py`, `tests/test_issue1895_runbook_contract.py`, affected #1895 readiness tests, default unit suite, and collection all pass; collection alone is not assertion evidence.
- [x] 4.4 `uv run ruff check .`, Python compilation for changed files, and `openspec validate preserve-explicit-cycle-named-bindings --strict --no-interactive` pass.
- [x] 4.5 Review confirms no forecast-store SQL, receipt schema, timeout, readonly, C1-C4, node-27 rollout, or protected unrelated OpenSpec change was weakened. This PR produces no node-27 live PASS; G7 live evidence remains pending #1895.

## Risk pack mapping

- Public API/CLI, schema/field names, resource bounds, compatibility, error/partial-output, hydro-met window, Timescale behavior, and published-display identity are selected and covered by 1.1-4.5.
- Config, file-path IO, auth changes, concurrency, packaging, migration docs, geospatial, SHUD, Slurm, external-provider, and run-manifest packs are not selected because their implementation surfaces remain unchanged.
