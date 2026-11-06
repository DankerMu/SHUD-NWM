## Fixture

- Issue: #2907 · Fixture level: **expanded** (governance mechanism over a frozen oracle + deletion of
  frozen integration tests; no upstream `Suggested fixture level`).
- Risk packs:
  - **Invariant / frozen-oracle**: the oracle stays byte-identical; every guard derives
    `oracle − retirements ∪ additions`; an unledgered change stays RED by name → tasks 1.x, 2.x.
  - **Test-evidence**: rewritten integration tests must execute on node-27, not skip → task 4.2.
  - **Legacy / dead-code removal**: no surviving caller, `get_river_segment` and the #2866 retirement
    assertions unaffected → tasks 3.x, 4.1.
  - **Documentation**: module docstring and inventory Residual paragraph restated → task 3.4.
- Must preserve: `get_river_segment` behaviour and its shared constants/helpers;
  `_delete_legacy_seg_rows`; `core.river_segment_crosswalk` writes; `.large-file-guard.json` unchanged;
  oracle file byte-identical; the #1903 helper-transition proof; QHH-bootstrap partition (#1948) untouched.

## 1. Retirements ledger

- [x] 1.1 Add `tests/fixtures/basins_registry_partition_retirements.json`
  (`basins-registry-partition-retirements/v1`) with the six #2907 records (two `rewrite`, four `retire`),
  `base_commit` = `git merge-base HEAD origin/master`, and per-record `nodes` / `integration_nodes`
  equal to the frozen oracle's nodes of that name.
- [x] 1.2 Add the pure checker `_partition_retirement_violations(oracle, retirements, additions, run_git, *, config)`
  exactly as design D2 (config carries schema id, partition owner set, retained core, database
  authority; no rule reads a `REGISTRY_PARTITION_*` global), every D2 rule a named `ledger.<rule>`
  violation, ordered git provenance (`base_commit_unresolved` → `base_commit_not_ancestor` →
  `owner_absent_at_base` → `name_absent_at_base`).
- [x] 1.3 Selector: route the ledger path to `tests/test_select_ci_tests.py` (positive + red-when-removed
  test, mirroring `test_select_tests_routes_the_registry_partition_additions_ledger_to_the_meta_suite`).

## 2. Guards consume the effective expectation

- [x] 2.1 Implement design D3 in the #1913 guards: frozen-digest comparisons use
  `(observed − added) ∪ retired` (retired names/nodes with their frozen owner/row restored); counts are
  derived (`96 − retired + added`, `94 − retired + added`, owner counts / integration owner map with
  retirements subtracted); `_registry_expected_execution_counts` gains the retirement rule and its
  `..._frozen_literals_plus_additions` test gains a retirements case asserting this PR's values
  (default `77 passed, 15 skipped`; not_integration `77 passed, 1 skipped, 14 deselected`;
  integration_only `14 skipped, 78 deselected` — recompute, report any difference);
  `_registry_owner_of` keeps retired names. Move `_registry_non_empty_ledger_proof` and the other #2183
  constructive proofs onto the effective set with the real retirements ledger (never `[]`).
- [x] 2.2 Tree-side arms in the definitions guard (`retired_still_defined`, `rewrite_row_drift` printing
  the observed row as JSON) plus RED tests: unledgered deletion and unledgered rewrite each name
  `<partition>::<name>`; every D2 rule fires by name on a synthetic ledger; both tree-side arms fire;
  the retirement arms join the `_registry_non_empty_ledger_proof` loop; the real ledger passes every guard.
- [x] 2.3 Rewrite the #1913 header comment in `tests/test_select_ci_tests.py` to document the
  retirements channel step by step (D7); #1903 stays the helper-member transition.

## 3. Store and test disposition

