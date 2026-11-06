## Context

- The #1913 Basins registry-import partition is frozen by a tracked oracle
  (`tests/fixtures/basins_registry_partition_oracle.json`, schema `basins-registry-partition-oracle/v1`)
  that nothing can regenerate (its generator and contract under `.workplans/issue-1913/` are gone).
  `tests/test_select_ci_tests.py` (#1913 section, around `REGISTRY_PARTITION_*`) reads EXPECTED values
  only from the oracle, the #2183 additions ledger and frozen literals, and OBSERVED values only from
  the tree.
- The additions ledger (`basins-registry-partition-additions/v1`, currently `additions: []`) is the
  precedent: a pure checker `_registry_ledger_violations(oracle, ledger, run_git)` reports named
  `ledger.<rule>` violations; guards evaluate `oracle ∪ additions`.
- Frozen literals that a removal touches (issue #2907 lists them; values are recomputed, not copied):
  `REGISTRY_PARTITION_FROZEN_DIGESTS`, the definition count `94`, the
  `_registry_expected_execution_counts` bases `78/18/17/79` and their expected strings, owner
  function/node counts (QHH 22), integration suffixes/owner map (17), node suffixes (96).
- Current store callers (master `4511d3f48`): five frozen integration tests, four
  `tests/test_list_search_contract.py` river cases, three `tests/test_model_registration.py` budget
  tests, and the `tests/test_api_contract_resources.py:516` stub. No `apps/` caller.

## Goals / Non-Goals

**Goals**
- One audited, reviewable channel to retire or rewrite a frozen partition definition, keeping the
  oracle byte-identical and every guard RED for an unledgered change.
- Delete the caller-less store list path, its constants (`_stable_segment_id` deferred to #3081) in the same PR.

**Non-Goals**
- No change to `get_river_segment`, `SELECTED_SEGMENT_GEOMETRY_MAX_*`,
  `RIVER_SEGMENT_DETAIL_MAX_SERIALIZED_BYTES`, `_escape_like`,
  `_enforce_river_segment_serialized_budget`, `_delete_legacy_seg_rows`, `core.river_segment_crosswalk`,
  any HTTP contract, schema, migration or production data.
- Not wiring the ledger into the QHH-bootstrap partition (#1948).
- Not regenerating or editing the oracle file.

## Decisions

### D1. Retirements ledger shape

`tests/fixtures/basins_registry_partition_retirements.json`:

```json
{"schema": "basins-registry-partition-retirements/v1", "retirements": [
  {"issue": 2907, "base_commit": "<40-hex merge-base>", "owner": "tests/test_basins_registry_import_qhh.py",
   "name": "test_segment_slice_count_matches_sp_rivseg", "action": "retire",
   "reason": "<one line>", "row": null, "nodes": ["<frozen suffix>", ...], "integration_nodes": [...]}
]}
```

- `action: retire` → the definition and its nodes are removed from every expectation. `row` is `null`.
- `action: rewrite` → the definition stays, keeps exactly its frozen node suffixes and markers, and its
  expected row (source/AST digests) becomes `row` (7 strings, `row[0] == owner`).
- `nodes` / `integration_nodes` MUST equal the oracle's frozen nodes / integration nodes of `name`
  exactly (no partial retirement, no node renaming through a rewrite).

### D2. Checker rules (`ledger.<rule>`), pure over inputs, partition-parameterised

`_partition_retirement_violations(oracle, retirements, additions, run_git, *, config)` where `config`
carries the schema id, the partition owner set, the retained-core path and the database authority, so
the #1948 QHH-bootstrap partition can call it with its own config later (it is not wired there in this
change; no rule reads a `REGISTRY_PARTITION_*` global directly). Rules, each a named violation:

- `schema`, `missing_key`, `field_shape` (int issue, 40-hex base_commit, action ∈ {retire, rewrite},
  non-empty reason string);
- `name_not_frozen` (name must be an oracle row), `owner_mismatch` (owner == frozen row[0]),
  `owner_not_allowed` (the retained core `tests/test_basins_registry_import.py` is excluded, exactly as
  for additions — its BUG-008 literal command and `bug008.suffixes` stay frozen), `duplicate_name`,
  `overlaps_addition` (name also present in the additions ledger);
- `nodes_mismatch` / `integration_nodes_mismatch` (must equal the oracle's frozen nodes / integration
  nodes of `name` exactly), `retire_non_integration_skip` (the single frozen non-integration skip node is
  outside the retirement domain);
- `retire_has_row` (retire with a non-null row); for rewrite: `row_shape` (7 strings),
  `row_frozen_field_changed` (fields [0] owner, [3] markers, [4] args digest, [5] decorators digest MUST
  equal the frozen row; only [1] source_sha256, [2] ast_sha256 and [6] monkeypatch digest may differ),
  `row_unchanged` (identical to the frozen row is not a rewrite);
- ordered git provenance, first failure only: `base_commit_unresolved` → `base_commit_not_ancestor` →
  `owner_absent_at_base` → `name_absent_at_base` (the inverse of additions: the owner blob at
  `base_commit` MUST define `name`).

The checker never reads the tree. Tree-side consistency is the **definitions guard's** job (D3/D4).

### D3. Effective expectation, and how frozen digests stay comparable

The oracle and its literals stay oracle facts. Guards compare in two layers:

1. **Frozen-digest layer (anti-laundering).** Wherever a guard today computes
   `digest(observed − added)` and compares it with `oracle.digests.{suffix, integration_suffix,
   owner_map, definition, ...}`, it now computes `digest((observed − added) ∪ retired)`: the ledgered
   retired names (with their frozen owner/row) and nodes are put back, reconstructing the full frozen
   set, and ledgered rewrites are compared on their frozen row for the digest and on their ledger row
   for tree identity. Because `nodes_mismatch` / `name_not_frozen` force retired entries to be exactly
   frozen entries, the reconstructed digest equals the oracle digest only when the tree lacks exactly
   the ledgered entries.
2. **Effective-count layer.** Hard-coded counts become derived: suffix total `96 − retired_nodes +
   added_nodes`, definition total `94 − retired_names + added_names`, owner function/node counts and the
   integration owner map with retired entries subtracted, execution counts by the rule below.
   `_registry_owner_of` keeps retired names so a *missing unledgered* definition is still named
   `<partition>::<name>`.

Execution-count rule (extends `_registry_expected_execution_counts`): a retired non-integration node is
subtracted from `passed` (default, not_integration) and from `deselected` (integration_only); a retired
integration node is subtracted from `skipped` (default, integration_only) and `deselected`
(not_integration). Expected for this PR (recompute and assert, do not copy): default `77 passed, 15
skipped`; not_integration `77 passed, 1 skipped, 14 deselected`; integration_only `14 skipped, 78
deselected`.

Existing #2183 constructive proofs (`_registry_non_empty_ledger_proof` and its siblings, which use
`oracle["node_suffixes"]` / `oracle["integration_suffixes"]` as the observed baseline and read the real
DB/QHH partition text) are moved onto the effective set and receive the real retirements ledger — never
an empty one — so they keep proving additions on top of the post-#2907 tree.

### D4. Tree-side checks and RED arms

The definitions guard (tree-reading) reports, by `<partition>::<name>`: an unledgered missing frozen
definition (existing arm unchanged); an unledgered changed frozen definition (existing drift arm);
`retired_still_defined` (a `retire` record whose name the tree still defines); `rewrite_row_drift`
(tree row ≠ ledger row; the message prints the observed row as JSON so it can be pasted into the
ledger). New RED tests: each D2 rule on a synthetic ledger; both tree-side arms; the real ledger passes
every guard; the retirement arms are added to the `_registry_non_empty_ledger_proof` loop.

### D5. Selector routing

`tests/fixtures/basins_registry_partition_retirements.json` routes to `tests/test_select_ci_tests.py`
exactly like the additions ledger (positive test + red-when-removed).

### D6. Store deletion and stub

Remove the list method, its private helpers left without callers, the four constants and re-exports,
(`_stable_segment_id` deferred to #3081 — host file must be split first); remove the non-frozen river tests listed in the proposal. **Delete** the
`tests/test_api_contract_resources.py` `_RiverSegmentDetailStore.list_river_segments` shadow method and
its `list_calls` assertion: with no production `list_river_segments`, the fake's counter proves
nothing; the existing 404 assertion carries "the list is retired".

### D7. Comments and docstrings that would go stale

- #1913 header comment in `tests/test_select_ci_tests.py` ("Amending an EXISTING frozen definition …
  keeps the #1903 transition shape"): rewritten to describe the retirements channel (step list mirroring
  the additions steps; #1903 remains the helper-member transition).
- `tests/test_basins_registry_import_qhh.py` module docstring (22 cases / seven integration nodes) and
  any other partition docstring counts: updated to the post-retirement facts (QHH 18 / 4). Module headers
  are not frozen rows.
- `packages/common/model_registry_river_segments.py` docstring and the inventory Residual paragraph:
  restated as fact. ADR 0004 is historical; record in the inventory Residual (not the ADR) that the
  `river_segment_id_trgm_idx` index (migration 000052) lost its only consumer (the list search) and is
  now an orphan — index removal is a separate DB change, out of scope.

## Risks / Trade-offs

- **Laundering**: a ledger could hide a regression. Mitigations: name must be frozen, nodes must equal the
  frozen nodes, rewrite must keep markers/nodes and be reviewed, git provenance checks; still a weak
  provenance check (author picks `base_commit`), same as additions.
- **Integration coverage loss**: three QHH integration cases leave. The 18-row crosswalk count stays
  covered by `test_pr2_contract_reach_rows_single_part_and_crosswalk_count`; the rewritten DB tests must
  actually execute on node-27 (not skip).
- **Large file**: `tests/test_select_ci_tests.py` is already guard-registered; no new exclusion.
  `tests/test_basins_registry_import_qhh.py` / `_db.py` must stay < 1000 lines (they shrink).

## Migration Plan

Single PR. Verification: local guards + two node-27 runs on an isolated temporary database derived from
`:55432` (procedure: `openspec/changes/archive/2026-09-05-partition-basins-registry-import-tests/tasks.md`
node-27 proof): (1) the combined registry auth/DB/QHH + QHH-bootstrap scheduler selection with integration
enabled and the real-Basins gate off; (2) the rewritten real-Basins smoke alone with
`NHMS_RUN_REAL_BASINS_IMPORT=1`, run from a disposable worktree whose `data/Basins` is a read-only symlink
to `/home/ghdc/nwm/Basins` (production checkout untouched). Both recorded in the PR.
