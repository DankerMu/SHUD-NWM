## Why

Batch 1 of the 2026-08 basin rollout closure (#1698, #1702, #1703, #1720). The
production rollout itself already happened (#1698 cutover executed 2026-08-22/23,
receipts on the issue); what remains in-repo is the #1720 observation, now with owner
decisions recorded on 2026-09-14:

- **#1720 Part 1** (owner decision: *runbook topology scoping*, not gate revival). With
  `NHMS_SCHEDULER_REQUIRE_DIRECT_GRID=true` the refresh publishes
  `previous_models_snapshot` as prospective, so the #1080/#1433 cutover gate can never
  see a model-set change. `docs/runbooks/current-production-ops.md` §3.1.2 still calls
  the retire declaration the "首选路径" with no topology fence; following it on
  direct-grid refuses the very next refresh with `registry_cutover_declaration_invalid`.
- **#1720 Part 2**. `selected_predecessor` in BLOCK evidence
  (`services/orchestrator/scheduler_generation.py` `_predecessor_identity`) reports
  `valid_time = T - lead_hours` without `cycle_id`, while the matcher key
  (`packages/common/state_manager.py` exact-predecessor lookup) and the WARM_CONTINUE
  evidence use `valid_time = T` plus `cycle_id = cycle_id_for(source, T - lead)`.
  Three conventions share one slot.

**Upstream-premise correction.** #1720 claims Part 2 affects operators only. It does
not: `services/orchestrator/scheduler_backfill_predecessor.py`
`_extract_pending_predecessors` reads `selected_predecessor.valid_time` as the §8.6
predecessor *cycle time*. The issue's recommended one-line fix would make §8.6 synthesize
a candidate at the successor's own cycle. The fix therefore changes producer and consumer
together (recorded as a deviation: the consumer seam was not declared upstream).

## What Changes

- `_predecessor_identity` emits the matcher key: `valid_time = T` (candidate cycle time),
  `cycle_id = cycle_id_for(source_id, T - lead_hours)`, plus existing
  `source_id`/`lead_hours`/`generation`. Both BLOCK call sites (after-effective and
  in-generation) use it; WARM_CONTINUE already matches.
- `_extract_pending_predecessors` derives the predecessor cycle time as
  `valid_time - lead_hours` and refuses (skips, as for other malformed evidence) a record
  whose `cycle_id` is present and disagrees with that derivation.
- Hand-built §8.6 test fixtures move to the unified shape.
- Runbook: fence §3.1.2 retire/replace declaration procedures to non-direct-grid
  topology with the immediate-refusal consequence; fix §5.7.1 dangling "5.x" pointer and
  the "会过期" understatement; name `scripts/provision_direct_grid_scheduler_registry.py`
  as the direct-grid change channel.
- ADR 0005 addendum records the Part 1 decision and rationale.

Out of repo (tracked on the issues, not in this diff): jialingjiang `source_path`
republish on node-22 (owner-authorized), `Basins-retired/*/README.md`, issue Evidence
Floor rewrites, ACL regression (human/root only).

## Impact

- Code: `services/orchestrator/scheduler_generation.py`,
  `services/orchestrator/scheduler_backfill_predecessor.py`, tests.
- Docs: `docs/runbooks/current-production-ops.md`, `docs/adr/0005-recalibration-state-carryover.md`.
- No schema, DB, env, or scheduling-decision change; evidence field values change for
  BLOCK decisions only.
