## 1. Selector routes (`scripts/select_ci_tests.py`)

- [x] 1.1 (#2316, D1) Append `tests/test_retention_extra_roots.py` to the `FILE_JOURNAL_READ_STATE_PATH_PATTERNS[11]` rule target tuple. Replace the "sibling ... gap stays open" sentence of that rule's comment with the #2316 closure note and the [9]/[10] trade-off. Leave `FILE_JOURNAL_READ_STATE_TESTS` and the rules of patterns [0]–[2] and [4]–[10] byte-identical (pattern [3] is D2).
- [x] 1.2 (#2390, D2, ruling A) Append the five `tests/test_gateway_reconcile_writer_{prepare,launch,rollforward,receipts,quiescence}.py` suites to the `FILE_JOURNAL_READ_STATE_PATH_PATTERNS[3]` rule target tuple. The rule comment must record:
  - why the edit is at the rule site;
  - why the importer-gap audit derives no gap (function-body imports);
  - the measured test count and wall clock.
- [x] 1.3 (#2317, D3) Add path-exact rules next to the #2183 additions-ledger rule, none with `stop_on_match` or `only_when_any_changed`:
  - `tests/fixtures/basins_registry_partition_oracle.json` → `("tests/test_select_ci_tests.py",)`;
  - `tests/fixtures/qhh_bootstrap_partition_oracle.json` → `("tests/test_select_ci_tests.py",)`;
  - `tests/fixtures/station_series_baseline_heihe_ifs_2026060100.json` → `("tests/test_object_store_forcing.py",)`.
- [x] 1.4 (#2323, D4) Add the production-topology mirror constants and a supplemental additive loop placed after the #1627 loop.
  - Target: the hard-gate node id.
  - Do not add the node when `tests/test_entropy_audit_report_contract.py` is already selected.
  - The loop does not set `matched`, takes no part in stop rules, and does not touch the unknown-backend fallback.
  - No stat and no existence check.
- [x] 1.5 (#2498, D5) Add the forcing discovery-root and pruned-directory mirror constants and an import-sniff supplemental additive loop that reuses `_top_level_imported_module_names`.
  - Fall through silently on a missing or non-regular file, `OSError`, `UnicodeDecodeError`, `SyntaxError` or `ValueError`.
  - Record the function-body / nested-import blind spot at the constant.

## 2. Meta-suite (`tests/test_select_ci_tests.py`)

- [x] 2.1 (#2316) Pin that `services/orchestrator/scheduler_runtime.py` selects `tests/test_retention_extra_roots.py` and both `RETENTION_COPYBACK_MUTEX_TESTS`. Pin that the selections for patterns [0], [8], [9] and [10] are unchanged; use exact sets where a pin already exists. Update the `STOP_RULE_AT_SITE_EXTENSIONS` row for `scheduler_runtime.py`.
- [x] 2.2 (#2390) Update the `STOP_RULE_AT_SITE_EXTENSIONS` row for `file_orchestration_migration.py` to list all six at-site targets. Pin membership of the five suites, and pin that `tests/test_state_clone.py` stays absent (the stop rule still stops).
- [x] 2.3 (#2317) Exact-set pins: each partition oracle selects exactly `["tests/test_select_ci_tests.py"]`; the station-series baseline selects exactly `["tests/test_object_store_forcing.py"]`. The selections of `tests/fixtures/basins_registry_partition_additions.json` and `tests/fixtures/river_ts_templates_51f9d273.json` must stay unchanged, with an exact-set pin. Deleting any new rule must red a pin.
- [x] 2.4 (#2323) Add the D4 drift oracles:
  - (a) mirror constants equal to the scanner constants;
  - (b) synthetic-tree equality between `_production_topology_scan_files` and the selector mirror, with the listed positives and negatives;
  - (c) real-tree containment of scanner output in the mirror;
  - (d) root-drop monkeypatch observed through `select_tests`.

  Also add routing pins:
  - `openspec/changes/compressed-chunk-cold-tablespace-tiering/evidence/retirement-c4-verification.json` selects the node, and keeps every target it had before;
  - one representative path per root and each direct file select the node;
  - `openapi/nhms.v1.yaml`, `docs/runbooks/x.log` and `tests/x.md` do not;
  - `scripts/governance/entropy_audit/check_topology.py` selects the whole partition and not the duplicate node.
- [x] 2.5 (#2498) Add a mirror pin equal to the registry's `DISCOVERY_ROOTS` and `_PRUNED_DIRECTORIES`.
  - Positive: under a `tmp_path` repo root with the forcing oracle targets stubbed, one new synthetic file per discovery root that imports `packages.common.forcing_ts_render` at module level selects every `FORCING_SQL_SHAPE_ORACLE_TESTS` member. This includes the issue's three paths, and covers all four import forms.
  - Negative: the same roots without that import select none of them.
  - Deleted-path case: a nonexistent path, an unparsable file and a non-UTF-8 file raise nothing.
  - Blind spot: a function-body-only import does not select, pinned as the documented limit.
  - The existing 16 census ∪ register paths keep their forcing routing.
- [x] 2.6 Keep every pre-existing meta-suite pin green. Every exact-set pin that legitimately gains a target from D1–D5 is updated, and each update names its reason. No pin may lose a target. Known movers, from the fixture review's static list (the full node-27 run is the complete check):
  - `test_generated_roots_and_unrelated_docs_stay_selector_empty` (~:3667) and `test_select_tests_ignores_docs_only_changes` (~:6605): retarget and rename to "selects exactly the hard-gate node", and rewrite the premise comments;
  - production-ops runbook pins (~:17923), infra/env sibling pins (~:3397), `openspec/changes/direct-grid-forcing/**` bounded pins (~:825, :834);
  - `scripts/node27_autopipeline.py` (~:1823), `scripts/node27_autopipe_cron.sh` (~:1937), `scripts/validate_readonly_db_boundary.py` (~:2747);
  - the scheduler_runtime literal pin (~:11246).
  - Keep authority imports (`tests.forcing_ts_template_registry`, entropy `constants` / `check_topology`) function-local, following the `:1795` precedent.

- [x] 2.7 (Review round 1, P1) Compute `meta_guard_only` over the final selection with the supplemental hard-gate node disregarded. Pins:
  - a deleted test file, an unrouted support module, or a D3 oracle, each plus `tasks.md`, gives `meta_guard_only=true` and `collection_smoke_required=true`;
  - `schemas/foo.json` plus `tasks.md` gives `[node]`, with both flags false (accepted trade);
  - an ordinary selection plus `tasks.md` stays non-collapsed.

## 3. Evidence Floor

- [x] 3.1 Selector outputs before and after, for:
  - every issue Verification command;
  - the 26-path BEFORE snapshot at `3b32f9ed0` (reproduced in the PR body);
  - each new path.

  The diff must show only the D1–D5 gains.
- [x] 3.2 Tracked-tree gain-only sweep, one `select_tests([p])` per tracked non-test path, before and after. Every difference must be a gain. The gained targets must be exactly:
  - the hard-gate node on D4-accepted paths;
  - the forcing oracles on D5 importers;
  - `tests/test_retention_extra_roots.py` on `scheduler_runtime.py`;
  - the five writer suites on `file_orchestration_migration.py`;
  - the three fixture routes.

  D5's gain on the tracked tree is expected to be empty, because the existing importers are already routed. D5's proof is the synthetic tree in 2.5.
- [x] 3.3 node-27 oracle at the final head (isolated worktree, disposable scratch PG, `NHMS_RUN_INTEGRATION=1`). Run:
  - `tests/test_select_ci_tests.py`;
  - `tests/test_retention_extra_roots.py`;
  - the five writer suites (count and wall clock recorded);
  - the hard-gate node;
  - `FORCING_SQL_SHAPE_ORACLE_TESTS`;
  - `tests/test_object_store_forcing.py`.
- [x] 3.4 `uv run ruff check .` and `uv run ruff format --check` on the touched files. `openspec validate close-selector-reader-routing-gaps --strict --no-interactive`.
- [x] 3.5 Close the #2316 known-limit entries in the archived changes:
  - `openspec/changes/archive/2026-09-13-close-selector-gate-fixture-gaps/tasks.md` (:42, :93);
  - `openspec/changes/archive/2026-09-11-route-retention-deletes-through-copyback-mutex/tasks.md` (:360-367).

  Append a "closed by #2316" note; do not rewrite history.
- [x] 3.6 Spec delta MODIFIED blocks for three requirements. The first is "Empty targeted-test selection MUST be loudly self-identifying": its `meta_guard_only` definition disregards the node, its empty class gains the topology exclusion, and two new scenarios are added (review round 1). The other two are "the scheduler refresh env template MUST select its content-asserting owner suite" and "the retention copyback mutex load-bearing modules MUST select the mutex suite". Restate **every** scenario of each with measured selections, including the `copyback_guard.py` and provider-refresh scenarios, and name the pre-existing staleness.
- [ ] 3.7 CI green on the final push.

## Deviations (recorded)

- #2323 AC names `tests/test_entropy_audit_script.py`, which is gone since the #1823 split. The hard gate is `tests/test_entropy_audit_report_contract.py::test_entropy_audit_current_repo_hard_gate_has_zero_production_topology_findings`, and the route targets that node id rather than the ~97s file.
- #2323 AC5 is met at the selector level only. A docs- or openspec-only PR starts neither `unit-test-targeted` nor, on master, `unit-test` (ci.yml `backend` filter, D6). A trigger change is out of scope and is filed as #2602.
- The issues say "local-only verification"; the user's batch mandate routes all verification through the node-27 oracle, so 3.3 runs there.

## Known limits

- The D5 import sniff misses function-body, nested (`if` / `try`) and attribute-through-parent-package imports of `forcing_ts_render`. Those cases red on master.
- The D4 mirror omits the scanner's size and symlink checks and the topology checker's archive/receipt/`scripts/governance` exclusions, so it can over-select only.
- The hard-gate node covers 13 check families; only the production-topology inputs are routed.
