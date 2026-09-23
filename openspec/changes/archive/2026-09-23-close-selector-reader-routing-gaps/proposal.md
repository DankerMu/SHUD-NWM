## Why

Five same-family `scripts/select_ci_tests.py` routing gaps, batched into one PR by explicit user instruction (batch J1). Each one lets a PR that changes a guarded input go green in the targeted `Unit Tests` lane while the guarding suite only runs in the post-merge master `Unit Tests (full)`:

- **#2316**: a diff to `services/orchestrator/scheduler_runtime.py` does not select `tests/test_retention_extra_roots.py`. That suite is the oracle for the `runs_only_roots` extra-root wiring of the retention deleter. `FILE_JOURNAL_READ_STATE_PATH_PATTERNS[11]` is a `stop_on_match` rule, and it shadows the `services/orchestrator/**` list that already carries the suite. This was the recorded #2260 sibling known limit.
- **#2317**: `tests/fixtures/basins_registry_partition_oracle.json` (#1913) and `tests/fixtures/qhh_bootstrap_partition_oracle.json` (#1948) select zero tests. They are frozen oracles read only by the meta-suite. Their self-digest guards run only when `tests/test_select_ci_tests.py` runs. The same family also has `tests/fixtures/station_series_baseline_heihe_ifs_2026060100.json`, which selects zero tests and whose non-gated reader is `tests/test_object_store_forcing.py`.
- **#2323**: the entropy production-topology hard gate reads every scannable text file under seven roots plus four root-level files. No selector rule routes those inputs to the hard-gate test, so an `openspec/changes/**/evidence/*.json` edit reddened master only after merge (PR #2321 → master run 34787384045).
- **#2390**: a diff to `services/orchestrator/file_orchestration_migration.py` selects none of the five `tests/test_gateway_reconcile_writer_*.py` rollback-lane suites. Two things combine here. The `stop_on_match` rule on `FILE_JOURNAL_READ_STATE_PATH_PATTERNS[3]` shadows the broad rules. And the suites' imports sit inside function bodies, so the importer-gap audit derives zero gap and cannot see the missing route.
- **#2498**: a **new** file under the forcing discovery roots that constructs a `ForcingTemplatePair` escapes every forcing shape oracle. The per-path riders and the register-derived wiring meta-test only cover paths that are already registered.

## What Changes

- Selector at-site extensions, with the shared `FILE_JOURNAL_READ_STATE_TESTS` constant left untouched:
  - pattern[11] (`scheduler_runtime.py`) gains `tests/test_retention_extra_roots.py`;
  - pattern[3] (`file_orchestration_migration.py`) gains the five gateway-reconcile writer suites (ruling A of #2390).
- Selector path-exact rules for the two frozen partition oracles, targeting `tests/test_select_ci_tests.py`, and for the station-series baseline fixture, targeting `tests/test_object_store_forcing.py`.
- Two new supplemental, additive selector loops. Neither sets `matched`, neither takes part in stop rules, and neither affects the unknown-backend fallback:
  - a production-topology reader route. It mirrors the scanner's scan roots, direct files, skip predicate and text-name predicate, and adds only the hard-gate node id;
  - a forcing-template import sniff. For a changed `.py` under the forcing discovery roots whose module-level imports include `packages.common.forcing_ts_render`, it adds `FORCING_SQL_SHAPE_ORACLE_TESTS`.
- Meta-suite (`tests/test_select_ci_tests.py`) additions:
  - exact-set and membership pins for every new edge;
  - `STOP_RULE_AT_SITE_EXTENSIONS` synced;
  - drift-red behavioural oracles that compare each mirror against its authority (the scanner's own `_production_topology_scan_files` over a synthetic tree, and the registry's `DISCOVERY_ROOTS`);
  - negative controls.
- Close the #2316 known-limit entries in the two archived changes that recorded it.

## Impact

- Affected specs: `ci-contract-baseline`. It gets five ADDED routing requirements. Two requirements are MODIFIED because their exact-set scenarios move: the `infra/env/*.example` owner-suite selections, and the scheduler-runtime mutex selection, which is already stale on master since the #2259 split.
- Affected code: `scripts/select_ci_tests.py`, `tests/test_select_ci_tests.py`. No production runtime code, no scanner or registry edit, no CI workflow change.
- Downstream: the K batch (#2460 #2490 #2527 #2532) touches the same two files and is expected to rebase on this change.
