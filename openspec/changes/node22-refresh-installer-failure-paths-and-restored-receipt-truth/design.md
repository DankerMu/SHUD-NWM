## Context

Two node-22 refresh-lane defects are fixed in one PR. Both come from the PR #2289 review, and neither was in that PR's scope.

- **#2294** is the failure-path and baseline contract of the refresh installer, `scripts/install_node22_scheduler_file_provider_refresh.sh`, plus defect 6 in the sibling probe installer, `scripts/install_node22_refresh_timer_health.sh`.
- **#2297** is the `replace_uncertain` receipt that still reports `after_*` for providers whose rollback was verified.

The line numbers in the proposal were re-checked at `be1081414`. Two further facts come from reading this design's own evidence.

**Live baseline (node-22, read-only, 2026-09-25).** No mutation; only `ls`/`od`/`systemctl show` were run.

- `provider-refresh/install-state/` was written 2026-07-15 02:04:35 +0800. It contains:
  - `refresh.before` = `disabled\tinactive\nstatic\tinactive\n` (34 bytes);
  - `scheduler.before` = `enabled\tinactivestatic\tinactive` (31 bytes);
  - both unit `.before` files (1142 and 227 bytes).

  This is already a **re-install snapshot**. Defect 6 has struck once: the units existed and were disarmed when the July install ran. `--rollback` against this baseline therefore restores the July unit files, disabled. It does not restore "no lane". That is disarmed, so it is safe, but the runbook has to say so.
- The live refresh timer is `enabled`/`active` and the refresh service is `static`/`inactive`.
- `refresh-timer-health/install-state/` contains only `protected.before` (2026-09-13). It has no unit `.before` files, so the probe was a first install onto a host with no probe units. The probe timer is `enabled`/`active`.

**`-E` double-runs the handler through command substitutions** (verified locally):

```text
$ bash -c 'set -Eeuo pipefail; h(){ echo "HANDLER pid=$BASHPID main=$$" >&2; }; trap h ERR; [[ "$(false)" == x ]]'
HANDLER pid=80384 main=80382
HANDLER pid=80382 main=80382
```

With `-E`, the ERR trap is inherited into `$( … )`. A failing command inside the substitution runs the handler in the subshell, and then again in the main shell when the enclosing test fails. The refresh installer's `--enable` has exactly this shape at `:161`: `[[ "$($systemctl_bin --user is-active "$timer")" == active ]]`, where `is-active` exits 3 for an inactive unit. So "just add `-E`" would run the restore twice, once from a subshell that cannot even update the main shell's failure flag. The sibling has the same shape at `:216`. That instance is out of scope and is reported as a follow-up, not fixed here (see Risks).

**Governing invariant.** No installer action prints a status line without reading back the state it claims. No `replace_uncertain` `after_*` describes bytes that are not on disk for a provider whose rollback was verified.

**Sibling surfaces.**
- both installers;
- runbook §refresh install/rollback (`file-provider-refresh.md:580-593`) and §probe install (`:751-770`);
- the emergency slot (`runner.py:694-713`);
- `_validate_receipt` (`receipt_validation.py`);
- the R9e reader (`node22_refresh_timer_health.py:131-134,365-420`) and its history fallback;
- `reconstruct_primary_receipt` (accepts only `published_receipt_failed`, so unaffected);
- the probe's `--enable` substitution at `:216` (declared out of scope, #2640).

## Goals / Non-Goals

**Goals**

- Every failure the refresh installer can hit after its first mutation runs **one** restore, in the main shell. The restore runs every step whatever the earlier steps did. It then reads the result back, and exits non-zero without a status line.
- `--rollback` reports success only after reading back that both halves of the runbook promise hold (`file-provider-refresh.md:593`):
  - the refresh units equal the baseline;
  - the compute-scheduler units are unchanged.
- The compute-scheduler comparison is per type and per invocation. It never reads a file written by another invocation.
- Neither installer's `--install` ever disarms an armed timer, and neither ever overwrites a restore baseline that already exists.
- A malformed recorded state fails loudly.
- For every verified-restored provider, a `replace_uncertain` receipt's `after_*` describes the bytes on disk.
- Each guarantee above is pinned by a behavioural test that turns red when the guarding code is removed.

