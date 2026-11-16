# Tasks — round3-found-followups (#3164)

## 1. z_policy tests (D1)
- [x] 1.1 Rewrite the two red tests in `tests/test_mapping_builder_z_policy_verdict.py` to accept the archive
  relocation. Production is unchanged.

## 2. Retire station_set_flip (D2)
- [x] 2.1 Delete `packages/common/station_set_flip.py`, `tests/test_direct_grid_display_cutover_flip_{atomic,mvt_set}.py`
  and `tests/direct_grid_display_cutover_flip_helpers.py`.
- [x] 2.2 Remove the selector rules, tables, splat, re-exports and their `tests/test_select_ci_tests.py` pins.
- [x] 2.3 Remove the `line_reference_baseline.json` and `full_test_durations.json` keys.
- [x] 2.4 History suite: drop the import, the Claim 4 test, and the Claim 4 docstring text.
- [x] 2.5 Comment-only refresh in the `model_registry*` files about the unconsumed `station_flag_flip` slot.
- [x] 2.6 Docs: glossary :39 and inventory :330/:340.
- [x] 2.7 Re-target the two full-mode pins to display_coverage, change the guarded-sources count from 9 to 8, and fix
  the stale rules comment at :2453-2456.

## 3. Stall reason (D4)
- [x] 3.1 Add `PassRecord.lock_reason` and the receipt field `lock_contended_reason`.
- [x] 3.3 Add the reason triage to the `stuck-detection.md` §stall-lock-contended-persistent runbook section.
- [x] 3.2 Tests in `tests/test_node22_scheduler_stall_health.py`:
  - the reason is carried for a config-refused streak;
  - a null reason is recorded as null;
  - a non-string reason is recorded as null and is not probe-failed;
  - `signals.lock_contended_reason` is null when the newest pass is not lock-contended;
  - the verdict, exit code and anchor are unchanged.

## 4. Lease fd (D5)
- [x] 4.1 Resolve the mode before opening the fd in `_guarded`, and add the no-leak regression test.

## 5. Umask-independent test (D6)
- [x] 5.1 Change `tests/test_node22_env_backup_sweep.py::test_script_static_contract` to check the git index mode
  and X_OK.

## 6. Docs (D7)
- [x] 6.1 `infra/env/README.md` compute.env note.
- [x] 6.2 Line references in `docs/spec/03_database_design.md`.
- [x] 6.3 The ADR 0002 :597 module path.
- [x] 6.4 ROLE_BOUNDARY gains precip and basemap.
- [x] 6.5 Unresolved spec names in the qhh checklist and the inventory.

## Evidence Floor
- [x] E1 `uv run ruff check .` clean; `uv run ruff format --check` on the touched .py files. — `ruff check .` passes. `ruff format --check` drift is pre-existing at HEAD; no touched file grows it, and CI does not gate format.
- [x] E2 Locally, the `scripts/select_ci_tests.py` selection over the diff, plus these, all green:
  - `tests/test_mapping_builder_z_policy_verdict.py`;
  - `tests/test_select_ci_tests.py`;
  - `tests/test_direct_grid_display_cutover_history.py`;
  - `tests/test_variant_activation_cutover.py`;
  - `tests/test_node22_scheduler_stall_health.py`;
  - the lease suites: `tests/test_node22_scheduler_env_drift_table.py`, `tests/test_production_scheduler.py`, and any
    suite that the selector routes for `services/orchestrator/scheduler_lease.py`;
  - `tests/test_node22_env_backup_sweep.py`;
  - `tests/test_role_boundary_static.py`. — locally, 174 selected files: 10332 passed, 27 skipped.
- [x] E3 The same set passes in a node-27 temporary detached worktree created under `umask 0002`
  (`umask 0002 && git worktree add --detach ...`). In that worktree the pre-fix D6 assertion is recorded red, and the
  D5 test is recorded red against the pre-fix `_guarded`. — node-27 at a55654c13 under umask 0002: 10353 passed, 5 skipped, 0 leftover `nhms_it_*` DBs. The checkout mode was 775, so the old D6 assertion is red. D5 red-on-old: `assert 13 == 12` (+1 fd leaked) with only the fix reverted. D4 red-on-old: 10 failed.
- [x] E4 `openspec validate round3-found-followups --strict --no-interactive`, plus `openspec validate --specs
  --strict` for the touched living specs. — the change is valid. The touched living specs fail strict only on the pre-existing placeholder `## Purpose` (batch-2 follow-up), which is not introduced here.
- [x] E5 Entropy hard gate at 0 failing. — `hard_gate_status: pass`, 0 failing.
- [x] E6 `git grep -nE 'station_set_flip|cutover_flip|DIRECT_GRID_DISPLAY_CUTOVER_FLIP|build_station_flag_flip_hook|StationFlagFlipError'`
  returns nothing, or only intentional "retired" mentions. It excludes:
  - `openspec/changes/archive/` (including the archived `rehearse.py`, which now fails to import; archived evidence,
    accepted);
  - `docs/review-loop-log.jsonl`;
  - the dated snapshot `docs/governance/entropy-audit-2026-10-16.snapshot.json`;
  - this change. — the remaining hits are intentional: `openspec/specs/{ci-contract-baseline:326,ci-test-selector-routing:12/29/32}`, rewritten by this change's deltas at archive, and `orchestrator-structural-burndown:917`, the batch-2 historical split record.
- [ ] E7 After merge: master full regression fails only `#3153 test_shared_preflight_rejects_realpath_stub_symlink`.
  The node-22 pull at a pass boundary shows failed_count=0. The node-27 pull plus display restart shows `/health` 200
  and `/api/v1/layers` unchanged.
