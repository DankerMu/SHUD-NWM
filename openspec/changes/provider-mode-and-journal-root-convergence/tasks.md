# Tasks — provider-mode / journal-root batch (#2403 #1632 #1631 #2384)

## Risk Packs

- [x] Error handling / partial outputs — **selected**: `~nosuchuser` roots become typed blockers at retention and restore. Covered by 4.x.
- [x] Legacy compatibility — **selected**: the `journal_root_*` blocker vocabulary is a receipt contract; parity pinned. Covered by 4.2.
- [x] File IO / path safety / permissions — **selected**: provider gates untouched (comment-only diff); #2403 test uses the #1513 helpers; #1632 receipt under umask 0002. Covered by 1.x, 2.x, 3.x.
- [x] Documentation / migration notes — **selected**: #1631 and #2384 rulings (design D3/D4), receipts under `receipts/`. Covered by 3.1, 5.x.
- [ ] Public API, Schema, Concurrency, Auth, Resource limits, Release — not selected.

## 0. Setup

- [x] 0.1 Branch `feat/issue-2403-1632-1631-2384-provider-mode-journal-root` from `origin/master` @`de4d1d182`.

## 1. #2403 — backfill test through provider_mode_helpers

- [x] 1.1 (orchestrator) RED on node-27 at master `de4d1d182`, `umask 002`: `tests/test_scheduler_backfill.py -k unreadable_index_is_not_memoized` fails `provider_lock_parent_unsafe`; `umask 022` passes.
  - `umask=0002`: `1 failed` (`StateManagerError: provider_lock_parent_unsafe` → `state_snapshot_index_write_failed`); `umask=0022`: `1 passed` (receipt `receipts/2026-09-23-issue-1632-umask0002-markers.md`, last section).
- [x] 1.2 (implementer) Design D1 edit; import `make_directory_with_explicit_mode`, `write_provider_destination` from `tests.provider_mode_helpers`. Local: the test passes under `umask 002` and `umask 022` (`(umask 002; uv run pytest -q tests/test_scheduler_backfill.py -k unreadable_index_is_not_memoized)`, same with 022).
  - Import added; `object_root.mkdir(...)` → `make_directory_with_explicit_mode(object_root)`; `index_path.write_text(...)` → `write_provider_destination(index_path, "{not json")`. Local macOS RED before, under explicit `umask 002`: `StateManagerError: provider_lock_parent_unsafe`, 1 failed. After: `umask 002` 1 passed, `umask 022` 1 passed. The selector rule `tests/provider_mode_helpers.py` now routes `tests/test_scheduler_backfill.py` (`scripts/select_ci_tests.py`, `# #2403`), which the new importer edge requires (`test_tests_support_module_rules_cover_their_non_gated_importer_closure`).

## 2. #1632 — marker suites under umask 0002 (orchestrator receipt)

- [x] 2.1 node-27 run per design D2 (21 files from `receipts/provider_atomic_closure.py`; first pass covered 17, second pass the 4 the first resolver missed; explicit `umask 002`, all marker opt-ins, one invocation per file) at master `de4d1d182`; receipt `receipts/2026-09-23-issue-1632-umask0002-markers.md` with a per-file table (passed / failed / skipped + reasons) and the log path.
- [x] 2.2 Static census: which of the 21 reference `publish_scheduler_registry_manifest`, `publish_canonical_readiness_index`, `publish_state_snapshot_index`, `stage_nfs_raw_manifest*`, `copyback_run_trees`, `merge_state_snapshot_index*`, `atomic_replace_provider_bytes`, `provider_destination_lock`.
- [x] 2.3 If 2.1 shows a provider-gate failure: fixed with the D1 helpers in this PR (implementer) and re-measured; otherwise record "no provider-gate failure".
  - 2.1: 21 files, `umask=0002`: 692 passed, 12 failed, 1 skipped. All 12 failures fail identically under `umask 022` and are not measured for umask:
    - 8 are GRIB cases (`test_e2e_ifs.py` ×2, `test_production_met_validation.py` ×6). pytest does not wire in node-27's GRIB env. With `/home/nwm/nhms-grib` wired in, they decode, but fail on fixture/decoder expectations, identically under 002 and 022. They are tracked in #2594.
    - 4 are `test_object_store_forcing_real_disk.py`, whose fixture cycle is 2026-06-20 and has already been removed by retention. They are tracked in #2595.
    - The skip is the opt-in real-`data/Basins` smoke. **No provider-gate failure.** 2.2: of the 21, only `test_orchestration_chain.py` names an entry (`copyback_run_trees`, monkeypatched). Receipt: `receipts/2026-09-23-issue-1632-umask0002-markers.md`.

## 3. #1631 — ruling

