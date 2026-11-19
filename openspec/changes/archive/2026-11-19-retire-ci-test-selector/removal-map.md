# Selector removal map for #3219 (read-only inventory, branch feat/issue-3218-process-rules, 2026-11-19)

Reading key: "L<n>" = line in the file named. REMOVE / MODIFY / KEEP as defined in the task.

## 0. Headline findings (read these first)

1. The selector is imported by exactly ONE test file: `tests/test_select_ci_tests.py` (25,260 lines, 690 top-level test functions). Every other `select_ci_tests` hit in `tests/`, `scripts/`, `db/`, `infra/` is a comment/docstring (section B). No other test imports `scripts.select_ci_tests*`, shells out to it, or runs `ci_selector_snapshot.sh`.
2. The 4 `tests/fixtures/*partition*` JSONs are read ONLY by `tests/test_select_ci_tests.py` (+ named in selector rule tables). After deletion they are orphans: delete them too.
3. `tests/test_select_ci_tests.py` ALSO carries the only pins on ci.yml workflow contracts that nothing else pins (about 60 test functions + helpers: concurrency, `backend`/`database`/`frontend` filter entries, real-db job contract incl. DSN/marker expression, workflow self-change). Deleting the file silently drops these (list in B.3). `tests/test_ci_shard_tests.py`, `tests/test_ci_workflow_locked_install.py`, `tests/test_full_regression_watch.py`, `tests/test_governance_workflow_hard_gate.py`, `tests/test_floating_resolve_report.py` pin other parts of ci.yml and WILL break/need edits when ci.yml changes (C.5).
4. The issue's verification command `git grep -n select_ci_tests -- ':!openspec/changes/archive'` currently returns 489 hits in 75 files. Hits that are in the issue's "frozen/history" class and are NOT code: `docs/review-loop-log.jsonl` (19 hits, frozen ledger guarded by `tests/test_loop_log_audit_attribution.py` REAL_LOG), `docs/runbooks/receipts/*` (5 files, 8 hits), `docs/adr/0003-review-lens-rotation-keep.md` (4), `docs/governance/entropy-audit-2026-10-16.md` (2) + `.snapshot.json` (1), `docs/runbooks/tier-node27-timeseries-storage.md` (2), `docs/runbooks/production-ops/stuck-detection.md` (1). The verification as written cannot pass without rewriting frozen ledgers/receipts/ADRs; the pathspec must be amended (e.g. exclude `docs/review-loop-log.jsonl`, `docs/runbooks/receipts`, `docs/adr`, `docs/governance/entropy-audit-*`) or those files edited. Decision needed from parent.
5. ci.yml `unit-test-targeted` runs `pytest -q <files>` with NO `-m` filter; `unit-test` runs `-m "not e2e and not grib and not integration"`. Targeted relies on conftest auto-skip (`tests/conftest.py` L96-121). PR will therefore stop running opt-in-skipped nodes the same way (they were skipped before, now deselected) - no behavioural loss, but the PR job count of "skipped" changes.
6. Hard-coded pins that break when ci.yml changes (C.5): `tests/test_ci_shard_tests.py` L288, L347-348, L358; `tests/test_floating_resolve_report.py` L497 (locked-install count 4 -> 3 once `unit-test-targeted` is deleted); `tests/test_ci_workflow_locked_install.py` L23 (`REQUIRED_PYTEST_JOBS` names `unit-test-targeted`); `tests/test_full_regression_watch.py` L165, L449, L469 (literal 4 shards).

## A. OpenSpec capability specs

Method: parsed every `openspec/specs/*/spec.md` by `### Requirement:` block; searched requirement text + scenarios for `select_ci_tests*`, `PathTestRule`, `PATH_TEST_RULES`, "selector", "targeted" CI/selection, `CORE_SMOKE_TESTS`, "meta-guard/meta-suite", `collect-only`, `full_test_durations`, `shard_tests`, `unit-test-targeted`, "Unit Tests", routing of suites. 295 specs scanned. The list given in the task was NOT complete. Specs with selector content: 15 (all 9 you named plus ci-merge-gates, guarded-display-module-partition, node27-test-oracle-lanes, node27-isolated-probe-routing, real-integration-test-matrix, scheduler-stall-alerting; re-checked after fixing a first-pass grep denylist that wrongly dropped lines containing "invariant"/"variant" - `variant-activation-cutover` and `direct-grid-variant-registration` have NO selector content). Table:

| spec | total reqs | REMOVE | MODIFY | KEEP | whole spec deletable? |
|---|---|---|---|---|---|
| ci-test-selector-routing | 6 | 6 | 0 | 0 | YES - delete the directory |
| ci-contract-baseline | 50 | 40 | 3 | 7 | no (7 KEEP + 3 MODIFY remain); Purpose bullets L4-10 MODIFY |
| ci-merge-gates (not in your list) | 9 | 1 | 2 | 6 | no |
| gateway-reconcile-test-partitioning | 4 | 1 | 1 | 2 | no |
| replay-lineage-test-partitioning | 1 | 0 | 1 | 0 | no (single requirement survives with 2 scenarios cut; if you choose REMOVE for it the spec has zero requirements -> delete) |
| guarded-display-module-partition (not in list) | 6 | 1 | 0 | 5 | no |
| orchestrator-structural-burndown | 34 | 0 | 8 (6 clear + L537, L609 flagged "enforcement orphaned") | 26 | no |
| node27-test-oracle-lanes (not in list) | 6 | 0 | 1 | 5 | no |
| node27-isolated-probe-routing (not in list) | 2 | 0 | 1 | 1 | no |
| real-integration-test-matrix (not in list) | 15 | 0 | 1 | 14 | no |
| scheduler-stall-alerting (not in list) | 12 | 0 | 1 | 11 | no |
| import-layering-guard | 4 | 0 | 1 | 3 | no |
| lint-ratchet | 2 | 0 | 1 | 1 | no |
| time-helper-consolidation | 3 | 0 | 1 | 2 | no |
| frontend-river-click-live-evidence | 5 | 0 | 1 | 4 | no |

Only `ci-test-selector-routing` ends with zero requirements. `ci-contract-baseline` keeps 10 requirements (7 KEEP + 3 MODIFY); `replay-lineage-test-partitioning` keeps its single requirement (MODIFY).

Spec files that matched my search but are KEEP (incidental): `api-contract-alignment` L50 "backend route tests"; `basins-asset-discovery` L20 "synthetic unit tests"; `frontend-*`, `map-*`, `mobile-*`, `mvt-tile-contract` L71, `station-mvt-tiles` L14, `hypertable-compression` L1064 "per-writer unit tests: the wire-site invariant", `runtime-evidence-and-operations` L186, `frontend-testing`: these say "unit tests"/"route tests" in the ordinary sense, nothing selector. `ci-merge-gates` L8-18 (locked install) is KEEP: still true; the guard test just loses `unit-test-targeted` from its list. Also KEEP: `legacy-dead-code-retirement` L164 ("selector carry" = a code branch carrying a retired route, unrelated) and `two-node-docker-runtime` L178/L205/L210 ("meta-guard" = its own TMPDIR guard).

### A.1 ci-test-selector-routing - DELETE WHOLE SPEC (6/6 REMOVE)
`openspec/specs/ci-test-selector-routing/spec.md` (98 lines). Requirements: L8 Owner diffs select their direct behavioural oracles; L28 Guarded owners fail closed on new importers; L35 Journal facade and sibling edits SHALL select the test files that patch facade globals; L46 Compatibility facade guard inputs SHALL select the facade-guard current-repo tests; L63 Canonical converter diffs SHALL select the converter and adapter suites; L84 Selector data tables and logic SHALL be separate modules with identical output. Note L46's body also mentions the selector (hits=2); all six are selector-only.

