## Risk packs

- **Error handling / rollback: selected.** This is the subject of #2294 items 1-3.
  - D1: one main-shell handler with guarded steps and read-backs that always run;
  - D2: the read-back;
  - D4: refusal with no mutation.

  Covered by 2.1-2.4 and the mutation set in 2.6.
- **Public API / CLI / script entry: selected.**
  - Both installers keep `--install|--enable|--rollback`, the usage exit code 2, the db-selector exit 2 and the status JSON lines.
  - New behaviour: `--install` refuses while armed (non-zero, stderr, no status line).
  - Callers: the runbooks `file-provider-refresh.md` §refresh install and §probe install. Covered by 2.4 and 2.8.
- **Legacy compatibility: selected.**
  - node-22 has a live `refresh.before` in the 2-line format, which is accepted as-is (D5).
  - It also has a 31-byte `scheduler.before`, which is ignored (D3).
  - The probe state has no marker (D4 legacy note).
  - Existing receipts stay schema-valid (D6).

  Covered by 2.5 and 3.2.
- **Schema / field names (receipt semantics): selected.** The `after_*` meaning for verified-restored providers in `replace_uncertain` changes. The schema, the validator and the R9e reader are all unchanged (D6). Covered by 3.1-3.3.
- **Release / operational: selected.** The node-22 deploy and the drill (D9, §5) wait for the #1831 window. The runbook documents the new drill order and the baseline reset (2.8).
- **File IO / path safety / overwrite: selected.** D4 is overwrite semantics:
  - temp + `mv` of `refresh.before` and `install.baseline` inside `state_root`;
  - keeping versus recapturing the unit `.before` files;
  - the refusal's byte-identical state root and unit dir, including the probe's `protected.before`.

  Covered by 2.4 and 2.6: D7 class (A) file-set assertions, and the interrupted-install case with no temp residue.
- **Documentation: selected.** The runbook (2.8) and this file.
- **Not selected:**
  - Migration: no DB.
  - Auth: no credentials; the env-file checks are unchanged.
  - Concurrency: the installers are operator-serialised. The refresh lock is untouched.
  - Performance: no hot path.
  - Display identity: not involved.

## Must-preserve

- **Status lines**, byte-exact:
  - refresh installer: `{"status":"installed_stopped","scheduler_unchanged":true}`, `{"status":"enabled_active","scheduler_unchanged":true}`, `{"status":"rolled_back","scheduler_unchanged":true}`;
  - probe installer: the same three statuses, with `"protected_unchanged":true`.
- **Refresh installer checks:** the usage `exit 2`; the db-selector `exit 2`; the env file (`600`, not a symlink, exactly one `NHMS_SCHEDULER_REQUIRE_DIRECT_GRID=true`); the `cmp -s` of unit files on `--enable`; `validate_current_receipt` before `--enable` arms.
- **Refresh service must be inactive** at the start of every refresh-installer action.
- **Environment variables** (`NHMS_SCHEDULER_REFRESH_*`, `NHMS_REFRESH_HEALTH_*`) and their defaults.
- **The probe installer's four-unit protected comparison**, per invocation, unchanged.
- **The probe's `assert_probe_units_gone` sets.**
- **Receipt schema:** `schemas/scheduler_file_provider_refresh_receipt.schema.json` is unchanged. The `restored is False` evidence is unchanged. The `restored_previous` branch is unchanged.
- **The R9e rule** in `scripts/node22_refresh_timer_health.py`, and the runbook substrings pinned by `tests/test_node22_refresh_timer_health_history.py:281-286`.
- **Unit files**, `infra/systemd/*`, are unchanged.

## 1. Baselines

- [x] 1.1 node-22 read-only baseline (design Context), 2026-09-25:
  - `refresh.before` = `disabled\tinactive\nstatic\tinactive\n` (34 B), `scheduler.before` = `enabled\tinactivestatic\tinactive` (31 B), both unit `.before` files present, all dated 2026-07-15 02:04:35 +0800;
  - refresh timer `enabled`/`active`, service `static`/`inactive`;
  - probe state: only `protected.before` (2026-09-13); probe timer `enabled`/`active`.
