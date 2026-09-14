## Context

Fixture level: expanded. Repair intensity: high (orchestrator state machine + evidence
contract with a programmatic §8.6 consumer). Project profile: NHMS
(`openspec/project-profile.md`). Upstream suggested level: absent (legacy issues).

## Decisions

- D1 (#1720 Part 1, owner 2026-09-14): runbook topology scoping. No refresh-logic change.
  Gate revival rejected: M-size change to the production refresh path for a procedure
  that already has a working direct-publish channel.
- D2 (#1720 Part 2): the `selected_predecessor` slot has ONE meaning — the state-index
  identity the matcher looks up. `valid_time` is the state valid time (`T`), `cycle_id`
  names the producing cycle (`T - lead_hours`). Consumers derive cycle time from the
  identity; no field carries a mislabeled value. Rejected alternative (rename to
  `predecessor_cycle_time` + extra key): keeps a second convention alive.
- D3: `_extract_pending_predecessors` runs on the current pass's in-memory blocked list;
  no persisted-evidence reader consumes the field programmatically, so no old-shape
  runtime compatibility is required. Old evidence JSON remains human-readable only.
- D4: consistency guard — the consumer computes the expected id with
  `cycle_id_for(source_id, valid_time - lead_hours)` (the same helper producer and gate
  use; it lowercases, e.g. `era5_…`, `ifs_…`). A present `cycle_id` that disagrees, or a
  source id `cycle_id_for` rejects, is malformed evidence and is skipped (existing
  malformed-record behavior), never "repaired" by preferring one field. A missing
  `cycle_id` is accepted and derived.
- D5: the emitted §8.6 candidate keeps its own `cycle_id` builder
  `_predecessor_cycle_id` (case-preserving) — unchanged behavior; the helper-casing
  divergence is pre-existing and out of scope.
- D6: `_predecessor_identity` calling `cycle_id_for` adds a `ValueError` path for unknown
  source ids; unreachable in production because the gate already calls
  `cycle_id_for(candidate.source_id, …)` (`scheduler_generation_gate.py` ~:370) before
  `evaluate_transition_decision`. Recorded, not guarded.

## Change surface

- `services/orchestrator/scheduler_generation.py`: `_predecessor_identity` (~:918),
  call sites (~:1225, ~:1292); WARM_CONTINUE (~:1273) unchanged.
- `services/orchestrator/scheduler_backfill_predecessor.py`: `_extract_pending_predecessors` (~:69-128).
- Tests: `tests/test_scheduler_generation.py`, `tests/test_scheduler_backfill_predecessor.py`,
  `tests/test_production_scheduler.py` (§8.6 fixture ~:53915).
- Docs: runbook §3.1.2 (~:932-975), §5.7.1 (~:2990-3040); ADR 0005.

## Must preserve

- Every TransitionDecision value, typed reason, and admission outcome is unchanged.
- §8.6 emits exactly the same predecessor candidate (cycle `T - lead_hours`, same
  `_predecessor_cycle_id` value, same model/source, same dedup key) for the same blocked
  successor; lineage `t*` scope-out (#1735) record `cycle_time` and emission evidence
  `predecessor_cycle_time` values are identical to pre-change output.
- Gate block evidence keeps `required_prior_cycle_time` (= `T - lead`) and `required_prior_cycle_id`.
- WARM_CONTINUE `selected_predecessor` shape and values.
- Non-direct-grid retire/replace declaration procedure text stays valid for that topology.

## Seams under test

- `evaluate_transition_decision` (pure, public in module) — BLOCK evidence identity.
- `emit_predecessor_candidates` / `_extract_pending_predecessors` — §8.6 consumer.
- End-to-end: `tests/test_production_scheduler.py` §8.6 warm-start toggle path through the real gate.
- Deviation: consumer seam was not declared by #1720 (issue premise falsified).

## Risk packs (core)

- Public API / CLI / script entry: not selected — no CLI/API change.
- Config / project setup: not selected — no env/config change.
- File IO / path safety / overwrite: not selected — no file IO touched.
- Schema / columns / units / field names: selected — evidence field value semantics + new `cycle_id` key.
- Auth / permissions / secrets: not selected — none.
- Concurrency / shared state / ordering: not selected — ordering/prepend code untouched; emission equality (1.5) covers the only input that changes.
- Resource limits / large input / discovery: not selected — `MAX_PREDECESSOR_EMISSIONS` untouched.
- Legacy compatibility / examples: selected — hand-built fixtures and old evidence readers.
- Error handling / rollback / partial outputs: selected — malformed/inconsistent evidence skip path.
- Release / packaging / dependency compatibility: not selected — none.
- Documentation / migration notes: selected — runbook topology fence + ADR.
- Domain: Hydro-met time series / forcing windows: selected — lead-hour arithmetic on cycle grid.
- Domain: Slurm production lifecycle: not selected — no submission/runtime change.

## Invariant Matrix

- Governing invariant: for the same candidate, `selected_predecessor`
  (`source_id`, `valid_time`, `cycle_id`, `lead_hours`) equals the inputs of the matcher's
  `_state_index_identity_key` exact-predecessor lookup, and every consumer derives the
  predecessor cycle from that identity.
- Source of truth: `packages/common/state_manager.py` exact-predecessor key
  (`valid_time=cutoff`, `cycle_id=expected_predecessor_cycle_id`, `lead_hours`); gate
  computes `expected_predecessor_cycle_id` in `scheduler_generation_gate.py` (~:370).
- Producers: `scheduler_generation.py` `_predecessor_identity` + 2 BLOCK call sites; WARM_CONTINUE literal (~:1273).
- Validators/preflight: `state_manager.py` `generation_scoped_history_signal` (unchanged).
- Storage/cache/query: state index entries (unchanged, read-only).
- Public routes/entrypoints: none — scheduler-internal.
- Frontend/downstream consumers: `scheduler_backfill_predecessor.py` §8.6; `scheduler_generation_gate.py` ~:675 evidence pass-through.
- Failure paths/rollback/stale state: malformed/inconsistent `selected_predecessor` (incl. `cycle_id_for` rejection) → skip, no emission; producer unknown-source `ValueError` unreachable (D6).
- Evidence/audit/readiness: pass evidence `registry_cutover_transition.selected_predecessor`.
- Regression rows:
  - block_predecessor_pending (after-effective) at T, lead 12 → `valid_time=T`, `cycle_id=<src>_<T-12h>` equal to matcher key.
  - block_wrong_generation (in-generation) at T → same equality.
  - block_predecessor_pending blocked successor → §8.6 emits candidate at `T-12h` with same cycle_id as before.
  - evidence with `cycle_id` disagreeing with `valid_time - lead_hours` → no emission (skipped).
  - evidence missing `cycle_id` but otherwise valid → emission derived from `valid_time - lead_hours`.
  - ERA5 block record T=2026-07-06T12Z, lead 12, `cycle_id=era5_2026070600` → emitted (not skipped), candidate cycle 2026-07-06T00Z, candidate `cycle_id` = `_predecessor_cycle_id` output (unchanged).
  - gate block evidence (~:675) → `selected_predecessor.cycle_id == required_prior_cycle_id` and `selected_predecessor.valid_time == required_prior_cycle_time + lead_hours`.
  - pre-`t*` lineage scope-out (#1735): t*=2026-07-06T06Z, successor T=2026-07-06T12Z, lead 12 → `lineage_scoped_out` with `cycle_time` 2026-07-06T00Z; emission evidence `predecessor_cycle_time` identical before/after.
  - unknown source id in `_predecessor_identity` → unreachable (D6).
  - WARM_CONTINUE → unchanged shape/values.

## Boundary-surface checklist

- Shared helper roots: `cycle_id_for` (reuse, unchanged).
- Read surfaces: `_extract_pending_predecessors`.
- Producer/consumer evidence boundaries: generation → gate → candidate `state_evidence` → §8.6.
- Unchanged downstream consumers: pass evidence writers/readers, operator runbooks citing the field.