### A.2 ci-contract-baseline (`openspec/specs/ci-contract-baseline/spec.md`, 1655 lines, 50 requirements)
Purpose L3-10 MODIFY: bullet 1 (L5-6 "targeted PR selection reaches every suite ...") and bullet 2 (L7 "an empty or degraded selection identifies itself") must go; bullets 3-4 (entropy gate / OpenAPI green; "workflow concurrency, path filters and tooling environments never silently drop or misroute a run") stay.

KEEP (7): L13 Governance cleanup starts from a green contract baseline; L33 Python tooling commands use the repository-managed environment; L414 CI paths-filters MUST NOT carry dead file patterns (about the `changes` job filters, still true; its scenario enforcement lived in the selector suite - flag); L428 Entropy hard gate MUST be green on master without weakening the gate; L655 CI concurrency MUST preserve every non-PR workflow run (ci.yml L12-19 unchanged by the issue; its pin tests `test_ci_concurrency_*` live in the selector suite - flag); L693 OpenAPI error validation MUST be green without weakening rules (L695 "collect-only check" is a prohibition on weakening validation, unrelated to the selector); L1595 Frontend type check MUST gate the frontend build.

MODIFY (3):
- L707 `OpenAPI changes MUST execute drift and generated-type assertions`: cut sentence 1 (L709 "A pull request changing `openapi/**` MUST open the backend targeted-test gate and select ... a change to `apps/api/openapi_patching.py` MUST also select the drift and 3.1 invariant suites ..."), scenario "OpenAPI-only change reaches assertions" (L711-714) and "Patch-owner change reaches all consumers" (L716-719). Keep sentence 2 ("Generated frontend types MUST match the static schema ...") and scenario "Generated business types do not drift" (L721-724). Retitle (no longer "execute"/select).
- L902 `Integration-owned production sources MUST trigger real-database CI`: keep the `database` paths-filter registry contract and real-db job scenarios; cut scenario "Integration source registry and filter cannot drift silently" (L912-914, names `tests/test_select_ci_tests.py`), scenario "Workflow changes execute the trigger contract" (L927-929), and in "Existing CI lanes retain their contracts" (L947-949) drop `unit-test-targeted,` from the list. WARNING: the DSN / step-override / marker-expression / closed-suite-command scenarios (L916-945) are enforced by the `test_real_db_job_*` helpers inside the deleted suite (B.3); either drop them or port the helper first.
- L1464 `The cross-PR review-gate memory file SHALL select its structural guard`: cut every "targeted selection SHALL route it to the suite ..." clause (L1466-1473 text, two occurrences) and the scenario's "AND targeted selection" half (L1475-1480). Surviving idea: `.review-gate-issues.json` must still open the backend lane (ci.yml L87-93 backend filter entry). If the `backend` filter is dropped for PRs this requirement collapses -> REMOVE; decide with C.6.

REMOVE (40), header line numbers (all selector-only): generated below in A.2a.

### A.3 other specs

#### A.2a ci-contract-baseline REMOVE list (header line : title)

1. L49: Targeted CI selection MUST include a changed script's same-name test suite
2. L109: Targeted CI selection MUST include the container contract's dependent suites
3. L153: Empty targeted-test selection MUST be loudly self-identifying
4. L315: Guarded-module selector rules MUST cover their non-gated importer closure
5. L350: Changed-test PRs MUST run the selector meta-guards
6. L446: Selector path-rule duplicate patterns MUST be allowlisted decisions
7. L465: Selector gating-marker exclusions MUST anchor to the conftest auto-skip set
8. L482: Directory-rule importer gaps MUST be dispositioned as selections or reasoned exclusions
9. L506: Support-module changes MUST select their non-gated importer suites
10. L565: Meta-guard tree derivations MUST stay content-faithful under parse caching
11. L622: Shell wrapper changes MUST gate their guard suites
12. L674: CI workflow policy changes MUST execute their contract suite
13. L726: Shared-library targeted selection MUST retain its baseline
14. L745: Tree-scanning invariant suites MUST follow every scanned source root
15. L858: Selector-development changes MUST retain full-tree collection smoke
16. L951: Changed test suites MUST select their non-gated top-level importer suites
17. L987: Calibration declaration changes MUST execute their consumer contract suites
18. L1008: node-27 mvt-cache-retention unit changes MUST reach the sibling systemd.err lane set pin
19. L1025: every node-27 service unit change MUST reach the sibling systemd.err lane set pin
20. L1074: node-27 unit files with a content-asserting owner suite MUST select that suite
21. L1141: precip service tree changes MUST select the prewarm reader suite
22. L1155: display and scheduler unit files with a content-asserting owner suite MUST select that suite
23. L1174: the scheduler refresh env template MUST select its content-asserting owner suite
24. L1204: the precipitation application-composition owners MUST select the precipitation surface oracles
25. L1234: the precipitation composition edges MUST be backed by constructive mutation proofs
26. L1248: the river-segment routed root set MUST be derived from the scan's own source, not frozen a second time
27. L1339: the retention copyback mutex load-bearing modules MUST select the mutex suite
28. L1361: path routing rules MUST NOT carry an activation gate
29. L1370: the supplemental invariant-root meta-guard MUST observe routing, not only the constant
30. L1384: registry partition additions MUST be registered, never backfilled
31. L1408: A selector rule naming consumers it cannot derive SHALL carry an independent exact-set anchor
32. L1424: The OpenAPI patch module's rule SHALL reach every suite that can observe its output, and SHALL NOT carry suites that cannot
33. L1481: the scheduler runtime rule site MUST select the extra-roots wiring suite
34. L1495: the file-orchestration migration rule site MUST select the rollback-lane writer suites
35. L1504: frozen fixture data read by a non-gated suite MUST select that suite
36. L1524: production-topology scanner inputs MUST select the topology hard gate
37. L1557: new forcing-template modules MUST select the forcing shape oracles
38. L1576: review-governance tools and records SHALL select their suites
39. L1614: SHUD executable preflight changes MUST select its behaviour suites
40. L1632: Irregular source and package routes MUST select their owned state-clone and mapping-builder suites

(count=40; expected 40)

Judgement calls inside the ci-contract-baseline REMOVE list (say so in the spec delta):
- L1234 "the precipitation composition edges MUST be backed by constructive mutation proofs": the proofs are tests in `tests/test_precip_overlay.py` (docstring L1489-1491) and `tests/test_openapi_drift.py` (docstring L148-150) that STAY green and useful; the requirement is justified only by a selector edge ("A selector edge that pulls a suite into the lane is only justified if..."). Treated as REMOVE; keep the tests, reword the two docstrings.
- L1384 "registry partition additions MUST be registered, never backfilled" and L1504 "frozen fixture data ... MUST select that suite": guards + ledger (`basins_registry_partition_additions.json`) are read only by the deleted suite -> REMOVE with the fixtures.
- L1248 "river-segment routed root set MUST be derived from the scan's own source": selector-suite-only (`tests/test_river_segment_write_surface_scan.py` L66 is just a comment).

#### A.3a Other specs (exact lines; the MODIFY quotes must go, line numbers are spec-file lines)

- `ci-merge-gates` (9 req: 1 REMOVE, 2 MODIFY, 6 KEEP)
  - REMOVE L95 "The frontend CI filter entries SHALL be pinned by a meta-test" (L97 "a selector meta-test SHALL fail when any of them is removed"; enforcement = `test_ci_frontend_filter_block_lists_the_lane_switch_entry` / `..._pin_reds_when_the_entry_leaves_the_frontend_block`, selector suite L14022/L14034). If the pin is ported to another test, KEEP instead.
  - MODIFY L52 "The master full regression runs as complete, disjoint shards": title and L54 "The `Unit Tests (full)` job SHALL run ..." say "master"; after #3219 the same job also runs on PRs. Keep sharding/marker-expression/timeout sentences (L54). Scenario L56-57 "New test file" (default weight) stays.
  - MODIFY L104 "Workflow files SHALL be linted on pull requests that change them": last scenario "The filter entry is lost" (L113-116, "THEN a meta-test in the targeted backend suite fails") must go or be re-homed; enforcement was in the selector suite (`test_ci_gates_batch_*`, L25061/L25085).
  - KEEP L8 (locked install), L19 (watcher: still master-push only; the "Skipped full run" scenario L26-28 stays true for a docs-only master push iff push keeps the `backend` gate), L34, L41, L64, L76.
