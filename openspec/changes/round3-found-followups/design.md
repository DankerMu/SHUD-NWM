# Design — round3-found-followups (#3164)

Fixture level: **expanded**. Risk packs: Legacy compatibility, Concurrency (lease fd), Documentation, Release.

## Decisions

### D1 — z_policy tests follow the resolver, not a frozen path
Production `resolve_verdict()` already falls back to `ARCHIVE_VERDICT_GLOB` once the change is archived, and the
checksum is the authority. The two red tests pin `DEFAULT_VERDICT_PATH`:
- `test_resolve_verdict_pinned_default_path_matches_checksum` asserts the resolved path. It changes to: the resolved
  path is either `_REPO_ROOT / DEFAULT_VERDICT_PATH`, or a path relative to `_REPO_ROOT` that matches
  `ARCHIVE_VERDICT_GLOB` (`fnmatch` or `PurePath.match`). It must still red if the resolver returns any other path.
- `test_verdict_resolution_record_includes_override_flag_and_path` reads `DEFAULT_VERDICT_PATH.read_bytes()`. It
  changes to read the bytes from `resolve_verdict().resolved_path`.

Production code is NOT changed. Reproduced red on master: `2 failed, 12 passed`.

### D2 — retire the flip hook, keep the mount slot
Delete `packages/common/station_set_flip.py`, both flip partitions and the shared helper. Remove every selector
artefact keyed on them:
- the rules in `select_ci_tests_rules.py`: the helper rule at ~:476-484 and the owner rule at ~:5580-5590;
- the table constants and the `MODEL_REGISTRY_IMPORTER_TESTS` splat in `select_ci_tests_tables.py` (~:1637-1660 and
  ~:1749-1767);
- the re-exports in `select_ci_tests.py:78-80`;
- every pin in `tests/test_select_ci_tests.py` that names them. A pin that only checked a flip artefact is deleted. A
  pin that also checks something else keeps the other part. Two pins are **re-targeted, not deleted**, because flip
  is their only full-mode subject:
  - `~:6578-6588`, the D0 reverse-direction pin (a full-mode guarded module whose one-hop adds fewer than
    `DIRECT_ONLY_ONE_HOP_THRESHOLD`);
  - `~:6626-6643`, the full-mode parameter of the new-importer mutation test.

  Both move to `packages.common.display_coverage` / `packages/common/display_coverage.py`, whose one-hop contribution is
  0 (`~:6528-6530`). The comment at `~:6576-6578` follows. The guarded-sources count at `~:16477` goes from `== 9` to
  `== 8`;
- the stale comment in `select_ci_tests_rules.py:2453-2456` that names the flip partitions;
- `line_reference_baseline.json` key `packages/common/station_set_flip.py`;
- the two `full_test_durations.json` keys.

The `display_cutover` tracked-tree guard (`tests/test_select_ci_tests.py:~3694`) is deleted. Its spec obligation is
retired by the `orchestrator-structural-burndown` MODIFIED delta, and the refresh-timer half of that guard stays.

`MODEL_REGISTRY_IMPORTER_TESTS` loses only the splat. Its derived direct importers stay, so `model_registry.py`
coverage is unchanged. The comment at ~:1749-1751 that justifies the splat goes with it.

`tests/test_direct_grid_display_cutover_history.py`: delete the import and
`test_station_flag_flip_sut_is_update_only_no_delete_or_history_writes`, and rewrite the module docstring's Claim 4
as retired. Claims 1-3 stay, because their tests do not touch the module.

**Kept:** the `PRE_ACTIVATION_HOOK_MOUNT_POINTS` tuple (including `"station_flag_flip"`), `register_pre_activation_hook`,
the lifecycle and the state-clone hook. The only edit to `model_registry_contracts.py:259-264`,
`model_registry.py:187-190` and `model_registry_lifecycle.py:333-340` is to their comments: they say the
`station_flag_flip` slot has no consumer since #3164 and that its removal is tracked by #3165. These
files are under 1000 lines. `tests/test_variant_activation_cutover.py` is NOT edited (1770 lines, large-file guard).

Reported, not fixed: `register_pre_activation_hook` has no non-test call site for `state_clone` either. Recorded in #3165.

### D3 — archive-family specs
These are spec-only changes. No code reads `NHMS_ARCHIVE_ROOT`. `scripts/node27_timeseries_retention.py:8-13` records
that #1370 deleted the gates, loaders and wrappers. The one remaining code mention is a comment in
`infra/env/node27-resource-governance.example:43`; leave it alone unless it claims the archive is live.

### D4 — stall reason
`PassRecord` gains an optional `lock_reason`, parsed from the pass artifact's `lock.reason` when it is a string and
`None` otherwise; parsing must never raise.