- [x] 1.2 The `-E` substitution double-run is reproduced locally (design Context). The refresh installer's `:161` and the sibling's `:216` have that shape.
- [x] 1.3 The validator accepts a `None` `after_sha256` (`receipt_validation.py:92-99`), and mirror equality is enforced only for `dry_run`/`published` (`:133-136`).

## 2. #2294: installers

- [ ] 2.1 **D1** in `scripts/install_node22_scheduler_file_provider_refresh.sh`:
  - `set -Eeuo pipefail`;
  - the `install_failure_restore` / `enable_failure_restore` handlers: main-shell guard first, `trap - ERR`, every step `|| rc=1`, read-backs always run, stderr diagnostics, `exit 1`, no status line;
  - `restore_unit_state` `enable`/`start` guarded;
  - the in-trap `is-active` substitution captured with `|| true`.
- [ ] 2.2 **D2**:
  - `assert_refresh_state_restored` (timer: both fields; service: UnitFileState), with targets as in the design table;
  - the `refresh_units_disarmed` predicate, used by the refusal and by the `--install` success read-back.
- [ ] 2.3 **D3**:
  - a per-invocation, in-memory, typed `protected_state` for the compute-scheduler timer (both fields) and service (UnitFileState);
  - `scheduler.before` neither read nor written.
- [ ] 2.4 **D4** in both installers:
  - refusal while armed, with no mutation;
  - the baseline is written once: refresh = `refresh.before` written last via temp + `mv`; probe = an `install.baseline` marker via temp + `mv`;
  - rollback keeps the baseline;
  - the probe refusal runs before the `protected.before` capture, with its own `refusing --install:` stderr text;
  - the sibling R15c test is rewritten to "restore the files that preceded the first install";
  - `test_install_refuses_installed_stopped_when_systemctl_refused_to_disarm_the_probe` (`tests/test_node22_refresh_timer_health_installer.py:519-544`) is rewritten into two cases: the armed-probe refusal (asserting the `refusing --install:` text), and a reachable read-back case, in which a disarmed probe reads back `enabled`/`active` after install and must hit `probe read-back:` with no status line.
- [ ] 2.5 **D5** in both installers: `parse_unit_state` with a strict 2-field check, and `refresh.before` held to exactly 2 lines.
- [ ] 2.6 **D7 tests.** New files `tests/scheduler_refresh_installer_harness.py`, `tests/test_scheduler_refresh_installer_failure_paths.py`, `tests/test_scheduler_refresh_installer_mutations.py`, plus a sibling partition if needed.
  - The divergence list in D7, each scenario asserting rc, the missing status line, and the fake-systemctl trace.
  - The mutation list in D7: each mutation asserts `source.count(anchor) == 1`, then shows the scenario's verdict flipping.
  - The installer-behaviour substring checks at `tests/test_scheduler_refresh_deployment_contract.py:96-142` are replaced. Its unit/env/DB-free assertions stay.
  - The existing lifecycle test stays green, updated only where D4 changes the expected trace.
- [ ] 2.7 **CI routing:** `scripts/select_ci_tests.py` tuples and helper constants for every new file, and the exact-set pins in `tests/test_select_ci_tests.py` updated. Checked with `git add -N` plus a local run of `tests/test_select_ci_tests.py`.
- [ ] 2.8 **D8 runbook:** `docs/runbooks/production-ops/file-provider-refresh.md`:
  - the installer promise;
  - the refusal when armed;
  - the baseline lifecycle and reset;
  - the node-22 July re-install-snapshot note;
  - `scheduler.before` ignored;
  - the probe first-install baseline, marker and legacy note;
  - the new drill order (D9).

## 3. #2297: receipt truth

- [ ] 3.1 **D6** in `scripts/scheduler_refresh/runner.py` `rollback_receipt_if_needed`, and only there. On `restored and uncertainty`, the name-matched copies of `committed` get `after_* := before_*` for the four fields; `entry_count` is kept. No other branch changes.
- [ ] 3.2 **Tests** in `tests/test_scheduler_refresh_restored_receipt_truth.py`:
  - the disk-truth assertions for `registry`, `registry_worker_mirror` and `readiness`;
  - `state` absent;
  - `latest.json == receipt`;
  - `_validate_receipt` accepts the receipt;
  - the emergency-slot record carries the same corrected evidence;
  - a forced `restored is False` gives exactly the pre-change evidence.

  The pinning test in `tests/test_scheduler_refresh_worker_mirror_transactions.py:626-652`, with its D3b docstring, is removed or rewritten. No other test's claim is weakened; any that pinned the old `after_*` is listed in the PR.