- `gateway-reconcile-test-partitioning` (4: 1 REMOVE, 1 MODIFY, 2 KEEP): REMOVE L9 "Targeted CI preserves gateway-reconcile ownership after partitioning" (L11 "The targeted-test selector SHALL replace the deleted monolithic target..."). MODIFY L42 "Moved gateway-reconcile dependencies retain their owning context": scenario "Compatibility consumers and documentation resolve moved tests" L43-47: drop the word "selector," from the list at L44 ("WHEN selector, entropy, demotion, ... checks run"). Purpose L5 mentions "targeted-CI ownership" -> edit. KEEP L50 (sacct), L64 (line guard).
- `replay-lineage-test-partitioning` (1 req): MODIFY L8. Cut scenarios "Targeted CI selects every partition" (L29-33) and "Support-module changes execute assertions" (L35-39). Purpose L4 "targeted-CI selection of every partition" -> edit. Remaining: collection identity, line guard, no retired-hook narrative.
- `guarded-display-module-partition` (6: 1 REMOVE): REMOVE L72 "Targeted CI selection covers every new partition" (L74-75 "pull-request targeted-test lane", scenarios L78-90 "selector runs ... guarded-module closure registry ... closure guard"). KEEP the other 5 (L110 "Importers and tests need no change" and L122 "unit tests" are ordinary).
- `orchestrator-structural-burndown` (34 req): MODIFY 6, REMOVE 2 (judgement), KEEP 26.
  - MODIFY L317 "Retention tests remain complete under physical partitioning": cut scenario "targeted CI owns every retention partition" L338-345 (L340 "targeted selection runs for ...retention.py", L343-345 "selector contract test fails").
  - MODIFY L477 "Basins package publication tests remain complete under physical partitioning": cut scenarios "production-owner selection reaches every publication partition" L500-507 and "helper-only selection reaches every consumer" L509-517 (L513 "selector's existing meta-guard rider"); scenario L519 (structural guard) has no selector wording: keep.
  - MODIFY, flagged "enforcement orphaned" (parent decides MODIFY vs REMOVE vs port the guards): L537 "QHH production-bootstrap tests remain complete under physical partitioning" and L609 "Basins registry-import tests remain complete under physical partitioning". These are about partition completeness (not selection), but their guard is the frozen oracles `tests/fixtures/qhh_bootstrap_partition_oracle.json` / `basins_registry_partition_{oracle,additions,retirements}.json` + ledger checker, all in `tests/test_select_ci_tests.py` (L18658-19531 QHH, L20778-23700 registry). Without that suite the 66/96-suffix identity claims are unenforced. Minimum MODIFY cut: selector scenarios L569-577 (QHH "production-owner and helper-only selection") and L679-691 (registry "Helper-only and production-owner selection"), L728 ("selector routes"), L762-763 ("retirements ledger path routes targeted selection to the partition meta-suite"); the oracle/ledger language L617-630, L641-652, L736, L741-760 only goes if the fixtures are deleted.
  - MODIFY L806 "Copyback replay and retention mutex corpora partition without drift": cut scenarios "targeted selection follows the retention owner split" L822-827 and "an orphaned partition cannot hide behind same-name derivation" L829-838 (names `PathTestRule`).
  - MODIFY L840 "Scheduler registry refresh surfaces split behind stable CLI facades": cut L856-858 "AND every new partition is named by an explicit `PathTestRule` ...".
  - MODIFY L898 "Production ops runbook becomes an index over sub-runbooks": cut L912 "AND the targeted selector routes the document's new paths."
  - MODIFY L964 "Refresh-timer and cutover-flip corpora partition without drift": cut L967-969 "SHALL route every partition through an explicit `PathTestRule`, and SHALL guard the corpus's exact tracked partition set" and scenario "a partition disappears from the tracked tree" L978-982 (names `tests/test_select_ci_tests.py`).
- `node27-test-oracle-lanes` (6: 1 MODIFY): MODIFY L8 "Oversized test suites are split without changing the collected set": cut scenario "Selector still routes every part" L15-17. "Collect-only parity" L11-13 is `pytest --collect-only` as a measurement, KEEP.
- `node27-isolated-probe-routing` (2: 1 MODIFY): MODIFY L6 requirement: cut scenario "Targeted PR selection remains honest" L25-28.
- `real-integration-test-matrix` (15: 1 MODIFY): MODIFY L440 "Repository pytest MUST fail on unhandled worker-thread exceptions": cut scenario "Config and lock changes execute the policy owner" L460-465 ("targeted selector includes ... selector meta-guard ... collect-only smoke").
- `scheduler-stall-alerting` (12: 1 MODIFY): MODIFY requirement L19 "A stalled DB-free scheduler SHALL become a non-zero unit without anyone reading an artifact", cutting (L36-43): cut the sentence "That parity test SHALL be routed in the repository's CI test selection so that it runs on a change to the service-layer definition; a parity test the selector cannot reach on the very diff that breaks parity is an after-the-fact detector, not a gate." (L40-43). Code mirror: `scripts/node22_scheduler_stall_health.py` L44, L135, L405 comments.
- `import-layering-guard` (4: 1 MODIFY): MODIFY L22 "A pytest AST guard SHALL forbid upward imports": cut the last sentence of L23 ("The test SHALL be selected by `scripts/select_ci_tests.py` whenever ... as those precedents did.") and scenarios "Guard runs for package changes" L37-39 and "Guard runs even under a stop-rule owner" L41-43.
- `lint-ratchet` (2: 1 MODIFY): MODIFY L19 "An `Any` count ratchet SHALL only decrease": cut last sentence of L20 ("The test SHALL be routed by `scripts/select_ci_tests.py` ... shadow).").
- `time-helper-consolidation` (3: 1 MODIFY): MODIFY L22 "A ceiling test SHALL forbid regrowth": cut last sentence of L23 ("The test SHALL be routed by `scripts/select_ci_tests.py` ... shadow).").
- `frontend-river-click-live-evidence` (5: 1 MODIFY): MODIFY L318: cut the clause at L335-336 "; frontend files SHALL not be routed through backend `scripts/select_ci_tests.py`" (keep "Existing frontend `pnpm test`/build ownership ... SHALL cover this change").
- Note also: specs that only PIN a spec-to-code literal (e.g. `api-contract-convergence`) were checked; they do not mention the selector.

After spec edits run `openspec validate --all --strict --no-interactive` (scripts/AGENTS.md L74-78 mandates). Change deltas must be expressed as an openspec change (REMOVED/MODIFIED requirements) - the files above are the baseline to edit.

## B. Code / test / doc references outside openspec

