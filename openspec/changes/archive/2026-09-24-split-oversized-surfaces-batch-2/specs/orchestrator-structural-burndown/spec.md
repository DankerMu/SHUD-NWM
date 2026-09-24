## ADDED Requirements

### Requirement: Guard exclusion burndown batch 2 retires three exclusions and unfreezes two model-registry owners

The repository SHALL split `tests/test_node22_refresh_timer_health.py`,
`tests/test_direct_grid_display_cutover_flip.py`,
`workers/model_registry/basins_discovery.py`,
`workers/model_registry/qhh_production_bootstrap.py` and
`workers/model_registry/basins_registry_import.py` so that every resulting file is
below 1,000 lines, SHALL reduce `workers/model_registry/basins_package_source_io.py`
to at most 900 lines, and SHALL remove exactly the three corresponding exclusions
from `.large-file-guard.json` in the same commit as each split. No exclusion SHALL be
added, and `maxLines`, `enabled` and every other entry SHALL remain byte-identical.
Oversized non-excluded consumers of these modules SHALL remain unmodified.

#### Scenario: the guard ledger only shrinks

- **WHEN** `.large-file-guard.json` is compared against its pre-change content
- **THEN** exactly the three named entries are absent and no entry was added
- **AND** every file the splits produce is below 1,000 lines.

#### Scenario: a split would force an edit to a frozen consumer

- **WHEN** a candidate cut would require editing an oversized non-excluded consumer
- **THEN** that cut is rejected in favour of one whose facade keeps the consumer's
  imports and patch seams valid, instead of adding an exclusion.

### Requirement: Model-registry facade splits keep patch seams, write surfaces and lock order

When a model-registry module is split behind a facade, every caller on the call path
that an existing monkeypatch exercises SHALL stay in the patched module and resolve the
patched name through its module globals, and each such seam SHALL be proven live by a
mutation that breaks the real call site. `_backfill_output_segment_geometry`, `_lock_river_network_version`
and `_seed_output_segment_rows` SHALL stay in their current modules, the
river-segment write-surface scan constants SHALL be unchanged, and ADR 0009 member
disposition markers SHALL stay inside their member function bodies.

#### Scenario: a spy still intercepts the real import path

- **WHEN** `tests/basins_registry_import_helpers.py` spies one of the eleven patched
  `basins_registry_import` names and the import orchestration runs
- **THEN** the spy records the call, and deliberately breaking the real call site
  turns a committed spy-count assertion red or, for a spy no committed assertion
  discriminates, a split-time seam probe red.

#### Scenario: the write-surface scan still finds its single writers

- **WHEN** `tests/test_river_segment_write_surface_scan.py` runs after the split
- **THEN** the only `UPDATE core.river_segment` and `geometry_generation` write
  remain in `basins_registry_import.py` and the only upsert in
  `qhh_production_bootstrap.py`, with the scan constants unchanged.

### Requirement: Refresh-timer and cutover-flip corpora partition without drift

The repository SHALL partition the node-22 refresh-timer health suite and the
direct-grid display cutover-flip suite into collectible modules below 1,000 lines with
shared definitions in non-collectible helpers, SHALL route every partition through an
explicit `PathTestRule`, and SHALL guard each corpus's exact tracked partition set.

#### Scenario: collection identity survives partitioning

- **WHEN** either corpus is collected after partitioning
- **THEN** the sorted `::test_name[param-id]` suffix set equals the pre-split
  baseline exactly, with no additions, losses, duplicates or skips.

#### Scenario: a partition disappears from the tracked tree

- **WHEN** one partition file is removed or a compatibility shim is added
- **THEN** the tracked-tree guard in `tests/test_select_ci_tests.py` fails.
