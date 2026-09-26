## Triage

```text
Issue type: bugfix (#2294, #2297)
Fixture level: expanded
Upstream suggested level: absent (#2294 is L, touches the installer of a live production lane plus its rollback guarantees; #2297 changes an audit receipt's field semantics)
Blast radius: a wrong trap/rollback path leaves node-22's production refresh lane half-restored or falsely reported restored; a wrong receipt rewrite makes the audit record lie in the other direction
Selected risk packs: Error handling/rollback, Public API/CLI/script entry, Legacy compatibility, Schema/field names (receipt semantics), Release/operational, Documentation (tasks.md)
Evidence floor: tasks.md Evidence Floor 1-8 (1-7 node-27 oracle pytest; 8 = node-22 live drill PENDING the #1831 maintenance window)
```

## Why

Batch O of the 10-batch serial run. Master is `be1081414`, after N #2637 / #2639.

### #2294: the node-22 refresh installer's failure paths do not do what the runbook promises

`scripts/install_node22_scheduler_file_provider_refresh.sh` installs the **live** node-22 file-provider refresh lane. The file split after #2294 was filed did not touch it. All 8 defects are still present at `be1081414`:

1. **`:2` is `set -euo pipefail`, without `-E`.** The ERR traps at `:140` and `:159` therefore never run when a function fails inside the trapped region, for example `assert_scheduler_unchanged` (`:149`, `:163`, called as a function). The script exits 1 with no rollback at all. (`validate_current_receipt` at `:156` runs before the `--enable` trap and before any mutation, and `restore_refresh_state` runs only inside the install trap body or the untrapped `--rollback`. Neither is an in-trap failure.)
2. **Trap bodies are not guarded.**
   - In `rollback_files`, the `daemon-reload` at `:113` is unguarded.
   - In `restore_unit_state`, the `enable` at `:58` and the `start` at `:63` are unguarded, while `disable`/`stop` are guarded.

   Once `-E` is on, the first failing step cuts the trap short: the rollback is only partly done, and the final assertion never runs.
3. **`--rollback` never reads back the refresh units** (`:166-171`). `restore_refresh_state` (`:69-76`) writes the state without checking it, and the only assertion covers a different pair of units, `nhms-compute-scheduler.*`. The runbook promise at `docs/runbooks/production-ops/file-provider-refresh.md:593` has two halves ("restore the refresh initial state" and "assert scheduler units unchanged"); only the second is asserted.
4. **The protected comparison is not type-aware** (`:45-50`). It compares `is-active` of `nhms-compute-scheduler.service`, a oneshot that its own timer activates every 5 minutes.
5. **`scheduler.before` is a cross-invocation disk contract.** Only `--install` writes it (`:126-128`), but `--enable`/`--rollback` read it (`:47`) when run as separate commands. Its format is a 4-field concatenation (`enabled\tinactivestatic\tinactive`, 31 bytes on node-22).
6. **A second `--install` overwrites the rollback baseline, and always disarms the timer.** `:126-132` rewrites `refresh.before` and the unit `.before` files on every run, and `:147` runs `disable --now` on the timer unconditionally. The sibling `scripts/install_node22_refresh_timer_health.sh`, now on master, has the same shape: `:183-189` rewrite, `:198` `disable --now`. Its test `test_rollback_after_two_installs_keeps_the_first_installs_disarmed_files` (`tests/test_node22_refresh_timer_health_installer.py:547-572`) and the spec scenario "rollback restores whatever unit files preceded the last install" treat this as intended (R15c).
7. **`IFS=$'\t' read -r enabled active` (`:56`) does not check the field count.** A malformed state silently merges fields.
8. **Test coverage has gaps.**
   - `tests/test_scheduler_refresh_deployment_contract.py:164-371` drives the real installer against a fake `systemctl` and asserts recorded state, but it injects only **top-level command** failures. Those already trigger the trap, so defects 1 and 2 are never exercised.
   - `:96-142` checks only substrings of the source (one-sided): `"scheduler_unchanged" in installer`, `'restore_unit_state "$timer"' in installer`, and so on.

The sibling installer has already solved defects 1, 2 (the `|| true`-guarded `daemon-reload` at `remove_probe_units`), 4 and 5, with `set -Eeuo pipefail`, typed `protected_timers`/`protected_oneshots`, and per-invocation `protected_state`. It is the reference implementation, except for defect 6.

### #2297: a `replace_uncertain` receipt claims bytes that were rolled back

In `scripts/scheduler_refresh/runner.py:118-154`, `rollback_receipt_if_needed` can find `_rollback_provider_transaction` (`providers.py:261-280`) returning `True`: every rollback record is sha256-verified as restored to `previous`. If a later lane has already set `transaction_uncertainty[0]` at that point, the `else` branch writes `outcome="replace_uncertain"` with `providers=committed`.

