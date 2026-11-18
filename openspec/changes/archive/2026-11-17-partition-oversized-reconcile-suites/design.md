## Context

Fixture level: expanded. This is a pure test move of six oversized suites, following the #1809 gateway-reconcile
partition method (`openspec/changes/archive/2026-08-24-split-gateway-reconcile-tests/design.md`) and the #3165
base-path-keeps-the-first-lane precedent.

## Governing invariant

The change moves only module paths. Every pre-change case in the six suites is collected exactly once afterwards and
runs unchanged. Every definition lives in exactly one module. CI selection reaches the same cases from the same
changed paths. The only exceptions are the differences E3 lists, each with the route that compensates for it.

## Decisions

1. **The base path keeps the first lane.** Existing references to the six paths keep resolving, and the node-id
   references in the selector tables stay valid without a rewrite.
2. **Helpers are owned per suite.** A helper used by one partition stays in it. A helper used by two or more moves to
   that suite's non-collectible support module. Same-named helpers across suites (`_fallback_row`, `_fallback_querier`)
   are not merged, because their bodies differ. No partition imports another partition.
3. **Commits.** One pure-move commit per suite, then one selector + durations commit, so E2 is provable on each move
   commit in isolation.
4. **Selector.**
   - Paths are expanded in place.
   - Runtime-budget dispositions are re-derived per partition from that partition's own file-level imports.
   - Each support module gets a route plus an anchor.
   - `GATEWAY_RECONCILE_PARTITIONS` membership policy is unchanged: suites outside it stay outside.
5. **Durations.** Each base weight is split by measured per-file seconds, or by case count if no measurement is
   available, so the master shard balance does not shift.

## Sibling surfaces

- `tests/orchestrator_bind_reserved_job_helpers.py` (non-collectible) imports the claimant helpers. Its collectible
  consumers are reached through its own route.
- `tests/test_gateway_reconcile_submitline_exact.py`, `tests/test_orchestrator_bind_reserved_job_cas.py` and
  `tests/test_orchestrator_bind_reserved_job_lane.py` are the collectible cross-file importers.
- Guards that read the tracked tree: `test_guarded_module_rules_cover_their_non_gated_importer_closure`,
  `test_directory_rule_importer_gaps_are_dispositioned`, and the `SUPPORT_MODULE_ROUTING_ANCHORS` equality at
  `tests/test_select_ci_tests.py:14159`. All of them need the new files tracked before they run.
- `scripts/ci/shard_tests.py` and `tests/test_ci_shard_tests.py:172-178` (sorted keys).

## Hazards

- A helper duplicated into two partitions passes a set-based check. E2 uses a multiset.
- A partition that loses a file-level import leaves a stale runtime-budget disposition. `_comment_fallback_bind` is the
  known case.
- The importer-closure derivation reads only tracked files. The #3176 false green came from an untracked file.

## Review focus

- E2 suffix and fingerprint identity per move commit.
- The E3 diff list and its allowed differences (a)-(d).
- The per-partition dispositions, routes and anchors.
- The `BLE001` placement.