- [x] 3.1 Re-verify each disposition against current code, then: rewrite
  `test_registry_import_creates_idempotent_inactive_rows` to assert the `core.river_segment_crosswalk`
  row count for the fixture RNV with `source='basins_seg_shp'` **== 2** (the same value the import report
  states as `row_counts.river_segment_crosswalk`), and `test_real_basins_import_smoke_is_gated` to assert
  the non-empty-geometry reach count of the imported model is > 0 — markers, decorators and arguments of
  both unchanged; delete the four QHH `test_segment_slice_*` definitions named in the proposal.
- [x] 3.2 Delete `list_river_segments`, its private helpers left without callers, the four
  `RIVER_SEGMENT_COLLECTION_*` constants and re-exports. **Deviation (2026-11-06):** `_stable_segment_id`
  stays — `workers/model_registry/basins_geometry.py` is 1432 lines and not a large-file guard exclusion, so
  any edit is rejected by the pre-commit hook; its deletion moves to #3081 together with the file split and
  `_deduplicate_pending_segment_ids`.
- [x] 3.3 Delete the four river cases in `tests/test_list_search_contract.py` (keep the file and station
  cases), the three store-level list budget tests in `tests/test_model_registration.py`, and the
  `tests/test_api_contract_resources.py` `_RiverSegmentDetailStore.list_river_segments` shadow and its
  `list_calls` assertion (D6).
- [x] 3.4 Docstrings/docs per D7: `tests/test_basins_registry_import_qhh.py` (and any partition) module
  docstring counts; `packages/common/model_registry_river_segments.py` docstring;
  `docs/governance/API_CONTRACT_RETIREMENT_INVENTORY.md` Residual paragraph restated as fact, including
  that `river_segment_id_trgm_idx` (migration 000052, ADR 0004) is now an orphan index (removal out of
  scope) and that `_deduplicate_pending_segment_ids` (`workers/model_registry/basins_geometry.py`) has no
  caller (reported, not removed here).

## 4. Verification (Evidence Floor)

- [x] 4.1 Local: `uv run ruff check .`; `uv run pytest -q tests/test_select_ci_tests.py -k registry_partition`
  green with the 2.2 RED evidence; `uv run pytest -q tests/test_basins_registry_import*.py
  tests/test_list_search_contract.py tests/test_model_registration.py tests/test_api_contract_resources.py`
  ∪ `select_tests(<all PR paths>)` green; `git grep -nE "list_river_segments|RIVER_SEGMENT_COLLECTION_|_stable_segment_id"
  -- apps packages workers services scripts tests` only the `_stable_segment_id` definition (deferred to #3081); `git grep -n "_seg_" -- apps packages
  workers services` only `_delete_legacy_seg_rows` / `basins_seg_shp` / `parse_seg_shp_crosswalk` (plus `_stable_segment_id`, #3081);
  `git diff --exit-code origin/master -- tests/fixtures/basins_registry_partition_oracle.json .large-file-guard.json`;
  every touched test file < 1000 lines; `openspec validate retire-river-segments-store --strict --no-interactive`.
- [ ] 4.2 node-27, on the frozen PR head, isolated temporary database and role derived from `:55432`
  (procedure: `openspec/changes/archive/2026-09-05-partition-basins-registry-import-tests/tasks.md`
  node-27 proof; DB and role dropped afterwards; production DB untouched; no credential in evidence):
  (a) the combined registry auth/DB/QHH + QHH-bootstrap scheduler selection with integration enabled and
  the real-Basins gate off → **25 collected, 24 PASSED, 1 SKIPPED** (only the real-Basins smoke; recompute
  and report any difference); (b) the rewritten real-Basins smoke alone with `NHMS_RUN_REAL_BASINS_IMPORT=1`
  from a disposable worktree of the same SHA whose `data/Basins` is a read-only symlink to
  `/home/ghdc/nwm/Basins`, with pytest started from that worktree's root and its own environment
  (the skipif tests the cwd-relative `data/Basins`) → **1 PASSED**; production checkout `/home/nwm/NWM` untouched. Record both in the PR.