- [x] 3.1 (orchestrator) Ruling written (design D3). Facts re-verified in three receipts:
  - ACL/ownership, 2026-09-23T13:09:24Z: `receipts/2026-09-23-issue-1631-acl-recheck.txt`;
  - group membership on both hosts, 13:41Z (this corrects the issue's fact 2 for node-22): `receipts/2026-09-23-issue-1631-group-membership.txt`;
  - the gid-1107 share under `canonical/`, 13:46:56Z: `receipts/2026-09-23-issue-1631-group-1107.txt`.
- [x] 3.2 (implementer) Comment-only cites of #1631 and the ruling at the lock-parent gate in `packages/common/provider_atomic.py` and at the explicit `0o755` pin in `packages/common/safe_fs.py::ensure_directory_no_follow`. `git diff` on both files is comment-only.
  - 5-line comments at the `& 0o022` gate (`provider_atomic.py`) and at the `os.mkdir(part, 0o755, …)` pin (`safe_fs.py`), each citing #1631 / ruling D3. `git diff -U0 … | grep '^[+-]' | grep -v '^+++\|^---' | grep -v '^+\s*#'` → empty.

## 4. #2384 — journal-root convergence

- [x] 4.1 (implementer) Adapter per design D4 at both journal-root sites; `_safe_existing_directory` / `_safe_archive_root` catch the `expanduser()` `RuntimeError` → `*_not_absolute`. Restore's raw `stage_root` `expanduser()` → `stage_root_not_absolute`. Every new `except RuntimeError` wraps only the `expanduser()` call (design D4). Census every `expanduser()` in `scheduler_journal_retention.py`, `scheduler_journal_restore.py` and `scripts/node22_scheduler_journal_retention.py`, and report any left.
  - `_verified_journal_root(value, *, setting)` + `_journal_root_refusal_reason` in `scheduler_journal_retention.py`, used by `config_from_env` (`setting` = `--journal-root` or `NHMS_SCHEDULER_JOURNAL_ROOT`) and by `verify_and_restore` (`--journal-root`). Narrow `except RuntimeError` added around `expanduser()` in `_safe_existing_directory`, in `_safe_archive_root`, and at restore's raw `stage_root`. No reason string added or changed. Census: restore `_safe_stage_root`'s `expanduser()` is left on purpose, because it receives the already-expanded path. The script has no `expanduser()`.
- [x] 4.2 (implementer) Tests per design D4 in the existing modules: unexpandable root at retention (function + script `main`) and restore (script `verify-restore`), RED before; parity parametrize (design D4's shape list × both entries) green before AND after; `verify-restore --stage-root ~nosuchuser_zz/x` → `stage_root_not_absolute`, RED before; delegation pin; existing `journal_root_unavailable` pin unchanged.
  - `tests/test_scheduler_journal_retention_planning.py` covers (now 607 lines; the archive module was left at 996 lines, below the 1000-line large-file guard):
    - parity: 10 shapes × 2 entries = 20 cases, green on the UNCHANGED code (vocabulary parity) and after;
    - the unexpandable root at `config_from_env` and at script `main` (exit 2 `preflight_blocked`, empty stderr);
    - the other-field unexpandable roots ×3;
    - `verify-restore` unexpandable journal/archive/stage roots ×3 (exit 2 exact blocked JSON, no stage dir created);
    - the delegation pin (the monkeypatched authority refuses a real dir → `journal_root_unsafe` at both entries; `__cause__` is `OrchestratorError`; settings recorded).
  - RED before: `9 failed, 20 passed` — 8 bare `RuntimeError: Could not determine home directory.` plus the delegation pin's missing attribute. GREEN: `29 passed`.
- [x] 4.3 Detection successor verified: `grep -rn '_safe_existing_directory(' services scripts | grep 'field="journal_root"'` → 0 hits; `grep -rn 'verify_journal_root_authority(' services scripts` lists the retention adapter.
  - Grep 1: `grep -rn '_safe_existing_directory(' services scripts | grep 'field="journal_root"'` → 0 hits (exit 1).
  - Grep 2: `grep -rn 'verify_journal_root_authority(' services scripts` → 19 lines, including `services/orchestrator/scheduler_journal_retention.py:157`.

## 5. Verification

- [x] 5.1 `uv run ruff check .`; `openspec validate provider-mode-and-journal-root-convergence --strict --no-interactive`.
  - `uv run ruff check .` → `All checks passed!` (the closure script was formatted with ruff; its output is unchanged, 421/248/21); `openspec validate … --strict` → valid.
- [x] 5.2 Local focused suites: `tests/test_scheduler_backfill.py tests/test_scheduler_journal_retention_archive.py tests/test_scheduler_journal_retention_planning.py tests/test_safe_fs.py tests/test_scheduler_journal_root_authority.py` + `tests/test_select_ci_tests.py`.
  - 5 focused suites: `185 passed`, with the default umask and under `umask 002`. `tests/test_select_ci_tests.py`: `791 passed`.
- [ ] 5.3 (orchestrator) node-27 isolated oracle at the PR head, explicit `umask 002` echoed: selector output ∪ 5.2 set; plus #2403's single test under `umask 022`.
- [ ] 5.4 CI green on the PR head.

Evidence Floor: 1.1 RED + 5.3 GREEN (002 and 022); 2.1 receipt table + 2.2 census; 3.1 receipts (ACL recheck + group membership on both hosts); 4.1 `expanduser` census; 4.2 RED→GREEN + parity before/after; 4.3 greps; `git diff` on `provider_atomic.py` / `safe_fs.py` comment-only; ruff; openspec strict.
