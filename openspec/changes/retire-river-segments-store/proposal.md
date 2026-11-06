## Why

#2866 / PR #2906 retired the `GET .../river-segments` list endpoint but had to keep the store read path:
`PsycopgModelRegistryStore.list_river_segments`, its four private helpers, the four
`RIVER_SEGMENT_COLLECTION_*` constants and their re-exports, plus the `_seg_` id producer
`workers/model_registry/basins_geometry.py::_stable_segment_id`. Five integration tests that still call
the store live in the #1913 frozen Basins registry-import partition, and that partition's oracle
(`tests/fixtures/basins_registry_partition_oracle.json`, guarded in `tests/test_select_ci_tests.py`)
has a channel for adding tests (#2183 additions ledger) but none for retiring or rewriting a frozen
definition. The result is about 765 lines of dead code protected only by a "Do not add callers"
comment, a live producer of the `_seg_` id that #2866 set out to eliminate, and seven non-frozen
store tests paying CI time for behaviour nothing calls.

Owner decision (2026-11-06, #2907 comment): add a **retirements ledger**. The oracle stays
byte-identical; retiring or rewriting a frozen definition is recorded once, reviewed with the PR, and
the guards evaluate `oracle − retirements ∪ additions`.

## What Changes

- New tracked ledger `tests/fixtures/basins_registry_partition_retirements.json`
  (`basins-registry-partition-retirements/v1`). Every record retires or rewrites exactly one frozen
  definition and carries `issue`, `base_commit`, `owner`, `name`, `action` (`retire` | `rewrite`),
  `reason`, `row` (the new 7-field row for `rewrite`, `null` for `retire`), `nodes` (the frozen node
  suffixes the record removes or keeps) and `integration_nodes`.
- The #1913 guards in `tests/test_select_ci_tests.py` derive every expectation (rows, node suffixes,
  owner function/node counts, integration suffixes and owner map, execution counts, the definition
  and digest checks, the database-authority node proof) from the frozen oracle with the retirements
  ledger applied, then united with the additions ledger. The ledger checker validates every record by
  name with `ledger.<rule>` violations, mirroring the additions checker. An unledgered deletion or
  rewrite of a frozen definition stays RED and names `<partition>::<name>`.
- Frozen-test disposition (re-verified by the implementer):
  - **rewrite** `tests/test_basins_registry_import_db.py::test_registry_import_creates_idempotent_inactive_rows`
    → SQL assertion on `core.river_segment_crosswalk` row counts per RNV with `source='basins_seg_shp'`.
  - **rewrite** `tests/test_basins_registry_import_db.py::test_real_basins_import_smoke_is_gated`
    → assert the non-empty-geometry reach row count is > 0.
  - **retire** `tests/test_basins_registry_import_qhh.py::{test_segment_slice_count_matches_sp_rivseg,
    test_segment_slice_last_endpoint_saturates_to_reach_terminus,
    test_segment_slice_river_segment_id_preserves_frontend_contract,
    test_segment_slice_length_m_none_uses_equal_partition}`.
- Delete `list_river_segments`, `_list_river_segment_rnv_ids`, `_has_segment_crosswalk_for_rnv`,
  `_list_river_segments_segment_slice`, `_reach_row_to_legacy_feature` (and any helper left without a
  caller), the four `RIVER_SEGMENT_COLLECTION_*` constants and their `model_registry.py` re-exports,
  (`_stable_segment_id` was in scope but is deferred to #3081: its 1432-line host file is not a
  large-file guard exclusion and must be split first).
- Delete the four river cases in `tests/test_list_search_contract.py` (file stays; station cases
  remain) and the three store-level list budget tests in `tests/test_model_registration.py`, and the
  `tests/test_api_contract_resources.py` `_RiverSegmentDetailStore.list_river_segments` shadow with its
  `list_calls` assertion (the 404 assertion keeps carrying "the list is retired").
- Docs: `packages/common/model_registry_river_segments.py` module docstring and the Residual paragraph
  of `docs/governance/API_CONTRACT_RETIREMENT_INVENTORY.md` restated as fact; ADR 0004 mention checked.

## Capabilities

### Modified Capabilities

- `orchestrator-structural-burndown`: the Basins registry-import partition requirement gains the
  retirements ledger as its single, audited channel for removing or rewriting a frozen definition.

## Impact

- Code: `packages/common/{model_registry_river_segments,model_registry_contracts,model_registry}.py`,
  `workers/model_registry/basins_geometry.py`.
- Tests: `tests/test_basins_registry_import_{db,qhh}.py`, `tests/test_list_search_contract.py`,
  `tests/test_model_registration.py`, `tests/test_api_contract_resources.py`,
  `tests/test_select_ci_tests.py` (#1913 guard section), new ledger fixture, selector routing.
- No HTTP contract, DB schema, migration or production data change. `get_river_segment` (detail) and
  everything it shares stay. `core.river_segment_crosswalk` stays (owner decision pending, out of scope).
- Reported, not fixed here: `river_segment_id_trgm_idx` (migration 000052, ADR 0004) loses its only
  consumer and becomes an orphan index; `_deduplicate_pending_segment_ids` in
  `workers/model_registry/basins_geometry.py` has no caller. Both are recorded in the inventory Residual.
- Out of scope: the sibling QHH-bootstrap partition (#1948) — the checker is written so it can adopt
  the same ledger later, but it is not wired here; `_delete_legacy_seg_rows` (live import cleanup);
  `test_segment_slice_geometry_is_subset_of_reach` (does not call the store).