The `committed` evidence (`runner.py:492,495,529`) still carries the post-publish `after_sha256`/`after_schema_version`/`after_generated_at`/`after_payload_checksum` of bytes that are no longer on disk. The emergency-slot record (`runner.py:694-713`) persists the same receipt.

Current readers cope only because they choose fields by `outcome`:
- the health probe's R9e rule, `TRUSTED_AFTER_GENERATED_AT_OUTCOMES` (`scripts/node22_refresh_timer_health.py:131-134`, `:365-420`);
- the runbook's `jq` (`file-provider-refresh.md:610-616`, `:729-735`).

The pinning test `test_replace_uncertain_receipt_carries_after_evidence_for_restored_registry_bytes` (`tests/test_scheduler_refresh_worker_mirror_transactions.py:626-652`) records this as design "D3b".

## What Changes

- **#2294 in the refresh installer, all 8 items in one pass.** #2294 imposes an order: 1 with 2, and 3 with or before 2.
  - `set -Eeuo pipefail`.
  - Every trap body runs every restore step guarded, collects a failure flag, always runs its final read-back assertions, and exits non-zero without a status line.
  - A new `assert_refresh_state_restored` read-back runs after every restore (`--rollback`, the install trap, the enable trap).
  - The protected comparison becomes type-aware, and its baseline is captured in memory at the start of every invocation. The legacy `scheduler.before` is neither read nor written. The existing 31-byte file on node-22 is left untouched and ignored.
  - `--install` refuses, with no mutation, while the refresh timer is armed. It keeps an existing restore baseline instead of overwriting it.
  - The strict state parser fails on a wrong field count.
- **#2294 item 6 also in the sibling installer.** User decision, 2026-09-25: "两个都改" (change both).
  - `--install` refuses while the probe timer is armed.
  - The unit-file baseline is captured only by the first install, marked complete by a marker file.
  - The R15c test and the spec scenario change to "rollback restores the unit files that preceded the first recorded install".
- **#2297, option (a).** User decision, 2026-09-25: "(a) after_*=回滚后磁盘" (after_* = the on-disk bytes after the rollback). When the rollback is verified (`restored is True`) but the receipt still has to be `replace_uncertain`, each committed provider whose rollback record was verified gets `after_*` set to its `before_*`. That is the bytes on disk, the same substitution dry-run already makes (`runner.py:486-490`). The schema is unchanged. The health probe's R9e rule and the runbook `jq` stay correct and keep working. When `restored is False`, nothing changes.
- **Tests.**
  - Behavioural fake-systemctl tests replace the one-sided source-string checks.
  - Divergence tests inject **function-internal** failures, and failures inside the trap bodies.
  - Mutation acceptance: deleting any of the named assertion calls turns the suite red.
- **Docs.** The runbook's installer promise, the baseline lifecycle, and the `after_*` semantics in the receipt section.
- **Spec.**
  - ADDED in `scheduler-registry-refresh`: the refresh installer's failure-path and baseline contract, and the truth of the restored-provider receipt;
  - MODIFIED: the "refresh timer's liveness" requirement's probe-installer scenario (R15c).

**Not in this PR, stated honestly:**
- the node-22 live drill (install → enable → rollback, with before and after state receipts);
- the node-22 deploy.

Both **wait for the #1831 maintenance window**. #1831 forbids pulling onto the node-22 active checkout before that window. The PR and the archive receipt mark them as pending window evidence, and none of the verification is faked.

## Capabilities

### Modified Capabilities

- `scheduler-registry-refresh`: refresh-installer failure paths, the baseline lifecycle, the restored-provider receipt truth, and the probe installer's rollback baseline.

## Impact

- **Code:**
  - `scripts/install_node22_scheduler_file_provider_refresh.sh`;
  - `scripts/install_node22_refresh_timer_health.sh`;
  - `scripts/scheduler_refresh/runner.py` (`rollback_receipt_if_needed` only).
- **Tests:**
  - `tests/test_scheduler_refresh_deployment_contract.py`;
  - `tests/test_node22_refresh_timer_health_installer.py`;
  - `tests/test_scheduler_refresh_worker_mirror_transactions.py`;
  - new test files as needed (`.large-file-guard.json` maxLines 1000).
- **Docs:** `docs/runbooks/production-ops/file-provider-refresh.md`.
- **No change:**
  - `schemas/scheduler_file_provider_refresh_receipt.schema.json`;
  - `scripts/node22_refresh_timer_health.py`;
  - systemd unit files;
  - the Python refresh publisher lanes.