**Non-Goals**

- The node-22 live drill and deploy. These wait for the #1831 maintenance window; see D9.
- Rewriting the installers in Python (the issue's alternative, rejected there).
- Timer-stall detection (#2041/#2146).
- Fixing the sibling's `--enable` substitution double-run (follow-up).
- Splitting `restored is False` into per-record uncertainty (#2297 names it out of scope).
- Any schema or health-probe change.

## Decisions

### D1 — `set -Eeuo pipefail`, one main-shell restore handler, guarded steps (#2294 items 1+2)

The refresh installer switches to `set -Eeuo pipefail`. Trap strings become named handler functions: `install_failure_restore` and `enable_failure_restore`. Each handler follows the same shape:

1. **Main shell only.** First statement: `[[ $BASHPID == "$$" ]] || exit 1`. A failure inside a subshell or command substitution only exits that subshell. The enclosing main-shell command then fails and runs the handler exactly once. Bash ≥ 4 is required for `$BASHPID`; node-22, node-27 and CI all run bash 5.
2. **No re-entry.** `trap - ERR` comes next, so a failing step inside the handler cannot re-enter it.
3. **Every step guarded.** Each restore step is `step || rc=1`: file restore, `daemon-reload`, each `enable`/`disable`/`start`/`stop`. Removing a guard must not be survivable under `-e`. A test injects a failure into each guarded step and asserts that the later steps still appear in the fake-systemctl trace.
4. **Read-backs always run.** The final assertions run unconditionally, each guarded the same way: D2's refresh read-back, then D3's protected read-back.
5. **Always a failure.** A diagnostic line naming each failed step goes to stderr, then `exit 1`. The handler exits 1 even when every restore step succeeded, because the operation it backs out failed. No status line is printed on this path.

`restore_unit_state`'s `enable` and `start` get the same guard as its `disable` and `stop`. Their failures are no longer swallowed silently; they count toward `rc` through the read-back. The read-back is what decides success (D2). This is the ordering #2294 requires: guards are added together with the read-back, never before it.

The one command substitution the refresh installer evaluates inside a trap region that can legitimately fail, `is-active` at `:161`, is rewritten to capture with `|| true` and compare. Correctness no longer depends on the main-shell guard; the guard is a backstop.

### D2 — `assert_refresh_state_restored`: the read-back (#2294 item 3)

`assert_refresh_state_restored <timer_state> <service_state>` reads the refresh units back with `unit_state`. It compares them to the given target by type:

- **timer:** unit-file state and active state;
- **service:** unit-file state only. The service is a oneshot the timer drives, so the same reasoning as D3 applies.

On a mismatch it names the unit, the expected value and the observed value on stderr, and returns 1.

Restore targets:

| path | target |
|---|---|
| `--rollback` | the baseline in `refresh.before` |
| `install_failure_restore` | the baseline in `refresh.before`. A failed `--install` is backed out to the same state `--rollback` produces. The install trap and `--rollback` then share one restore function and one target, and when the baseline predates this invocation, a failed re-install leaves the host rolled back, not half-updated. Under D4 every baseline captured by the new installer records a disarmed timer, so this restore never re-arms. |
| `enable_failure_restore` | this invocation's starting state, captured before `enable --now` as on master. A failed `--enable` must not disarm a lane that was already armed. |

`#2294` literally asks for every trap to be read back against `refresh.before`. The enable trap deliberately deviates from that wording, for the reason in the last row, and the deviation is recorded in the PR.

**Disarmed predicate for the refresh units.** One shell function, `refresh_units_disarmed`, is used by both the D4 refusal and the `--install` success read-back. It mirrors the sibling's `assert_probe_units_gone` sets:

- `is-enabled ∈ {disabled, static, not-found, ''}`;
- `is-active ∈ {inactive, failed}`.

Master's `--install` swallows `disable --now` (`:147`) and prints `installed_stopped` without looking. It now reads back `refresh_units_disarmed` before printing. This follows directly from D1, which guards the swallowed call, so it is in scope. It is the #2294 item-2 pattern applied to the success path.

### D3 — Per-type, per-invocation compute-scheduler baseline (#2294 items 4+5)

`protected_state` is copied from the sibling's shape: `unit\tenabled\tactive` for `nhms-compute-scheduler.timer`, and `unit\tenabled` for `nhms-compute-scheduler.service`. It is captured into a shell variable before the action's first mutation, in every action. `assert_scheduler_unchanged` compares against that variable.

`scheduler.before` is **neither read nor written**. The 31-byte legacy file on node-22 is ignored and left in place: no code path parses it, so its format cannot break anything. The refresh installer's two protected units are only the compute scheduler's. The refresh units are what this installer manages, so D2 covers them.

The status lines keep their `"scheduler_unchanged":true` key (must-preserve).

### D4 — `--install` never disarms and never overwrites a baseline (#2294 item 6, both installers)

**Refresh installer.**

- **Refusal when armed.** `--install` starts by evaluating `refresh_units_disarmed` for the refresh timer and service. If either is armed, it prints `refusing --install: <unit> is <enabled>/<active>; run --rollback first` to stderr and exits 1. Nothing is mutated: the fake-systemctl trace shows only read-only verbs, and no file under the unit dir or the state root changes.
- **Baseline is written once.**
  - When `refresh.before` exists, it is the baseline: it and the unit `.before` files are neither rewritten nor removed.
  - When it is absent, the unit `.before` files are captured first. `refresh.before` is then written last, through a temp file in the state root and `mv`, so its presence marks a complete baseline. An interrupted first install leaves no `refresh.before`, and the next install recaptures everything.
  - The legacy 2-line format is the format the strict parser (D5) accepts, so node-22's existing file is read as-is.
- **Rollback keeps the baseline.** `--rollback` does not delete the baseline, so a repeated rollback is idempotent. The documented reset is: after a successful `--rollback`, delete `refresh.before` and both unit `.before` files. A first `--install` on a host whose timer is armed with no baseline (armed by hand) refuses; the runbook says to disarm it by hand first.

**Probe installer (sibling).**

- The same refusal applies to the probe timer and service. It uses the same state sets as `assert_probe_units_gone`, but through its own check with its own stderr text (`refusing --install: <unit> is <enabled>/<active>; run --rollback first`). It must not call `assert_probe_units_gone`: its "probe read-back: … is still …" message would let a refusal test pass while exercising the read-back.
- The refusal runs **before** the per-invocation `protected_state > protected.before` capture (`:180`). A refused `--install` therefore leaves every file under the state root and the unit dir byte-identical, including `protected.before`.
- The baseline marker is a new file, `install.baseline`, written through temp and `mv` after the unit `.before` capture.
  - With the marker present, the unit `.before` files are kept.
  - Without it, they are captured fresh. This covers first installs and legacy state.
- `protected.before` remains a per-invocation capture, unchanged.
- Legacy note, documented: node-22's probe state has no marker. Because the timer is armed, the refusal forces `--rollback` first. That rollback removes the units (there are no `.before` files), which is correct, and the next `--install` records "no probe units" as its baseline. On a legacy host that was installed but not armed, run `--rollback` once before the first new `--install`; otherwise the installed files become the baseline.
- R15c changes. `test_rollback_after_two_installs_keeps_the_first_installs_disarmed_files` becomes a test that two installs, then a rollback, restore the files that preceded the **first** install. The spec scenario changes to match (MODIFIED requirement).

### D5 — Strict state parsing (#2294 item 7)

`parse_unit_state <state>` takes exactly two tab-separated, non-empty fields with no embedded newline. It sets `parsed_enabled`/`parsed_active` or returns 1 with a message.

`refresh.before` must be exactly two lines, each parseable. Any other shape makes `--rollback` fail before its first mutation and makes the install trap's restore step fail, which counts toward `rc`, and the read-back still runs. The sibling's `restore_probe_timer` read gets the same parser.

### D6 — `after_*` of verified-restored providers describes disk (#2297, user option (a))

This changes only `rollback_receipt_if_needed` (`runner.py:118-154`). When `restored is True` and `transaction_uncertainty[0]` is True, the `replace_uncertain` receipt's providers are a copy of `committed`. Each provider whose `name` matches a record in the verified `rollback_stack` gets:

- `after_sha256 = before_sha256`;
- `after_schema_version = before_schema_version`;
- `after_generated_at = before_generated_at`;
- `after_payload_checksum = before_payload_checksum`.

`entry_count` is kept, as on the dry-run lane (`runner.py:486-490`). It is the count of the generation this run attempted, and the schema gives it no "after" meaning.

- **Name-matched and defensive.** A committed provider with no matching record keeps its evidence unchanged. In the tracked mode that reaches this branch, every committed provider has a record.
- **Unverified rollback.** When `restored is False`, the branch is byte-for-byte unchanged.
- **No re-collection.** `restored is True` already proves that each record's path equals `record.previous` by sha256 (`providers.py:261-280`). The substitution needs no new filesystem read and has no new failure mode.
- **Acceptance is stated against disk, not against `before_*`.** For every restored provider, the test asserts `after_sha256 == sha256(on-disk bytes)`, or `None` when the path is absent. It also asserts that `after_generated_at` equals the on-disk manifest's `generated_at` where the payload has one. `_provider_evidence` derives `before_*` from the preimage, a different source from `record.previous`, so the test checks the claim that matters.
- **Schema and validator unchanged.** Every `before_*`/`after_*` is nullable; `receipt_validation.py:92-99` accepts `None` digests. Mirror equality is enforced only for `dry_run`/`published` (`:133-136`). An existing receipt stays valid.
- **Emergency record.** Its content follows the primary receipt, so the emergency slot carries the corrected evidence too. `reconstruct_primary_receipt` accepts only `published_receipt_failed`, so it is unaffected.
- **R9e is kept (evaluation #2297 asks for).** The health probe's outcome rule (`node22_refresh_timer_health.py:131-134`) and the runbook `jq` stay. A `restored is False` receipt still carries post-publish `after_*` that may not be on disk, so readers must still choose the field by `outcome`. The runbook prose is updated: after this change, `replace_uncertain` `after_*` equals disk for verified-restored providers, and the rule stays because of unverified rollbacks. The pinned runbook substrings `then .after_generated_at else .before_generated_at end` and ``时取 `registry.after_generated_at` `` (asserted by `tests/test_node22_refresh_timer_health_history.py:281-286`) are preserved.
- **Pinning test rewritten.** `test_replace_uncertain_receipt_carries_after_evidence_for_restored_registry_bytes` and its D3b docstring are replaced by a test built on `_tracked_transaction_fixture(fail_lane="", unowned_lane="state")`. It asserts the disk-truth claim for `registry`, `registry_worker_mirror` and `readiness`, asserts that `state` is absent from `providers` (it never committed), and asserts that `latest.json` equals the returned receipt. A companion test forces `restored is False` and asserts that the evidence is exactly the pre-change evidence.

### D7 — Tests: behavioural, divergence and mutation (#2294 item 8)

- **Replace the one-sided checks.** The source-substring checks in `test_systemd_refresh_contract_is_db_free_daily_and_scheduler_independent` (`tests/test_scheduler_refresh_deployment_contract.py:96-142`) that concern installer behaviour are replaced by fake-systemctl runs. They include `"scheduler_unchanged" in installer` and `'restore_unit_state "$timer"' in installer`. The unit-file, env-template and DB-free assertions in that test are not about installer behaviour and stay.
- **Harness.** The existing fake systemctl keeps `FAKE_SYSTEMCTL_STATE`, `FAKE_SYSTEMCTL_TRACE` and `FAKE_FAIL_AFTER`, and gains:
  - a per-verb/per-unit failure injector;
  - a hook that flips `nhms-compute-scheduler.service` active between calls, to simulate the oneshot firing.
  - **real `is-active`/`is-enabled` exit codes**: non-zero (3 for `is-active` on an inactive unit, 1 for `is-enabled` on a `disabled`/`masked`/missing one) whenever real systemd would be; `is-enabled` exits 0 for `enabled`, `static`, `indirect` and `generated`, as systemd does. The existing fakes always exit 0 (`tests/node22_refresh_timer_health_helpers.py:242-326`, reported in #2640), which hides the substitution double-run. The main-shell and exactly-one-restore scenarios are only meaningful against a fake with these exit codes.
- **Divergence scenarios.** Each asserts the exit code, the absence of a status line, and the trace. There are three classes, each with its own expected trace:
  - **(A) failure before any mutation.** Cases: `validate_current_receipt` failing in `--enable`; the D4 refusal in `--install`; the D5 parser failing on a malformed `refresh.before` in `--rollback`; a missing `refresh.before` in `--rollback`. Expected: rc ≠ 0, no status line, **no mutating verb** in the trace, no restore sequence, and the state root and unit dir file sets byte-identical.
  - **(B) `--rollback` read-back failure.** Cases: D2's `assert_refresh_state_restored` or D3's `assert_scheduler_unchanged` failing after the rollback steps ran. Expected: rc ≠ 0, no status line, and the rollback steps in the trace exactly once. Rollback is itself the restore, so it has no trap and runs no second restore.
  - **(C) failure after the first mutation in `--install`/`--enable`.** Cases: a D3 assertion failing (called as a function); the `--install` success read-back failing; the post-arm `is-active` check failing. Expected: rc ≠ 0, no status line, and **exactly one** main-shell restore sequence, followed by both read-backs.
  - The `--install` success-read-back case in (C) is reached through a fake-systemctl divergence: after unit placement and `daemon-reload`, the fake answers `enabled`/`active` for the refresh timer, as if the swallowed `disable --now` had not taken effect. D7 mutation 9 depends on this scenario.
  - a failure in each guarded step inside each handler;
  - a restore that silently leaves the timer enabled, so the read-back must catch it;
  - `is-active` failing inside a command substitution, with exactly one restore sequence in the trace;
  - the compute-scheduler oneshot flipping active mid-run, which must not abort;
  - the refusal-when-armed, with no mutation;
  - two installs then a rollback, which restores the first baseline, for both installers;
  - an interrupted first install, recaptured by the next, with no temp-file residue left in the state root;
  - the legacy `scheduler.before` present with a garbage format, which is ignored;
  - the legacy `refresh.before` in node-22's exact bytes, which is accepted.
- **Mutation acceptance.** This is what the issue requires. A parametrized test copies the installer and applies one exact-anchor mutation. Each mutation first asserts `source.count(anchor) == 1`, so a refactor that removes the anchor turns the test red instead of making it a no-op. The test then runs the scenario that mutation must break, and asserts the scenario's verdict flips. Mutations:
  1. `-E` dropped;
  2. `assert_scheduler_unchanged` deleted from `--install`, `--enable`, `--rollback`, `install_failure_restore` and `enable_failure_restore` (5 mutations);
  3. `assert_refresh_state_restored` deleted from `--rollback`, `install_failure_restore` and `enable_failure_restore` (3);
  4. the main-shell guard deleted;
  5. one handler step's `|| rc=1` guard deleted;
  6. the refusal deleted;
  7. baseline preservation deleted;
  8. the strict parser's field-count check deleted;
  9. the `--install` success read-back deleted.

  The sibling gets mutations for its refusal and marker preservation.
- **Tests used as-is.** The #2297 tests are plain pytest on the fixture helper.
- **Placement and budget.**
  - New tests go in new files: `tests/test_scheduler_refresh_installer_failure_paths.py`, `tests/test_scheduler_refresh_installer_mutations.py` and `tests/test_scheduler_refresh_restored_receipt_truth.py`, sharing the fake-systemctl harness through `tests/scheduler_refresh_installer_harness.py`. The sibling's install-twice change is made in place in `tests/test_node22_refresh_timer_health_installer.py`, if it stays ≤ 1000 lines; otherwise it moves to a new partition.
  - `.large-file-guard.json` sets maxLines 1000. Current sizes: 596 (deployment contract), 723 (probe installer), worker-mirror transactions checked at implement time.
- **CI routing (same PR).** `scripts/select_ci_tests.py` exact-set tuples route the new files:
  - `SCHEDULER_REFRESH_TESTS` / `SCHEDULER_REFRESH_DEPLOYMENT_TESTS` for the refresh installer and runner;
  - `NODE22_REFRESH_TIMER_HEALTH_*` for any new sibling partition;
  - a helper path constant for the harness.

  `tests/test_select_ci_tests.py` closes the corpus against the tracked tree, so its pins are updated in the same commit. The pre-commit check is `git add -N` plus a local run of `tests/test_select_ci_tests.py` (Batch N lesson).

### D8 — Docs

`docs/runbooks/production-ops/file-provider-refresh.md`:

- Rewrite the `:592-593` installer promise to describe D1-D4. That covers the two read-backs, the refusal when armed, the baseline lifecycle and reset, the node-22 note that the existing baseline is a July re-install snapshot (restoring it leaves the July unit files, disabled), and the legacy `scheduler.before` being ignored.
- Update the probe-installer prose at `:751-765` for the first-install baseline, the marker and the legacy note.
- Update the receipt `after_*` semantics at `:610-616` / `:729-735`, keeping the pinned substrings (D6).

### D9 — Verification and the pending window

- **Oracle.** node-27 pytest (Linux, bash 5, GNU coreutils) on the pushed SHA: the targeted set plus the whole `tests/test_select_ci_tests.py`. Locally: `bash -n` on both installers, ruff, and `openspec validate --strict`.
- **Pending the #1831 window (not faked, not skipped).** The issue's own drill starts with `--install`. Under D4 that now refuses on node-22, whose timer is armed, and run as written it would leave production disarmed. The drill sequence is therefore:
  1. `git status --porcelain` → `git pull --ff-only` (window only).
  2. Record before-state: `od -c` of `refresh.before` and `scheduler.before` (the latter informational only), and `systemctl --user show -p UnitFileState -p ActiveState` for the four units.
  3. `--rollback`. The refresh units must read back as the baseline (disabled/inactive, static/inactive), and the scheduler must be unchanged.
  4. `--install`. The baseline is kept (the file is byte-identical to step 2), and the lane is `installed_stopped`.
  5. `--enable` → `enabled_active`.
  6. A failure-path drill: re-run `--install` while armed. It must refuse with no mutation.
  7. The lane is left armed (`--enable` done in step 5), and the after-state is recorded.
  8. The probe installer: `--rollback`, `--install`, `--enable`, with its state recorded.

  Each step's stdout, stderr and rc, and the before and after `show` output, are written into a receipt under `docs/runbooks/receipts/`. Until then, the PR body and tasks §5 say this evidence is **pending the #1831 window**, and #2294 stays open for that acceptance item.

## Risks / Trade-offs

- **A failed re-install now rolls back to the baseline instead of leaving the previous install's files.** Both states are disarmed. Choosing a single target keeps the install trap and `--rollback` identical and testable. The runbook states it.
- **The baseline can be stale on purpose.** It is kept until an operator resets it. On node-22 it is the July re-install snapshot, not pre-lane. This is documented, and it is safe because it is disarmed.
- **Sibling legacy state** without the marker needs one `--rollback` before the first new `--install` when installed-but-unarmed. Documented.
- **`$BASHPID` needs bash ≥ 4.** macOS `/bin/bash` 3.2 is not a target of either installer. Tests run under bash 5 (node-27 oracle). The local macOS run uses Homebrew bash if present, or skips with a reason.
- **The sibling's `--enable` at `:216` has the same substitution double-run.** It is out of scope (report, don't fix): filed as #2640. The handler there is idempotent, so it only runs a redundant restore.
- **`entry_count` of a restored provider still reports the attempted generation's count.** This matches dry-run; the schema has no after/before split for it. Documented in D6.
