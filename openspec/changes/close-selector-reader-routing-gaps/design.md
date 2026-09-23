## Context

`scripts/select_ci_tests.py` picks the targeted PR-lane backend tests. It has three rule tables: `PATH_TEST_RULES` (first match wins, with `stop_on_match` rules), `CHANGED_TEST_FILE_RULES` and `SUPPORT_MODULE_TEST_RULES`. After those, `select_tests()` runs supplemental additive loops: #1656 timescale write-guard roots, #2185 river-segment write surface, and #1627/#2452 path-canonicalization plus resolve surface. The selector is stdlib-only and imports no repository module. That is why a mirror of an external authority must be pinned by a meta-test rather than imported.

All five gaps below share one shape. A guarded input changes in a PR, the guard's suite is not selected, and the guard only fires in the post-merge master full run.

## Goals / Non-Goals

- Goals:
  - Each of the five inputs selects its guarding suite in the PR lane.
  - Every new route is additive. No existing target is lost for any tracked path.
  - Every mirror of an external authority fails a meta-test when it drifts.
- Non-goals:
  - Changing scanner classification (entropy), `FORCING_REGISTRY` contents, or guard semantics.
  - Changing the CI `changes` path filter or targeted/full gating (see D6).
  - Widening the importer-gap audit to function-body imports (#2390 boundary).
  - Relocating or rewriting the gateway-reconcile suites.

## Decisions

### D1 (#2316): extend `FILE_JOURNAL_READ_STATE_PATH_PATTERNS[11]` at the rule site

Add `"tests/test_retention_extra_roots.py"` to the pattern[11] target tuple and replace the "#2260 sibling gap stays open" sentence of the rule comment with the closure note.

`FILE_JOURNAL_READ_STATE_TESTS` is spliced into every journal pattern, so editing it would move eleven selections. Pattern[9] (`scheduler.py`) and pattern[10] (`scheduler_core.py`) stay unchanged. The trade-off, recorded at the rule site:
- The protected wiring (`runs_only_roots=(os.getenv("WORKSPACE_ROOT"), self.config.object_store_copyback_root)`) lives in `scheduler_runtime.py`.
- `_pass_scheduler` imports `ProductionScheduler` from `scheduler.py` only to construct the object under test.
- A regression of the extra-root deletion surface therefore has to touch `scheduler_runtime.py`.
- Widening [9]/[10] would move two more selections for no new failure it could catch.

### D2 (#2390): ruling A — the five writer suites ride pattern[3] at the rule site

**Ruling A.** Append the five suites to the pattern[3] (`file_orchestration_migration.py`) target tuple:
- `tests/test_gateway_reconcile_writer_prepare.py`
- `tests/test_gateway_reconcile_writer_launch.py`
- `tests/test_gateway_reconcile_writer_rollforward.py`
- `tests/test_gateway_reconcile_writer_receipts.py`
- `tests/test_gateway_reconcile_writer_quiescence.py`

Update that module's `STOP_RULE_AT_SITE_EXTENSIONS` row. Record in the rule comment:
- Why the edit is at the rule site: the shared constant serves eleven other patterns.
- Why the importer-gap audit could not see the gap: the imports are function-body imports, and `_non_gated_top_level_importer_index` walks module-level imports only.
- The re-measured count and wall clock.

**Rejected alternatives.**
- **B (comment-only disposition).** The #2390 issue body lays out why:
  - `INTENTIONAL_RULE_GAP_EXCLUSIONS` keys are derived `(module, suite)` importer pairs;
  - these five pairs never derive, so an exclusion entry would redden the audit as stale;
  - a comment-only disposition is not machine-checked.
- **C (subset / node ids).** 34 of the 36 top-level tests touch the module, so no subset boundary is defensible.

**Cost.** The issue measured 77 tests / 15.70s on macOS. That sits between the existing at-site riders (under 2s) and `CHAIN_IMPORTER_TESTS` (~193s). The stop rule must still stop: `tests/test_state_clone.py`, a `services/orchestrator/**` broad-list member, must stay absent from the selection.

### D3 (#2317): path-exact rules for the frozen oracles and the station-series baseline

- **Frozen oracles.** Add two path-exact `PathTestRule`s next to the #2183 additions-ledger rule, one each for `tests/fixtures/basins_registry_partition_oracle.json` and `tests/fixtures/qhh_bootstrap_partition_oracle.json`. Each targets exactly `("tests/test_select_ci_tests.py",)`. Neither uses `stop_on_match` or `only_when_any_changed`.
- **Station-series baseline.** Add one path-exact rule for `tests/fixtures/station_series_baseline_heihe_ifs_2026060100.json` targeting `("tests/test_object_store_forcing.py",)`. That is its only non-gated reader, at `:805`. The e2e/real-disk reader is marker-gated and deselected in CI anyway. The #2317 issue body offered this adjacent gap to the implementer's discretion. It is the same failure class and costs a single rule, so it is closed here rather than refiled.
- **Why not a glob.** A `tests/fixtures/*_partition_*.json` glob would change the shape #2313 just pinned and could absorb a future non-meta reader.
- **Declined: "every tracked fixture routed" meta-guard.** It is not in any acceptance criterion. It would also force an allowlist for fixtures whose readers are legitimately gated (YAGNI).

### D4 (#2323): supplemental production-topology reader route

**What is added.** A new supplemental additive loop sits after the #1627 loop. It is driven by module-level mirror constants:

- `PRODUCTION_TOPOLOGY_SCAN_ROOTS = ("scripts", "infra/env", "instructions/agents", "docs/governance", "docs/runbooks", "openspec/changes", "openspec/specs")`
- `PRODUCTION_TOPOLOGY_SCAN_FILES = ("AGENTS.md", "CLAUDE.md", "infra/README.two-node-docker.md", "openspec/project-profile.md")`
- the skip predicate mirrored from `repo_files._repo_relative_path_is_skipped`:
  - any part in the `SCAN_SKIP_DIRS` mirror;
  - any part starting with a `SCAN_SKIP_PREFIXES` entry;
  - first part in `SCAN_SKIP_ROOT_DIRS`;
- the text-name predicate mirrored from `_has_scannable_text_name`:
  - suffix in the `TEXT_EXTENSIONS` mirror;
  - or name in `{Makefile, .gitignore, .dockerignore, .env}`;
  - or name starting with `.env.`.

A changed path that is under a root, or equal to a direct file, and that passes both predicates adds exactly one target, the hard-gate node id. The path does not have to exist; a deletion changes the scan too.

**Target choice.** The node id is `tests/test_entropy_audit_report_contract.py::test_entropy_audit_current_repo_hard_gate_has_zero_production_topology_findings`.
- The issue AC names `tests/test_entropy_audit_script.py`, which no longer exists after the #1823 split. The hard gate now lives in `tests/test_entropy_audit_report_contract.py`.
- The node takes about 20s; the whole partition takes about 97s.
- When the whole file `tests/test_entropy_audit_report_contract.py` is already selected (every entropy scanner-module rule selects `ENTROPY_AUDIT_TESTS`), the node id is not added. The file run already executes it, and a duplicate target must not appear.

**Deliberately not mirrored.** The topology checker's own path exclusions (`archive`, `archived`, `receipt(s)`, `scripts/governance/**`, `check_topology.py:203-207`) are not mirrored; those paths are over-selected on purpose, which is cheap and never under-selects. The size cap (`MAX_SCANNED_TEXT_FILE_BYTES`) and the symlink / regular-file checks are left out. They need a stat of the changed file, and dropping them can only over-select. Over-selection is the safe direction, and a file over 1 MiB under these roots is exceptional.

**Why not import the scanner.** The selector must stay stdlib-only. The roots are also local to `_production_topology_scan_files` rather than module constants, and the scanner is not edited (issue boundary). So drift is caught behaviourally:

1. The meta-test pins the mirrored skip/extension constants equal to `scripts.governance.entropy_audit.constants`. It is allowed to import repo modules.
2. A synthetic tree under `tmp_path` holds:
   - one scannable file under each mirrored root;
   - each direct file;
   - negatives: an unsupported extension (`.log`), a skipped part (`node_modules`, `.nhms-x`), and out-of-root files (`openapi/nhms.v1.yaml`, `tests/x.md`, `docs/other/x.md`). The `SCAN_SKIP_ROOT_DIRS` arm is unreachable under the seven roots; it is kept for fidelity and covered by the constant pin only.

   The meta-test asserts that the relative-path set `_production_topology_scan_files(tmp_path)` yields equals the set the selector mirror accepts over the same candidate list.
3. A real-tree check compares the mirror with the scanner:
   - every path `_production_topology_scan_files(REPO_ROOT)` yields must be accepted by the mirror (the dangerous drift direction: a newly added scanner root with files reddens here);
   - every tracked file the mirror accepts but the scanner does not yield must be one the scanner rejects only for size, symlink or non-regular type.
4. A root-drop monkeypatch observed through `select_tests` proves the constant is the live authority.

**Known limit.** A scanner root added over a directory that holds no scannable file yet is not seen until a file lands there. That is harmless, because such a root scans nothing.

### D5 (#2498): supplemental forcing-template import sniff

**Mirror constants.** Add `FORCING_TEMPLATE_DISCOVERY_ROOTS = ("packages", "workers", "scripts", "services", "apps", "db")` and `FORCING_TEMPLATE_PRUNED_DIRECTORIES`. Both are pinned equal to `tests/forcing_ts_template_registry.py`'s `DISCOVERY_ROOTS` and `_PRUNED_DIRECTORIES` by a meta-test.

**The loop.** A new supplemental additive loop handles each changed path that ends in `.py`, whose first part is in the roots, and that has no pruned part:
1. Read and parse `repo_root / path`.
2. If `"packages.common.forcing_ts_render"` is in `_top_level_imported_module_names(path, tree)`, add `FORCING_SQL_SHAPE_ORACLE_TESTS`.

The existing helpers resolve all four import forms: `import packages.common.forcing_ts_render`, `from packages.common import forcing_ts_render`, `from packages.common.forcing_ts_render import X`, and relative `from .forcing_ts_render import X` inside `packages/common/`.

**Fail-open on unreadable input.** A missing file, a non-regular file, `OSError`, `UnicodeDecodeError` or `SyntaxError` falls through silently. A deleted or renamed path cannot construct a pair.

**Why not route every backend path.** The #2498 alternative, riding all six roots, was rejected by the #1990 cut (b) review on cost: three suites, ~23s, on every backend path.

**Known limit — imports the sniff does not see.** `_top_level_imported_module_names` walks `tree.body` only, deliberately (#1561). It therefore misses:
- function-body imports;
- imports nested under module-level `if` / `try`;
- attribute access through `from packages import common`.

The guard itself is fail-closed on function-scope pairs (`tests/forcing_ts_template_registry.py:698-704`), so these cases red on master, not in production. The helper is not widened because other consumers depend on its #1561 semantics. This is the same function-body blind spot that #2390 records.

### D6: CI trigger scope is not changed (limit of #2323 AC5, follow-up #2602)

The `.github/workflows/ci.yml` `backend` filter (`:52-90`) already covers `infra/**`, `**/*.py` and `scripts/**/*.sh`. It does not cover:
- `openspec/**`;
- `docs/governance/**`;
- `docs/runbooks/**`, apart from one literal;
- `instructions/agents/**`, apart from `shared.md`;
- `AGENTS.md` and `CLAUDE.md`;
- non-`.py`/`.sh` text under `scripts/`.

`unit-test-targeted` and `unit-test` (full) both require `needs.changes.outputs.backend == 'true'`, including on master pushes (`ci.yml:289-294`). A diff that touches only those paths therefore runs the hard gate on neither the PR nor master. The three exact literals the filter does include (`instructions/agents/shared.md`, `docs/runbooks/tier-node27-timeseries-storage.md`, `scripts/diagnostic/qhh/README.md`) do open the lane, and for them the route is live even on a docs-only PR. The first red would presumably land on the next backend-touching master push.

This means the new route fires only when the diff also contains a backend path. That was the #2321 shape. Widening the trigger is a CI gating-strategy change, which #2323 explicitly puts out of scope. It is filed as #2602, and #2602's recommendation is a hard-gate step in `governance.yml`.

## Risks / Trade-offs

- **Added lane cost.**
  - About +20s (the node builds one full-repo report) on effectively **every** backend PR. The repo's CI-cost discipline puts `openspec/changes/**/tasks.md` in the final push of each PR, and that path is a scan input. The node id is already the cheapest target that runs the gate.
  - The re-measured cost of the five writer suites on `file_orchestration_migration.py` diffs.
  - `tests/test_retention_extra_roots.py` on `scheduler_runtime.py` diffs.
- **Contract flips in the meta-suite.**
  - Docs and root-instruction paths under the scan roots that used to select `[]` now select exactly the hard-gate node (`test_generated_roots_and_unrelated_docs_stay_selector_empty`, `test_select_tests_ignores_docs_only_changes`). Those tests are retargeted and renamed, with the premise comment rewritten, not widened in place.
  - The two D3 oracle routes select exactly the meta-guard, so `meta_guard_only=true` and the collection smoke runs for them. This is the #2183 precedent, not a regression.
- **Exact-set pins in the main spec move.** Both requirements the delta MODIFIES are restated with measured selections in every scenario. Their other scenarios are already stale on master (the pre-#2259 mutex file name; the provider-refresh owner suites), and the delta names that.
- **Residual scope.** The hard-gate node asserts all 13 `HARD_GATE_CHECK_IDS`. D4 routes only the production-topology inputs, so other gated families' inputs stay unrouted; that is outside #2323.
