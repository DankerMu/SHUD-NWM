Fixture level: expanded
Repair intensity: high
Project profile: NHMS
Change surface: gateway-reconcile test modules, shared test helpers, targeted-test selector, selector governance tests, compatibility inventories, failed-basin runbook
Must preserve: 538 unique collected case suffixes; every test body/decorator/assertion; Slurm-gateway selector ownership; orchestrator runtime-budget exclusions; downstream helper imports
Seams under test: pytest collection/full-suite boundary; `select_tests`; support-module routing; entropy/inventory checks
Minimal mergeable slice: one atomic behavior-preserving split with selector and reference updates; a partial split leaves either the guard or CI selection broken

## 1. Freeze the Oracle

- [x] 1.1 Capture master collection (238 top-level tests / 538 unique `::test_name[param-id]` suffixes), full-suite PASS, and selector outputs for Slurm gateway, real backend, reconcile, and persistence paths.
- [x] 1.2 Preserve a deterministic old-suffix-to-new-full-node-ID mapping artifact and prove no duplicate or missing suffix.

## 2. Partition the Suite

- [x] 2.1 Replace `tests/test_gateway_reconcile.py` with flat responsibility-focused `tests/test_gateway_reconcile_*.py` modules, each at or below 1,000 lines, with no collectible shim.
- [x] 2.2 Extract shared store/cohort/identity and writer/barrier fixtures into non-collectible support modules at or below 1,000 lines; update all checked-in consumers.
- [x] 2.3 Preserve patched symbol ownership, each `Path(__file__)` source-inspection target, function-local chain import order, test decorators/parameter IDs, and all assertions byte-for-byte except necessary import/path moves.
- [x] 2.4 Correct only the stale explanatory comment so the accepted decision set is `{absence_retry_permitted, operator_verified_absence}`.

## 3. Preserve Selector and Reference Contracts

- [x] 3.1 Replace the monolith target in the existing `services/slurm_gateway/**` rule with every split suite; do not add the suites to `services/orchestrator/**` or the narrow real-backend extension.
- [x] 3.2 Add exact support-module routing/anchors and per-partition runtime-budget dispositions; prove helper-only changes run collectible consumers and do not select `tests/test_production_scheduler.py`.
- [x] 3.3 Update demotion/scheduler test imports, active OpenSpec commands, canonical owner references, compatibility-inventory commands, and failed-basin node IDs. Keep the frozen entropy-test literals as explicitly non-executable provenance until #1823 removes that separate large-file deadlock.

## 4. Risk Packs

- [x] 4.1 Public API / CLI / script entry: selected - preserve `select_tests` outputs and targeted-CI ownership with exact selector probes.
- [x] 4.2 Config / project setup: selected - satisfy the unchanged 1,000-line guard without exemption and keep pytest discovery configuration unchanged.
- [x] 4.3 File IO / path safety / overwrite: selected - delete only the monolith, add bounded modules, and reject stale/missing selector or documentation paths; runtime path-security behavior is a non-goal.
- [x] 4.4 Schema / columns / units / field names: not selected - no production schema, columns, units, payload fields, or serialized format changes.
- [x] 4.5 Auth / permissions / secrets: not selected - no authorization boundary or secret handling changes.
- [x] 4.6 Concurrency / shared state / ordering: selected - preserve idempotency/barrier test bindings, monkeypatch ownership, and chain import order.
- [x] 4.7 Resource limits / large input / discovery: selected - enforce every module at or below 1,000 lines and preserve the recorded runtime-budget selector boundary.
- [x] 4.8 Legacy compatibility / examples: selected - preserve test suffix identities, checked-in imports, inventories, and runbook node IDs.
- [x] 4.9 Error handling / rollback / partial outputs: selected - collection loss, duplicate collection, stale selector targets, or broken helper imports must fail evidence; no partial split is mergeable.
- [x] 4.10 Release / packaging / dependency compatibility: selected - support modules remain importable but non-collectible under existing pytest/package rules; no dependency changes.
- [x] 4.11 Documentation / migration notes: selected - update tracked executable commands/node IDs; record full-path migration via the mapping artifact.
- [x] 4.12 Geospatial / CRS / basin geometry: not selected - no geospatial behavior changes.
- [x] 4.13 Hydro-met time series / forcing windows: not selected - no forcing/time-window behavior changes.
- [x] 4.14 SHUD numerical runtime / conservation / NaN: not selected - no model runtime or numerical changes.
- [x] 4.15 PostGIS / TimescaleDB domain behavior: not selected - no database schema/query/runtime behavior changes.
- [x] 4.16 Slurm production lifecycle / mock-vs-real parity: selected - preserve the existing gateway-reconcile corpus and Slurm-gateway selector ownership; no node-22 runtime change is introduced.
- [x] 4.17 External hydro-met providers / snapshot reproducibility: not selected - no provider behavior changes.
- [x] 4.18 Run manifest / QC provenance: not selected - no manifest/QC producer or evidence format changes.
- [x] 4.19 Published NHMS artifacts / display identity: not selected - no publish/display path changes.

## 5. Required Evidence

- [x] 5.1 `uv run pytest --collect-only -q tests/test_gateway_reconcile_*.py`: 538 cases; sorted suffix diff against the frozen baseline empty; duplicate suffix report empty; mapping TSV complete.
- [x] 5.2 `uv run pytest -q tests/test_gateway_reconcile_*.py`: all 538 cases pass.
- [x] 5.3 `uv run pytest -q tests/test_select_ci_tests.py`: all selector governance tests pass; representative selector outputs preserve the frozen boundary.
- [x] 5.4 Run focused compatibility tests for demotion, entropy/inventory references, and moved runbook node IDs; expected result is no import/path/oracle failure.
- [x] 5.5 `uv run ruff check .` and `openspec validate split-gateway-reconcile-tests --strict --no-interactive`: zero findings / strict-valid.
- [x] 5.6 Confirm no changed/new gateway-reconcile Python module exceeds 1,000 lines, `.large-file-guard.json` is unchanged, production source is unchanged, and no `[DEBUG-` marker or `red-proof` stash remains.
Phase 8 external merge gate (not a change task): required GitHub CI must pass on the frozen final head and is recorded in PR evidence.

## 6. Non-Goals and Review Focus

- [x] 6.1 Non-goals confirmed: no production source, schema, dependency, pytest discovery, assertion/oracle, guard exemption, unrelated oversized-suite, node-27 deployment, or node-22 scheduling change.
- [x] 6.2 Review focus: one-to-one collection mapping; no oracle weakening; helper/module binding correctness; exact selector ownership/no broadening; executable tracked references.
