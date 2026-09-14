Fixture level: expanded; repair intensity: high. Risk-pack selections and Invariant Matrix in `design.md`.

Minimal mergeable slice: atomic for Part 2 — producer and §8.6 consumer must change together or
§8.6 emits a self-referential candidate; docs ride along (no runtime coupling).

## 1. #1720 Part 2 — predecessor identity

- [x] 1.1 `_predecessor_identity` emits `valid_time = T` and `cycle_id = cycle_id_for(source_id, T - lead_hours)`; both BLOCK call sites updated.
- [x] 1.2 `_extract_pending_predecessors` derives cycle time as `valid_time - lead_hours`; guard compares a present `cycle_id` against `cycle_id_for(source_id, valid_time - lead_hours)`; mismatch or helper rejection → skip; missing → derived. Emitted candidate `cycle_id` stays `_predecessor_cycle_id` (D5).
- [x] 1.3 Test (block_predecessor_pending): `selected_predecessor` fields equal the matcher key inputs
      (`valid_time=T`, `cycle_id=<src>_<T-lead>`, `lead_hours`) — input T=2026-07-06T12Z, lead 12 → `valid_time` 2026-07-06T12:00Z, `cycle_id` gfs_2026070600.
- [x] 1.4 Test (block_wrong_generation): same equality on the in-generation branch.
- [x] 1.5 Test (§8.6 consumer): blocked successor at T with unified evidence → emitted predecessor candidate cycle `T-12h`, `cycle_id` unchanged from pre-change behavior.
- [x] 1.6 Test (§8.6 malformed): `cycle_id` disagreeing with `valid_time - lead_hours` → no emission; missing `cycle_id` → derived emission.
- [x] 1.6a Test (ERA5 casing): T=2026-07-06T12Z, lead 12, `cycle_id=era5_2026070600` → emitted, candidate cycle 2026-07-06T00Z, candidate `cycle_id` equals `_predecessor_cycle_id("ERA5"|given source, 00Z)` as before.
- [x] 1.6b Test (gate evidence, `block_wrong_generation` at gate ~:675; pending case via nested `registry_cutover_transition.selected_predecessor` vs self-heal `required_prior_*` ~:795): block evidence from the gate has `selected_predecessor.cycle_id == required_prior_cycle_id` and `selected_predecessor.valid_time == required_prior_cycle_time + lead_hours`.
- [x] 1.7 #1735 lineage scope-out: t*=2026-07-06T06Z, successor T=2026-07-06T12Z, lead 12 → `lineage_scoped_out` record `cycle_time` 2026-07-06T00Z; emission evidence `predecessor_cycle_time` identical to pre-change; §8.6 end-to-end fixture (`tests/test_production_scheduler.py` ~:53915) moved to the unified shape and green.
- [x] 1.8 Red proof: 1.3/1.4/1.5 red against pre-change source (batched), recorded in implementer report.

## 2. #1720 Part 1 — topology scoping (docs)

- [x] 2.1 Runbook §3.1.2: topology fence before the replace/retire declaration procedures — applies only when
      `NHMS_SCHEDULER_REQUIRE_DIRECT_GRID` is not true; on direct-grid a declaration matches no removal and the NEXT
      refresh fails `registry_cutover_declaration_invalid`; point to §5.7.1 and §7.2.
- [x] 2.2 Runbook §5.7.1: replace "5.x" with the real section reference; correct "会过期" to immediate refusal on next
      refresh; name `scripts/provision_direct_grid_scheduler_registry.py` as the provision channel.
- [x] 2.3 ADR 0005 addendum: #1720 Part 1 decision (topology scoping), rejected gate revival, consequence that
      `NHMS_REGISTRY_CUTOVER_DECLARATION_PATH` must stay unset on direct-grid production.

## 3. Verification

- [x] 3.1 `uv run ruff check .`
- [x] 3.2 `uv run pytest -q tests/test_scheduler_generation.py tests/test_scheduler_backfill_predecessor.py`
- [x] 3.3 `uv run pytest -q tests/ -k "generation or state_index or predecessor"`
- [ ] 3.4 node-27 exact-head: `uv run pytest -q tests/test_production_scheduler.py tests/test_scheduler_generation.py tests/test_scheduler_backfill_predecessor.py`
- [x] 3.5 `openspec validate align-predecessor-identity-direct-grid-topology --strict --no-interactive`

## 4. Live ops (outside diff, receipts on issues)

- [ ] 4.1 jialingjiang registry rows `resource_profile.source_path` moved off scratch staging (owner-authorized republish), receipt on #1698.
- [ ] 4.2 `Basins-retired/forcing-cleanup-20260825/README.md` and `Basins-retired/issue-1701-20260825/README.md` created (source issue + 90-day deletable date), receipt on #1702.
- [ ] 4.3 Follow-ups filed: ACL regression (862 non-writable), Huai-MAIN #1816 hop-3 repackage receipt gap (`dg_5bd9935f…`/`dg_67210bfe…`) — filed #2364 (ACL) and #2365 (Huai-MAIN).

## Non-goals

- Refresh cutover gate revival on direct-grid (rejected D1).
- root `setfacl`, stakeholder notification (human-only; #1702).
- Changing `_state_index_identity_key` or matcher semantics.