- [ ] 3.3 **R9e evaluation:** kept (design D6). The runbook prose is updated and the pinned substrings are preserved.

## 4. Verification (node-27 oracle + local)

- [ ] 4.1 Local:
  - `bash -n` on both installers;
  - `uv run ruff check .`;
  - `openspec validate node22-refresh-installer-failure-paths-and-restored-receipt-truth --strict --no-interactive`;
  - the targeted pytest set, with the whole of `tests/test_select_ci_tests.py`.
- [ ] 4.2 node-27 on the pushed SHA (`/home/nwm/tmp/node27-pr-runner.sh`, `TMPDIR=/home/nwm/tmp`):
  - the targeted set: all new files, `tests/test_scheduler_refresh_*`, `tests/test_node22_refresh_timer_health_*`, `tests/test_select_ci_tests.py`;
  - then the full suite.

  The full-suite failure set must be a subset of master's (#2615).
- [ ] 4.3 Red proof: the new failure-path, mutation and receipt-truth tests run against master's installers and runner must fail. Record the counts.

## 5. node-22 live drill: PENDING the #1831 maintenance window

This section is **not done in this PR** and is not claimed done. #1831 forbids pulling onto the node-22 active checkout before the window. The PR body states that this is pending, and #2294 stays open for its last acceptance item.

- [ ] 5.1 During the window only: `git status --porcelain` → `git pull --ff-only` on `/scratch/frd_muziyao/NWM`. Only the active interpreter or the checked-in wrappers are used; no `uv`.
- [ ] 5.2 The D9 sequence:
  1. before-state;
  2. `--rollback` with read-back;
  3. `--install` with the baseline kept byte-identical;
  4. `--enable`;
  5. `--install` while armed, which must refuse with no mutation;
  6. after-state, with the lane left **armed**;
  7. the probe installer: `--rollback` → `--install` → `--enable`.
- [ ] 5.3 A receipt at `docs/runbooks/receipts/<date>-issue-2294-installer-drill-node22.md`: each step's stdout, stderr and rc, the before and after `systemctl --user show` of the six units, and `od -c` of `refresh.before` before and after.

## Evidence Floor

1. **Defects 1+2** (D7 classes):
   - (A) a pre-mutation failure (`validate_current_receipt`, the refusal, the D5 parser, a missing baseline) gives rc ≠ 0, no status line, no mutating verb and byte-identical file sets;
   - (B) a `--rollback` read-back failure gives rc ≠ 0, no status line, and the rollback steps once;
   - (C) a post-mutation failure in `--install`/`--enable` runs exactly one main-shell restore, including through the `is-active` substitution, then both read-backs; rc 1 and no status line;
   - a failure injected into each handler step leaves the later steps and both read-backs in the trace.
2. **Defect 3:**
   - `--rollback`, the install trap and the enable trap each read back the refresh units against their D2 target;
   - a restore that silently leaves the timer enabled fails.
3. **Defects 4+5:**
   - a mid-run activation of the compute-scheduler oneshot does not abort;
   - a garbage `scheduler.before` is ignored;
   - `--enable`/`--rollback` run without any `scheduler.before`.
4. **Defect 6 (both installers):**
   - `--install` refuses while armed, with no mutating verb in the trace;
   - two installs then a rollback restore the first baseline;
   - an interrupted first install is recaptured.
5. **Defect 7:** a 1-field, 3-field and 3-line state each fail.
6. **Defect 8:** every D7 mutation flips its scenario (the anchor count is asserted).
7. **#2297:**
   - disk-truth `after_*` for the restored providers;
   - the `restored is False` evidence is unchanged;
   - the receipt is valid;
   - R9e is kept and documented.
8. **Pending the #1831 window:** the node-22 drill receipt (§5).