Scope used: `git grep -nI -e select_ci_tests -e ci_selector_snapshot -e PathTestRule -e CORE_SMOKE_TESTS` plus selector vocabulary (`SUPPORT_MODULE_TEST_RULES`, `CHANGED_TEST_FILE_RULES`, `PATH_TEST_RULES`, "selector", "meta-guard", `unit-test-targeted`, "targeted Unit Tests"). Excluding `openspec/`, `docs/runbooks/receipts`, `docs/review-loop-log.jsonl` per the brief (but see 0.4: they matter for the issue's own grep).

### B.1 Files to DELETE (6, as stated in the issue)
| file | lines | note |
|---|---|---|
| `scripts/select_ci_tests.py` | 892 | facade; imports `_imports`, `_rules`, `_tables` (L29, L38, L56) |
| `scripts/select_ci_tests_rules.py` | 5859 | rule tables; names fixtures at L3246, L3265, L3269 |
| `scripts/select_ci_tests_tables.py` | 1776 | constants (`FULL_REGRESSION_SHARD_*` L891-892, `LINE_REFERENCE_BASELINE_PATH` L878) |
| `scripts/select_ci_tests_imports.py` | 232 | importer-index AST parse |
| `scripts/ci_selector_snapshot.sh` | 93 | parent/head byte-equality oracle (#2948); only mentioned by itself and `scripts/select_ci_tests_rules.py` (1 hit) and the selector suite (1 hit) |
| `tests/test_select_ci_tests.py` | 25260 | 690 top-level test functions: EVERY function in it breaks/vanishes (imports `scripts.select_ci_tests`); no function needs individual handling |

### B.2 Orphan data to delete with them (only reader is the deleted suite)
- `tests/fixtures/basins_registry_partition_oracle.json` (82 KB), `tests/fixtures/basins_registry_partition_additions.json`, `tests/fixtures/basins_registry_partition_retirements.json`, `tests/fixtures/qhh_bootstrap_partition_oracle.json` (52 KB; it also contains the string `scripts/select_ci_tests.py` at L794). Verified: `git grep` for each name outside the selector files/suite hits only docstring comments (`tests/test_backfill_geometry_network_scope_integration.py` L6, `tests/test_basins_registry_import_qhh.py` L8, `docs/governance/API_CONTRACT_RETIREMENT_INVENTORY.md` L301/L303). No `tests/fixtures/*partition*` file is read by any other test.
- Decision for parent: deleting them drops the frozen collection-identity oracles for the registry-import and QHH-bootstrap partitions (spec A.3a orchestrator L537, L609).

### B.3 What else lives ONLY inside `tests/test_select_ci_tests.py` (silent loss on deletion; port or accept). NOT exhaustive: built from a test-name regex plus the spec citations; the file has 690 tests
ci.yml / workflow contract pins (def line numbers in the deleted file): `test_ci_concurrency_pins_pr_number_run_id_and_conditional_cancel` L11387 and the 12 `test_ci_concurrency_*` mutants L11403-11630; `test_ci_changed_files_authority_is_a_single_workflow_contract` L11448; backend-filter literal pins L4288, L4304, L4403, L4424, L4491, L5075, L25061; frontend-filter pins `test_ci_frontend_filter_block_lists_the_lane_switch_entry` L14022 and `..._pin_reds_when_the_entry_leaves_the_frontend_block` L14034; database-filter pins L2600, L14303, L14312, L16680, L17063, L17080, L19931-19996, L23615-23651; `test_real_db_job_contract_*` / `test_real_db_marker_expression_*` / `test_real_db_job_effective_*` L17423-17963 (dedicated DSN, marker expression `integration and not timescaledb_210`, step-override, continue-on-error); `test_workflow_path_matches_backend_and_database_filters` L17986; `test_ci_workflow_self_change_opens_the_backend_targeted_gate` L14339 + `..._reds_when_the_path_leaves_the_backend_block` L14377; targeted-job contract helpers L8980-9150 and L9939-10353 (`unit-test-targeted` job text/gate: L10009 "gate must be exactly backend == 'true' && pull_request"; `test_ci_workflow_consumes_the_collection_smoke_required_output` L10122). I found no other test that parses ci.yml's `real-db-integration` job or its concurrency block (`tests/test_ci_workflow_locked_install.py` checks installs only; `tests/test_governance_workflow_hard_gate.py` parses governance.yml only), so these pins are not duplicated. Spec requirements that depend on them: ci-contract-baseline L655 (KEEP), L414 (KEEP), L902 (MODIFY).
Tracked-tree / partition-completeness guard families that specs cite as living in this file (each silently disappears): retention partitions (spec orchestrator L317-345), copyback replay + retention mutex corpora (L806-838), refresh-timer corpus tracked-tree guard (L964-982), scheduler registry refresh partitions (L840-858), QHH bootstrap partition oracle/shape/digest tests (L18658-19531), basins-registry partition oracle + additions/retirements ledger checker (L20778-23700), gateway-reconcile and replay-lineage partition routes (gateway-reconcile-test-partitioning L9-31; replay-lineage L29-39), `GUARDED_MODULE_CLOSURES` importer-closure guards. Whether the 1,000-line guard (`.large-file-guard.json` + pre-commit) still protects the partitions is unaffected; collection-identity proofs are what is lost.
Other non-routing content in the same file: Any-ratchet / time-helper / import-layering `_SCAN_ROOTS` parity meta-tests, `GATING_MARKER_NAMES` vs conftest anchor (spec L465), `_parse_tracked` cache guard (spec L565) - all selector-internal.

### B.4 Non-deleted files that must be EDITED
Config / data
| file:line | reference | action |
|---|---|---|
| `.large-file-guard.json` L69 `scripts/select_ci_tests_tables.py`, L70 `scripts/select_ci_tests_rules.py`, L76 `tests/test_select_ci_tests.py` | exemptions for deleted files | delete the three entries (no test reads this file: only the pre-commit hook and frozen fixtures) |
| `pyproject.toml` L218 | `"tests/test_select_ci_tests.py" = ["B007", "B023", "B905"]` per-file ignore | delete line (ruff: ignores for a missing file are silent; delete for hygiene and for the issue grep) |
| `scripts/governance/entropy_audit/line_reference_baseline.json` L116-~L140 | block `"scripts/select_ci_tests_rules.py": {...}` frozen hard line refs | delete the block; `check_line_references` (scripts/governance/entropy_audit/check_line_references.py L46-58) iterates SCANNED files, so a stale key is inert, but `tests/test_entropy_audit_line_references.py` L231-236 only caps the total (cap can only be satisfied more easily). No change to cap needed. ci.yml L105-107 backend-filter literal for this file stays valid. |
| `scripts/ci/full_test_durations.json` L419 | `"tests/test_select_ci_tests.py": 334.343` | delete entry; also stale entry `tests/test_retry_runtime_root_blocked_reads.py` (file gone) and 99 on-disk files absent (see D) |
| `tests/status_literal_allowlist.json`, `tests/import_layering_allowlist.json`, `tests/time_helper_allowlist.json`, `tests/any_ratchet_ceiling.json`, `tests/time_helper_ceiling.json` | grep for `select_ci` / `ci_selector`: NO hits | nothing |
| `db/roles/node27_write_roles.sql` L715-717 | SQL comment "scripts/select_ci_tests.py maps db/migrations/*.sql ... that suite" | reword comment (the suite `tests/test_node27_write_roles.py` still pins the migration set; also its L1208 docstring "`scripts/select_ci_tests.py` carries the ...") |
| `infra/env/README.md` L121 (`tests/test_select_ci_tests.py` in the consumer list) and L125-130 ("On CI the authority is `scripts/select_ci_tests.py` ... `:2301-2304` ... `:2314-2317` ... `:496`") | live doc that tells contributors where the routing authority is | delete the selector bullet and the list entry; keep the "local oracle is the grep-derived consumer set" guidance |

Test infrastructure
| file:line | reference | action |
|---|---|---|
| `tests/conftest.py` L155-172 `pytest_unconfigure` | hook exists ONLY to clear `tests.test_select_ci_tests._PARSE_CACHE` (L169-172) | delete the whole hook; then `import gc` (L3) is unused (only use L172) -> delete or ruff F401 fires. No other file references `pytest_unconfigure` or `_PARSE_CACHE` (outside the selector's own `_SUITE_IMPORTER_PARSE_CACHE`). |
| `tests/conftest.py` L144, L291 | message cites `docs/runbooks/ci-test-routing.md` | KEEP (runbook is retained; `tests/test_conftest_grib_preflight.py` L56 asserts the path in the message; `tests/test_node22_entrypoint_invariant.py` L250-373 reads the runbook) |
| `tests/test_ci_workflow_locked_install.py` L5 (docstring "targeted Unit Tests"), L23 `REQUIRED_PYTEST_JOBS = ("real-db-integration", "unit-test", "unit-test-targeted")` | executable | remove `"unit-test-targeted"` from tuple (else `missing` violation) and fix docstring |
| `tests/test_ci_shard_tests.py` L288, L347-348, L358 (+ names at L283, L291) | executable pins of the matrix, gate string and 4 locked installs | update (C.5) |
| `tests/test_floating_resolve_report.py` L494-497 (test name L494 says "four") | `text.count(f"run: {LOCKED_SYNC}\n") == 4` | becomes 3 after `unit-test-targeted` deletion; also test name `..._four_locked_installs...` |
| `tests/test_full_regression_watch.py` L165, L446 (`[(60 - 2) // 5] * 4`), L449, L469 | literal 4 shards | update to the new shard count or derive from matrix (the watcher itself derives it, `full_job_shard_names`) |
| `tests/test_governance_workflow_hard_gate.py` L3-4 | docstring "ci.yml's `backend` path filter skips both pytest jobs for docs-..." | stale if `backend` filter semantics change; edit docstring |

Comment/docstring-only references to the selector in other tests and scripts (nothing breaks at import/run time, but the edits are MANDATORY: the issue's verification `git grep -n select_ci_tests` must return nothing). All verified comment/docstring, none import or execute the selector:
`tests/entropy_audit_helpers.py` L11-12 (+`SUPPORT_MODULE_TEST_RULES`); `tests/node22_entrypoint_helpers.py` L11-12; `tests/production_ops_runbook.py` L13-14; `tests/test_any_ratchet.py` L36-39; `tests/test_api_contract.py` L16 (`PathTestRule`) and L45 ("hosted targeted Unit Tests"); `tests/test_backfill_geometry_network_scope_integration.py` L8 (cites `tests/test_select_ci_tests.py:14486/...`); `tests/test_basins_registry_import.py` L10 (`BASINS_REGISTRY_IMPORT_TESTS`); `tests/test_chain_cross_stage_state.py` L40; `tests/test_convert_cohort_members.py` L116; `tests/test_direct_grid_evidence_smoke.py` L27; `tests/test_file_journal_read_blocked_consumers.py` L19, L22; `tests/test_file_orchestration_journal.py` L18389, L18511; `tests/test_forcing_read_path_store_routing.py` L39; `tests/test_hydro_display_mvt_scaling.py` L5-9; `tests/test_hydro_run_attempt_triage.py` L46; `tests/test_import_layering_guard.py` L39; `tests/test_node22_scheduler_stall_health.py` L82, L2470, L4102, L4656; `tests/test_node27_timeseries_compression_live_evidence.py` L7164; `tests/test_node27_write_roles.py` L1208; `tests/test_openapi_drift.py` L148 and `tests/test_precip_overlay.py` L1489 (selector-edge justification docstrings; the mutation-proof tests themselves stay); `tests/test_openapi_31_contract.py` L22, L43 ("targeted Unit Tests job has no pnpm/node_modules" - equally true of the full job; reword); `tests/test_operator_action_reservation_lease.py` L21; `tests/test_quarantine_forecast_restart_guards.py` L37; `tests/test_registry_parent_lock_order_integration.py` L129; `tests/test_river_segment_write_surface_scan.py` L66; `tests/test_river_ts_text_identity_cleanup.py` L124, L1432-1433; `tests/test_safe_fs.py` L24; `tests/test_safe_fs_lock.py` L14; `tests/test_scheduler_evidence_decidability.py` L19; `tests/test_scheduler_evidence_retention_reservation_mtime.py` L19; `tests/test_scheduler_lease_loop_root.py` L13; `tests/test_scheduler_refresh_deployment_contract.py` L5, L11-12; `tests/test_sql_shape_helpers.py` L35, L39 (`CHANGED_TEST_FILE_RULES`); `tests/test_sqlalchemy_driver_explicit.py` L25; `tests/test_time_helper_consolidation.py` L35-38; `tests/test_object_store_forcing_real_disk.py` L53 ("CI `--collect-only` smoke" - becomes untrue once the smoke is gone); selector-vocabulary-only (no `select_ci_tests` string): `tests/test_operator_action_listing.py` L2181, `tests/test_operator_action_status_closure.py` L14, `tests/test_qhh_output_stream_type_integration.py` L23.
scripts: `scripts/node22_scheduler_stall_health.py` L44, L135, L405 (comments naming the selector); `scripts/validate_bugs_ledger.py` L213 is unrelated (pytest `-k` selector) - KEEP.
Code that LOOKS related but is unaffected: `scripts/ci/shard_tests.py` (stays per issue item 4; its only mentions of the selector: none; header says "master full regression" L2 - reword), `scripts/ci/full_regression_watch.py`.

### B.5 Scoped agent instructions and docs
| file:line | what | action |
|---|---|---|
| `scripts/AGENTS.md` L10-11 | Required Reading lists `openspec/specs/ci-test-selector-routing/spec.md` | remove line 11 (spec deleted) |
| `scripts/AGENTS.md` L30-34 | "`scripts/select_ci_tests.py` is the PR test selector. Adding a test file ... needs a matching rule there; `tests/test_select_ci_tests.py` fails when a guarded owner gains an unrouted importer ..." | delete bullet; keep marker-routing pointer to `docs/runbooks/ci-test-routing.md`. This is the "tells a contributor to add a route" text |
| `scripts/AGENTS.md` L35-36 | `scripts/ci/` description | keep |
| `scripts/AGENTS.md` L84-95 | "When the selector changes, run its meta-suite: `uv run pytest -q tests/test_select_ci_tests.py`" and "For any other script, ask the selector which suites own the changed path: `printf ... | uv run python scripts/select_ci_tests.py`" | delete both blocks; the replacement guidance (`uv run pytest -q <suites>`) is the caller's choice |
| `packages/common/AGENTS.md` L78-82 | "has no directory rule in the CI selector, so ask the selector ..." with the `printf | select_ci_tests.py` command | reword to name the focused set already given at L84-90 |
| `workers/grid_registry/AGENTS.md` L88 | "(the directory has no selector rule)" | drop parenthetical |
| `workers/model_registry/AGENTS.md` L80 | "(the selector rule for this directory owns a wider set of suites)" | drop parenthetical |
| `instructions/agents/shared.md` L113-122 | source of CLAUDE.md/AGENTS.md; L116 "PR 上后端只跑定向测试: `unit-test-targeted` ... `select_ci_tests.py` ... `--collect-only` 冒烟"; L117 "全量 `unit-test` ... 只在 push master 或手动 `workflow_dispatch` 跑"; L118 (shard_tests, keep); L119 (shard count per ci.yml, keep); L122 "PR 绿 != 全量 pytest 通过: 全量回归是 merge 后 master run 才跑的事后发现" (now false); L115 "纯前端/纯 docs PR 不跑后端 pytest" (docs PRs now run it) | rewrite L115, L116-117, L122; regenerate `CLAUDE.md` L122-128 and `AGENTS.md` L122-128 (generated, do not hand-edit; `instructions/agents/{claude,codex}.md` are the other sources and contain no selector text). `tests/test_entropy_audit_instruction_inventory.py` etc. read AGENTS/CLAUDE; run `uv run pytest -q tests/test_entropy_audit_*.py` per scripts/AGENTS.md L74-78 |
| `docs/runbooks/ci-test-routing.md` L40-97 | "CI exclusion" section describes ONLY the sharded `unit-test` job and the marker expression; no selector text (title says "routing" = e2e/grib markers) | KEEP file (conftest + two tests pin it); L42-58 "4 is the shard count" and the `--total 4` repro (L64) -> new count; add that PRs also run it |
| `docs/runbooks/production-ops/stuck-detection.md` L514 | "`scripts/select_ci_tests.py` 在改 `scheduler_runtime.py` 的 PR 上选中" | reword |
| `docs/runbooks/tier-node27-timeseries-storage.md` L8829, L9320 | historical notes about selector mapping | reword or leave (see 0.4) |
| `docs/governance/API_CONTRACT_RETIREMENT_INVENTORY.md` L301-303, L347 | cites partition fixtures / "selector rules" | reword when fixtures deleted |
| `docs/governance/entropy-audit-2026-10-16.md` L60, L148, `.snapshot.json` L975 | dated audit snapshot ("select_ci_tests.py 7 623 lines") | historical; leave and exclude from the issue's grep, or accept as a documented exception |
| `docs/adr/0003-review-lens-rotation-keep.md` L3617, L3635, L3660, L3690 | ADR history | leave / exclude |
| `docs/review-loop-log.jsonl` (19), `docs/runbooks/receipts/*` (8 hits in 5 files) | frozen evidence | leave / exclude |
| `README.md`, `CONTRIBUTING*`, `.github/` templates, `Makefile`, `.pre-commit-config.yaml` | no selector reference found | nothing |

## Ratchets (checked: unaffected)
- Any ratchet (`tests/any_ratchet_ceiling.json` ceiling 9500; scans `apps packages services workers scripts`): computed with the ratchet's own `count_any`: current total 9268 (9268*20 = 185360 >= 9500*19 = 180500 so the 5% floor holds); the four selector scripts contribute 0 `Any`, so deleting them changes nothing and no ceiling move is needed.
- Time-helper ceiling (`tests/time_helper_ceiling.json` ceiling 0): `scan_hits` returns 10 hits (allowlisted), none in the selector scripts; unchanged.
- `tests/test_entropy_audit_line_references.py` baseline total cap only bounds from above; removing the `scripts/select_ci_tests_rules.py` block lowers the total.

## C. CI workflows

### C.1 `.github/workflows/ci.yml` `changes` job (L25-227; 674-line file)
Outputs (L29-38), all `steps.filter.outputs.*` from one `dorny/paths-filter@v3` step (L41, `id: filter` L42) with `list-files: json` (L50) and `filters:` from L51:
| output | filter (ci.yml lines) | consumers | only for the targeted job? |
|---|---|---|---|
| `backend` | L54-113: `**/*.py`, `pyproject.toml`, `uv.lock`, `tests/**`, `packages/**`, `apps/api/**`, `services/**`, `workers/**`, `infra/**`, `schemas/**`, `db/**`, `scripts/validate_*.py`, `scripts/**/*.sh`, `.github/workflows/ci.yml`, `openapi/**`, plus exact literals L66-113 (`.python-version`, `instructions/agents/shared.md`, `scripts/run_qhh_cycle.sbatch`, `scripts/diagnostic/qhh/README.md`, `docs/runbooks/tier-node27-timeseries-storage.md`, `config/calibration_overrides.yaml`, `.review-gate-issues.json`, `docs/review-loop-log.jsonl`, `.github/workflows/full-regression-watch.yml`, `.github/workflows/governance.yml`, `scripts/governance/entropy_audit/line_reference_baseline.json`, `.github/workflows/floating-resolve-report.yml`, `scripts/ci/full_test_durations.json`) | `unit-test` L414-417 (push leg), `unit-test-targeted` L485-488 | Shared. The exact-literal block L70-113 exists solely so "producer-only/workflow-only/JSON-only" PRs open the TARGETED gate (comments L70-76, 82-85, 87-92, 94-98, 99-103, 105-112 say "targeted"/"backend lane"). It is dead weight for PRs once PRs run the full suite on any non-frontend diff; it only still matters if `push` keeps the `backend` gate. Note L79 (`instructions/agents/shared.md`) and `scripts/ci/full_test_durations.json` L113 are among them |
| `database` | L114-199 (`db/**`, ~60 literals + `tests/*integration*.py`, `tests/**/*integration*.py`, `tests/test_real_*.py`, partition-owner literals ~L165-199, `.github/workflows/ci.yml`, `pyproject.toml`, `uv.lock`, `tests/conftest.py`) | `real-db-integration` L361-367 only | NO - independent, keep as is (pins live in the deleted suite, B.3) |
| `frontend` | L200-210 (`apps/frontend/**`, `openapi/**`, two C4 schema globs, `.github/workflows/ci.yml`) | `frontend-build` L592, `frontend-e2e` L625 | no |
| `docs` | L211-212 (`docs/**`) | `markdown-lint` L232 | no |
| `openapi` | L213-214 | `openapi-validate` L244 | no |
| `schemas` | L215-216 | `json-schema-validate` L312 | no |
| `node_tests` | L220-225 | `script-node-tests` L265 | no |
| `workflows` | L226-227 | `workflow-lint` L283 | no |
| `all_files` | filter `all: ['**']` L52-53 + `list-files: json` -> JSON array of ALL changed paths | ONLY `unit-test-targeted` L519, L529-530 (`CHANGED_FILES_JSON` -> `.ci-changed-files.txt` -> selector) | YES - the sole targeted-only output; the `all` filter (L52-53) and the L43-49 comment about "single authority for both paths-filter and targeted selection (#1650 D3)" also exist for it |
`list-files: json` (L50) is only needed for `all_files`; with the selector gone it can be dropped unless the new "non-frontend" signal is computed from it (C.6). No other job `needs:` anything but `changes` (verified; `workflow-lint` comment L283-285 forbids dependants).

### C.2 `unit-test` (L411-477, "Unit Tests (full)") vs `unit-test-targeted` (L482-587, "Unit Tests")
| aspect | `unit-test` (full) | `unit-test-targeted` |
|---|---|---|
| `needs` | `changes` L413 | `changes` L484 |
| `if` | L414-417 `workflow_dispatch \|\| (backend=='true' && push)` | L485-488 `backend=='true' && pull_request` (no dispatch, no draft check) |
| runs-on | ubuntu-latest | ubuntu-latest |
| strategy | matrix `shard: [1, 2, 3, 4]`, `fail-fast: false` L428-431 | none |
| timeout-minutes | 30 per shard L438 (pinned by `tests/test_ci_shard_tests.py` L298) | 35 L492 |
| services / container / job env | none | none (same) |
| checkout | `fetch-depth: 0` L442 | `fetch-depth: 0` (same) |
| setup | setup-python 3.11, setup-uv@v5, `uv sync --locked --all-extras --dev`, venv on `$GITHUB_PATH`, scratch evidence root (`sudo mkdir /scratch/frd_muziyao`) | identical five steps (L494-514 vs L440-461) |
| selection | step "List this shard's test files" L462-468: `python scripts/ci/shard_tests.py --shard ${{ matrix.shard }} --total ${{ strategy.job-total }} > shard-files.txt; test -s shard-files.txt` | steps "Select targeted tests" L516-530 and "Run targeted tests" L531-587 (`jq` on `all_files` -> `python scripts/select_ci_tests.py --changed-file ... --github-output`) |
| pytest | L470-477: `pytest "${shard_files[@]}" -q --tb=short --durations=25 -m "not e2e and not grib and not integration"` | L531-587: `pytest -q <selected files>` - NO `-m`, no `--tb`, no `--durations`; opt-in markers are handled only by `tests/conftest.py` `pytest_collection_modifyitems` L96-121 auto-skip (integration, e2e, grib skipped without env) |
| extras only in targeted | none to port | (a) `pytest tests/ -q --collect-only` smoke when count==0 (L570-586, zero assertions) or `collection_smoke_required` (L537-567; pytest at L562); (b) `$GITHUB_STEP_SUMMARY` + `::warning` annotations (selector meta-guard / selector-development / 0 assertions); no integration tests, no extra smoke beyond collect-only |
Porting facts: the full job already contains everything the targeted job needs. The only behavioural differences: (1) targeted does not pass `-m`; full excludes `e2e`, `grib`, `integration` by deselection (deselected vs skipped), (2) `--tb=short --durations=25` only in full. `scripts/ci/shard_tests.py` collects files from disk (`testpaths=["tests"]`, pyproject L237) so integration-only files are still scheduled; their nodes are deselected under the full job's `-m`.

### C.3 `concurrency` block (ci.yml L12-19)
```
# PR runs share one group per PR number and cancel superseded runs ... the master full pytest is the only whole-repo
# regression after the PR targeted lane (#1650).
concurrency:
  group: ci-${{ github.workflow }}-${{ github.event_name == 'pull_request' && github.event.pull_request.number || github.run_id }}
  cancel-in-progress: ${{ github.event_name == 'pull_request' }}
```
Semantics stay correct for full-suite PR runs (superseded PR run cancelled; push/dispatch isolated per `run_id`). Only the comment L16-17 ("only whole-repo regression after the PR targeted lane") becomes false -> reword. Spec ci-contract-baseline L655 requirement text is unchanged. Pins on this block are in the deleted suite (B.3).

### C.4 What `real-db-integration` (L358-409, "SQL Migration Dry Run") depends on
`needs: changes` only; `if` L361-367: `workflow_dispatch || (database=='true' && (push || pull_request.draft == false))`. Uses `outputs.database` only. No dependency on `unit-test*`, on `backend`, or on `all_files`. Its comment L354-357 ("Full unit pytest is reserved for push-to-master or manual workflow_dispatch; PRs run the focused unit-test job below") is the only text to update. Marker `integration and not timescaledb_210` at L407-409 is untouched.

### C.5 Tests that read ci.yml and what #3219 changes in them
| test | line | pin | effect |
|---|---|---|---|
| `tests/test_ci_shard_tests.py` | L284-290 | matrix == `{"shard": [1,2,3,4]}` (test name says four) | edit to new shard list |
| same | L293-299 | `timeout-minutes == 30` | edit only if timeout changes |
| same | L335-352 `test_full_job_keeps_its_gate_and_prerequisite_steps` | `job["if"]` == `"github.event_name == 'workflow_dispatch' \|\| (needs.changes.outputs.backend == 'true' && github.event_name == 'push')"` | MUST be rewritten to the new gate |
| same | L355-358 | `text.count("run: uv sync --locked --all-extras --dev\n") == 4` (lint-ruff, real-db, unit-test, unit-test-targeted) | becomes 3 |
| same | L361-460, L481-482, L511-570 | other lanes (frontend e2e, node tests, workflow-lint) | unaffected; L473/L567 assert names do not start with "Unit Tests" (still true) |
| `tests/test_ci_workflow_locked_install.py` | L23 | `REQUIRED_PYTEST_JOBS` includes `unit-test-targeted` | edit |
| `tests/test_floating_resolve_report.py` | L494-497 | exactly 4 locked installs | becomes 3 |
| `tests/test_full_regression_watch.py` | L165, L446, L449, L469 | literal 4 shards | edit (watcher derives shard count itself) |
| `tests/test_governance_workflow_hard_gate.py` | L3-4 | docstring only | edit |
| `tests/test_node22_entrypoint_invariant.py`, `tests/test_conftest_grib_preflight.py` | - | read `docs/runbooks/ci-test-routing.md` | keep runbook |

### C.6 Minimal `if:` proposal (facts only)
Neither existing output can express "diff is not only `apps/frontend/**`": `frontend` is true also for `openapi/**`, C4 schemas and ci.yml (L200-210), and `all_files` is a JSON string (the Actions expression language cannot filter an array by prefix). Options:
(a) one NEW `changes` output computed in a shell step after the filter, e.g. `non_frontend` = `jq -e 'any(.[]; startswith("apps/frontend/") | not)' <<< "$ALL_FILES"` written to `$GITHUB_OUTPUT` (needs `all_files` retained, so `list-files: json` stays). Works with the current action, touches no other filter.
(b) a second paths-filter step (separate `id`) with `predicate-quantifier: every` and patterns `'**'` + `'!apps/frontend/**'`. `predicate-quantifier` is an action-level input (applies to all filters of that step), so it cannot share the existing step without changing every other filter's semantics. (I did not verify the pinned `@v3` tag supports it; confirm before relying on it.)
Gate with (a):
```
if: >-
  github.event_name == 'workflow_dispatch' ||
  (github.event_name == 'push' && needs.changes.outputs.backend == 'true') ||
  (github.event_name == 'pull_request' && needs.changes.outputs.non_frontend == 'true')
```
(keep the existing push leg verbatim so master behaviour is unchanged; if master pushes should also run on docs-only diffs, replace the push leg with `github.event_name == 'push'` and then `backend` has no remaining consumer and the whole L56-115 filter can be deleted). Empty changed-file list on a PR (e.g. rebase noise) evaluates `any(...)` to false -> skipped; if that is unwanted use `all(...startswith("apps/frontend/")) | not` with an explicit empty check. `tests/test_ci_shard_tests.py` L343-348 pins the gate string verbatim.
Also needed: delete `unit-test-targeted` (L479-587 incl. its L479-481 comment), reword the L471-472 comment ("unit-test-targeted already uses -q") and the L354-357 comment; rename nothing else (job key `unit-test` and `name: Unit Tests (full)` are pinned by the watcher: `scripts/ci/full_regression_watch.py` L56, L60, L231-248).

### C.7 `full-regression-watch.yml` and `scripts/ci/full_regression_watch.py`
- Shard names/count: `full_job_shard_names()` (`scripts/ci/full_regression_watch.py` L231-248) reads `jobs.unit-test` from ci.yml, requires static `name == "Unit Tests (full)"` (L56 `JOB_NAME`), exactly one matrix dimension `shard` with a literal list (L242-247), and returns `f"Unit Tests (full) ({value})"`. So raising the shard count in ci.yml needs NO watcher code change; `full_job_timeout_minutes()` (L226-228) reads `timeout-minutes` from the same job for the 80% margin (`MARGIN_THRESHOLD = 0.8`, L64).
- PRs running the same job names: the watcher is unaffected functionally. Workflow trigger `workflow_run` is `branches: [master]` (yml L13) and job `if` is `workflow_dispatch || workflow_run.event == 'push'` (yml L29); `check-run` default listing queries `branch=master&event=push&status=completed` (script L207); `margin` queries `event=push` (script L303). PR runs are never sampled, so the P95 margin stays master-only.
- Must change: yml header comment L3-7 ("`Unit Tests (full)` runs only on master pushes, after the PR checks were already green"), `scripts/ci/full_regression_watch.py` module docstring L2 ("master `Unit Tests (full)` shards") if you reword; spec ci-merge-gates L19-33 (KEEP) and L52 (MODIFY). Watcher tests: `tests/test_full_regression_watch.py` literal 4 (C.5). The watcher "Skipped full run" shape (one skipped `Unit Tests (full)` job) is still produced by master push with `backend=='false'`.
- `.github/workflows/governance.yml`: no selector reference (grep: only an unrelated "backend-lane" comment L108). L106-108 comment says the pytest node "remains the backend-lane check". `.github/workflows/floating-resolve-report.yml` L40 comment "same prerequisites as ci.yml's `unit-test` job" stays true.

## D. Shard sizing (computed read-only with `scripts/ci/shard_tests.py`; script in the scratchpad `shards.py`)

Method: `collect_test_files()` (570 test files on disk), `load_durations()` (471 entries, total 2483.6 s), `partition()` = the production greedy LPT. The CLI output (`uv run --no-sync python scripts/ci/shard_tests.py --shard i --total 4`) was compared to `partition()` for N=4 and is identical, so the numbers below for 4/6/8 use `partition()` directly. Files absent from the table get the table MEDIAN weight (`default_weight`, L89-90 of shard_tests.py) = 0.671 s today (0.671 after removal too within rounding). Assignment: sort by (weight desc, path asc), put each on the currently lightest shard, lowest index on ties.

Table hygiene: 99 of the 570 files on disk are NOT in `full_test_durations.json` (hence weighted 0.671 s each; includes `tests/test_ci_shard_tests.py`, `tests/test_any_ratchet.py`, `tests/test_import_layering_guard.py`, `tests/test_time_helper_consolidation.py`, `tests/test_status_literal_scan.py`, the `*_integration.py` files, the node22/node27 split partitions); 1 entry is stale (`tests/test_retry_runtime_root_blocked_reads.py`). Many missing files are `*_integration.py` (deselected under `-m`, true cost ~0) but the non-integration ones are under-weighted; the table's `Unit Tests (full)` sum 2483.6 s matches the comment at ci.yml L431-436 (2484 s).

Per-shard summed seconds (estimated test seconds, excluding ~1-2 min of checkout/uv sync/venv setup per shard):
| config | N=4 | N=6 | N=8 |
|---|---|---|---|
| as-is (selector suite 334.343 s still in table) max / min shard | 637.5 / 637.5 | 425.0 / 425.0 | 334.3 / 316.5 |
| as-is, per-shard sums | 637.5 x4 | 425.0 x6 | 334.3, 316.5 x7 |
| selector suite removed (file + table entry) max / min | 553.8 / 553.8 | 369.2 / 369.2 | 276.9 / 276.9 |
| per-shard sums | 553.8 x4 | 369.2 x6 | 276.9 x8 |
| removed from files only (table entry left) | 553.9 | 369.3 | 276.9 |
Files per shard (as-is): N=4 [140,143,144,143]; N=6 [88,94,96,97,97,98]; N=8 [1,75,81,82,82,83,83,83] (as-is, N=8, shard 1 is ONLY `tests/test_select_ci_tests.py`). Removed: N=4 [141,142,143,143]; N=6 [92,94,95,95,97,96]; N=8 [62,71,72,72,73,73,73,73].

Lower bound per shard = largest single file. Top files by table seconds: `tests/test_select_ci_tests.py` 334.343 (removed by #3219), `tests/test_production_scheduler.py` 226.986, `tests/test_two_node_e2e_evidence.py` 144.047, `tests/test_scheduler_refresh_installer_failure_paths.py` 114.405, `tests/test_orchestration_chain.py` 94.202, `tests/test_node22_refresh_timer_health_installer.py` 61.282, `tests/test_entropy_audit_report_contract.py` 60.087, `tests/test_scheduler_refresh_installer_mutations.py` 58.912. After removal the floor is 227.0 s (so more than ~10 shards stops helping: 2149.3 s of total test time / 227 s ~= 9.5). As-is the floor is 334.3 s, which is why N=8 as-is is not balanced (one shard holds only that file).

Relation to the issue's goal ("PR wall time near today's median 525 s"): removing the selector cuts the busiest shard by 83.7 s at N=4 (637.5 -> 553.8). CAVEAT: these are LOCAL-run seconds (table measured 2026-10-04 on one local full run, ci.yml L431-436: 41.7 min single process vs 39-59 min hosted single job), so hosted wall time = ~0.9-1.4x these numbers plus ~1-2 min setup per shard (checkout fetch-depth 0, `uv sync --locked`, scratch dir). Raw test-second equivalents: ~9.2 min (N=4), ~6.2 min (N=6), ~4.6 min (N=8). The issue states 10-19 min hosted for 4 shards today; real shard wall times should be pulled with `gh run view <master run> --json jobs` before choosing N. `timeout-minutes: 30` (ci.yml L438) is pinned to 30 by `tests/test_ci_shard_tests.py` L298; the watcher's margin line warns at 80% of it (24 min).
Edits needed for a new N: `matrix.shard` list in ci.yml L431 (and L642 is the SEPARATE frontend-e2e matrix, leave); `tests/test_ci_shard_tests.py` L288; `tests/test_full_regression_watch.py` literals (C.5); `docs/runbooks/ci-test-routing.md` L58-64 ("4 is the shard count", `--total 4`); optionally refresh the durations table via `shard_tests.py durations --junit` (runbook L81-97) and delete the selector entry (JSON L419).

## E. Other readers of selector output / assumptions that "PR runs targeted tests"

Searched docs, tests, scripts, workflows for "collect-only", "Unit Tests (full)", `unit-test-targeted`, "targeted tests", "targeted selection", "定向测试", `meta_guard_only`, `collection_smoke_required`, `count`/`tests_json` outputs. Beyond A, B, C:
- `.github/workflows/ci.yml` L532-587 is the ONLY consumer of the selector's GitHub outputs (`tests_json`, `count`, `meta_guard_only`, `collection_smoke_required`); nothing else (no other workflow, no script) reads them.
- `tests/conftest.py` L163 docstring ("`--collect-only` runs no fixtures ...") is part of the deleted `pytest_unconfigure` (B.4).
- `tests/test_object_store_forcing_real_disk.py` L53: "the CI `--collect-only` smoke imports ..." - stale once the smoke is gone (comment).
- `tests/test_api_contract.py` L45, `tests/test_openapi_31_contract.py` L22 and L43, `tests/test_ci_workflow_locked_install.py` L5: "targeted Unit Tests job" wording (comments; the properties they describe - no pnpm install/node_modules, locked venv - hold equally for `unit-test`).
- `instructions/agents/shared.md` L113-122 / generated `CLAUDE.md` L122-128, `AGENTS.md` L122-128: the only doc statements of "PR runs targeted tests" and "PR green != full pytest passed" (B.5). The CLAUDE.md "CI 门控要点" sentence "纯前端/纯 docs PR 不跑后端 pytest" (shared.md L115) is also wrong after #3219 (docs diffs now run it; only apps/frontend-only skips).
- `docs/runbooks/ci-test-routing.md`: no selector text (confirmed by grep for select/route/targeted); L42 and L58-64 need the PR/shard-count update only.
- No file under `docs/` other than those in B.5 mentions "targeted" CI selection; `docs/adr/0003-review-lens-rotation-keep.md` L3617-3690 discusses a selector miss historically.
- `openspec/changes/archive/**` is excluded by the issue's own grep (unchanged). Active (non-archive) `openspec/changes/*` has ZERO `select_ci_tests` hits today (the 489-hit count in 0.4 lists none there); re-run `git grep -n select_ci_tests -- openspec/changes ':!openspec/changes/archive'` before archiving this change.

## F. Execution order suggestion (facts-based, not design)
1. Specs: delete `ci-test-selector-routing`; apply A.2/A.3a (MODIFY/REMOVE) as an openspec change; `openspec validate --all --strict --no-interactive`.
2. Delete B.1 + B.2 files; edit B.4 config (`.large-file-guard.json`, `pyproject.toml`, `line_reference_baseline.json`, `full_test_durations.json`), conftest hook + `import gc`.
3. ci.yml: new `changes` output (C.6), new `unit-test` gate + matrix size, delete `unit-test-targeted`, comments (C.3, C.4); update tests in C.5 in the same push.
4. Comments/docs: B.4 comment-only list, B.5; regenerate CLAUDE.md/AGENTS.md from `instructions/agents/shared.md`.
5. Verify: `uv run ruff check . --no-cache`; `actionlint`; `workflow_dispatch` full run green on every shard; `git grep -n select_ci_tests` with the amended pathspec (0.4).
