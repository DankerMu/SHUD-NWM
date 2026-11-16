# Tasks — round3-found-followups (#3164)

## 1. z_policy tests (D1)
- [ ] 1.1 Rewrite the two red tests in `tests/test_mapping_builder_z_policy_verdict.py` to accept the archive
  relocation. Production is unchanged.

## 2. Retire station_set_flip (D2)
- [ ] 2.1 Delete `packages/common/station_set_flip.py`, `tests/test_direct_grid_display_cutover_flip_{atomic,mvt_set}.py`
  and `tests/direct_grid_display_cutover_flip_helpers.py`.
- [ ] 2.2 Remove the selector rules, tables, splat, re-exports and their `tests/test_select_ci_tests.py` pins.
- [ ] 2.3 Remove the `line_reference_baseline.json` and `full_test_durations.json` keys.
- [ ] 2.4 History suite: drop the import, the Claim 4 test, and the Claim 4 docstring text.
- [ ] 2.5 Comment-only refresh in the `model_registry*` files about the unconsumed `station_flag_flip` slot.
- [ ] 2.6 Docs: glossary :39 and inventory :330/:340.
- [ ] 2.7 Re-target the two full-mode pins to display_coverage, change the guarded-sources count from 9 to 8, and fix
  the stale rules comment at :2453-2456.

## 3. Stall reason (D4)
- [ ] 3.1 Add `PassRecord.lock_reason` and the receipt field `lock_contended_reason`.
- [ ] 3.3 Add the reason triage to the `stuck-detection.md` §stall-lock-contended-persistent runbook section.
- [ ] 3.2 Tests in `tests/test_node22_scheduler_stall_health.py`:
  - the reason is carried for a config-refused streak;
  - a null reason is recorded as null;
  - a non-string reason is recorded as null and is not probe-failed;
  - `signals.lock_contended_reason` is null when the newest pass is not lock-contended;
  - the verdict, exit code and anchor are unchanged.

## 4. Lease fd (D5)
- [ ] 4.1 Resolve the mode before opening the fd in `_guarded`, and add the no-leak regression test.

## 5. Umask-independent test (D6)
- [ ] 5.1 Change `tests/test_node22_env_backup_sweep.py::test_script_static_contract` to check the git index mode
  and X_OK.

## 6. Docs (D7)
- [ ] 6.1 `infra/env/README.md` compute.env note.
- [ ] 6.2 Line references in `docs/spec/03_database_design.md`.
- [ ] 6.3 The ADR 0002 :597 module path.
- [ ] 6.4 ROLE_BOUNDARY gains precip and basemap.
- [ ] 6.5 Unresolved spec names in the qhh checklist and the inventory.

## Evidence Floor
- [ ] E1 `uv run ruff check .` clean; `uv run ruff format --check` on the touched .py files.
- [ ] E2 Locally, the `scripts/select_ci_tests.py` selection over the diff, plus these, all green:
  - `tests/test_mapping_builder_z_policy_verdict.py`;
  - `tests/test_select_ci_tests.py`;
  - `tests/test_direct_grid_display_cutover_history.py`;
  - `tests/test_variant_activation_cutover.py`;
  - `tests/test_node22_scheduler_stall_health.py`;
  - the lease suites: `tests/test_node22_scheduler_env_drift_table.py`, `tests/test_production_scheduler.py`, and any
    suite that the selector routes for `services/orchestrator/scheduler_lease.py`;
  - `tests/test_node22_env_backup_sweep.py`;
  - `tests/test_role_boundary_static.py`.
- [ ] E3 The same set passes in a node-27 temporary detached worktree created under `umask 0002`
  (`umask 0002 && git worktree add --detach ...`). In that worktree the pre-fix D6 assertion is recorded red, and the
  D5 test is recorded red against the pre-fix `_guarded`.
- [ ] E4 `openspec validate round3-found-followups --strict --no-interactive`, plus `openspec validate --specs
  --strict` for the touched living specs.
- [ ] E5 Entropy hard gate at 0 failing.
- [ ] E6 `git grep -nE 'station_set_flip|cutover_flip|DIRECT_GRID_DISPLAY_CUTOVER_FLIP|build_station_flag_flip_hook|StationFlagFlipError'`
  returns nothing, or only intentional "retired" mentions. It excludes:
  - `openspec/changes/archive/` (including the archived `rehearse.py`, which now fails to import; archived evidence,
    accepted);
  - `docs/review-loop-log.jsonl`;
  - the dated snapshot `docs/governance/entropy-audit-2026-10-16.snapshot.json`;
  - this change.
- [ ] E7 After merge: master full regression fails only `#3153 test_shared_preflight_rejects_realpath_stub_symlink`.
  The node-22 pull at a pass boundary shows failed_count=0. The node-27 pull plus display restart shows `/health` 200
  and `/api/v1/layers` unchanged.