The receipt's `signals` block gains `lock_contended_reason`, beside `lock_contended_streak` and written on every tick.
Its value is `_clip(records[0].lock_reason)` when the newest graded record's status is `lock_contended`, and null
otherwise.

The reason reaches the artifact through two links:
- `scheduler_lease.py:228-237` → `scheduler_runtime.py:712-719`;
- the DB-free safe-code filter, which preserves it (`tests/test_production_scheduler.py:~24212`).

Ordinary contention carries no `reason` key (`scheduler_lease.py:357-363`), so it records null. The stderr line prints
only `verdict=`, and neither the top level nor `signals` has an exact key pin. The new tests pin the new key, its
value in both cases, and that the verdict, exit code and anchor are unchanged.

Runbook: `docs/runbooks/production-ops/stuck-detection.md` §`stall-lock-contended-persistent` (~:293-301) gains a
triage step. Read `signals.lock_contended_reason` first:
- `retired_lock_guard_mode_key:<KEY>` → set `<KEY>` in the scheduler env, at a pass boundary;
- `unsafe_lock_guard_mode` → fix the value to `atomic` or `flock`;
- null → the existing lock-owner check and stale-lock clear.

The probe never clears a lock.

### D5 — lease fd
In `_guarded`, compute `mode = _file_lock_guard_mode()` before `open_lock_parent_directory(...)`. The raise then
happens before any fd exists.

The regression test has to discriminate:
- Build a real lease whose lock parent directory exists under `tmp_path`, so the old code really opens `parent_fd`.
- `monkeypatch.delenv(NHMS_SCHEDULER_LEASE_GUARD_MODE)` and set only the legacy key.
- Enter `with lease._guarded():`. It is a context manager, so a bare call executes nothing.
- Assert `UnsafeSchedulerLockError` with exactly `reason == "retired_lock_guard_mode_key:NHMS_SCHEDULER_LEASE_GUARD_MODE"`.
- Assert the open-fd count (`os.listdir('/dev/fd')`, or `/proc/self/fd` on Linux) is equal before and after.

Monkeypatching the opener is NOT used, because `_scheduler_compat_function` resolves the
`services.orchestrator.scheduler` attribute first. The implementer must show that the test is red on the
pre-fix code (leaked count +1) and records it.

### D6 — umask-independent mode test
Replace `stat.S_IMODE(SCRIPT.stat().st_mode) == 0o755` with two assertions:
- `git ls-files -s -- <script>` reports `100755`;
- `os.access(SCRIPT, os.X_OK)`.

The proof is on node-27: run `git worktree add` with `umask 0002`, because umask applies at checkout. In that worktree
the old assertion is red and the new one green.

### D7 — docs
- `infra/env/README.md`: add a row or note that the node-22 `compute.env` (with `DATABASE_URL`) is the superseded
  compose-lane instance and no unit loads it.
- `docs/spec/03_database_design.md`: check each `file.py:N` reference in ~:145-337 against HEAD. Fix the ones that
  drifted, or replace them with `file.py::symbol`.
- `docs/adr/0002-…md:597`: `packages/common/runtime_storage_source.py` does not exist. Use `git log --all -S` / #846 to
  find the actual module and point there; if nothing is found, mark it as removed.
- `docs/governance/ROLE_BOUNDARY.md`: add `precip.py` and `basemap.py` to the route list, with the role read from the
  code (they are display read-only routes, so verify). Keep `tests/test_role_boundary_static.py` green.
- `docs/governance/API_CONTRACT_RETIREMENT_INVENTORY.md` ~:330/:340 and `openspec/glossary.md:39`: the flip module is
  retired (#3164).
- `docs/runbooks/qhh-mvp-production-like-e2e-checklist.md` and the inventory ~:140: fix every `openspec/specs/<name>`
  that does not resolve, and every spec name that does not exist.

## Must preserve

- Model activation behaviour: the hook chain, rollback-on-raise, already-current skip, and every
  `test_variant_activation_cutover.py` test green and unedited.
- `model_registry.py` selector coverage: its derived importer set.
- Stall verdicts and exit codes; lease acquisition semantics in both modes (`atomic`/`flock`); the retired-key
  fail-closed.
- Entropy hard gate at 0 failing; `openspec validate --strict` for the change and every touched spec.

## Risks

- A selector pin that also guards a non-flip invariant is deleted by mistake. Mitigation: review seat
  test-evidence lists every deleted assertion.
- A deleted flip test was the only oracle for some shared behaviour, for example the history Claims. Mitigation:
  Claims 1-3 stay; deleting Claim 4 is the retirement itself.
