# The gateway rollout and rollback refuse while a scheduler pass is running

Issue: #2779. Fixture level: **standard**. Risk pack: **Ordering fail-closed** (runbook procedure for node-22
production credentials; no code path of a service changes).

## Why

Step 0 of the gateway rollout and of the rollback in `docs/runbooks/production-ops/gateway-and-services.md`
(:238, :521) guards "a scheduler pass is still running: print the instruction and exit before backup /
overwrite" with `if systemctl --user is-active --quiet nhms-compute-scheduler.service; then`. The scheduler
service is `Type=oneshot`: during a pass it is `activating`, and `is-active --quiet` exits 3, so the branch
never runs in the one situation it exists for. `tests/test_slurm_gateway_deployment_contract.py:524-526` pins
that line (`LIVE_PASS_FAILCLOSED`, asserted at :627 and :755). Same misreading as #2749.

node-22, read-only, 2026-10-08 08:03Z, during a pass: `is-active` printed `activating` and exited 3;
`is-active --quiet` exited 3; `show` gave `ActiveState=activating SubState=start ConditionResult=yes`.

What happens today after the dead guard (not tested on node-22, no rollout was run): the block goes on to
`systemctl --user start nhms-compute-scheduler.service` (:253, :531) on a unit that is already starting. On
2026-10-08 a `systemctl --user start` of the refresh oneshot blocked until that unit's run had ended; if the
same holds here the block waits for the pass, then `test ... = inactive` passes and the next line
`test "$(... -p ConditionResult --value)" = no` stops the block under `set -e`, because the reading during a
pass is `yes` and a start that joins a running job does not re-evaluate conditions. So the backup/overwrite
is probably not reached, but through a silent `test` failure that looks like "the fence is not live", after a
wait the block's own comment rules out ("No sleep loop ... no waiting/retry").

## What changes

1. Both guards read the printed state and classify it, as `scripts/model_succession/systemd.py:21-22`
   (`NOT_RUNNING_STATES`, `RUNNING_STATES`) and the refresh wrapper do:
   - capture with `|| true` (the block runs under `set -euo pipefail`; `is-active` exits 3 for `inactive`);
   - `inactive`: continue;
   - `failed`: continue, after one stderr line: the last pass failed; the fence probe below requires
     `inactive`; if it stops there, run `systemctl --user reset-failed nhms-compute-scheduler.service` and
     re-run the step. The block itself does not reset the unit. (Today a `failed` service also passes the
     guard and, as far as systemd semantics go, stops silently at `test ... = inactive` (:254, :532) because a
     condition-skipped start leaves the state `failed`; not tested on node-22. The line makes that stop
     explicable; the repository's precedent for requiring `reset-failed` is `file-provider-refresh.md:669-672`.)
   - `active`, `activating`, `deactivating`, `reloading`: the two existing instruction lines, with the state
     in the first one, then `exit 1`;
   - anything else (nothing printed, another word): a line naming what was printed and that the state is
     unknown, then `exit 1`.
   No waiting, no retry, the service is never stopped or killed. The comment above the rollout guard says
   why the printed state is read (oneshot, `activating`).
2. `tests/test_slurm_gateway_deployment_contract.py` (952 lines; the large-file hook refuses more than 1000
   and the file is not exempt, so the additions stay within 48 lines; no new test file):
   - `LIVE_PASS_FAILCLOSED` pins the capture line (after `_executable_lines`' indentation stripping,
     :156-164); the rollout and rollback order assertions (:627, :755) keep it before the `start` probes and
     before `BACKUP_ROOT=` / the `BACKUP_POINTER` restore; the comment at :649 ("no `--quiet` swallowing, no
     `|| true`") is corrected.
   - Neither block contains `is-active --quiet` (the needle is written without the unit name, so the grep of
     the Evidence does not hit the test).
   - The guard is extracted from each block, from the capture line to its `esac`; a missing anchor raises
     (as :553-558 do). It is run under `/bin/bash` with `set -euo pipefail` prepended and a fake `systemctl`
     first on `PATH` that prints the given state and exits 0 only for `active`, else 3 (so a missing
     `|| true` is caught). Parametrised over the six known states taken from the Python constants, an empty
     answer and an unknown word: `inactive` and `failed` fall through (exit 0; `failed` with its stderr line),
     the four running states exit 1 with "let it finish naturally", empty and unknown exit 1 naming the
     answer. This test fails on the old runbook text.
   - Existing mechanisms the new runbook text must respect: `_rollout_only_blocks` (:199-201) cuts at the
     first capitalised `Rollback`, so the rollout guard's comments and messages must not contain that word;
     `_assert_no_service_stop_kill` (:566-571) rejects an executable line that has the unit name together
     with `stop ` or `kill `, so the messages avoid those words.
3. List A of the issue: only `scripts/select_ci_tests.py:5749` (`:3599-3611`) and `:5751` (`:3603-3604,
   :3633`) are still stale: the referenced test file has 824 lines and the function is
   `test_systemd_refresh_contract_is_db_free_daily_and_scheduler_independent`
   (`tests/test_scheduler_refresh_deployment_contract.py:98`). The comment names the function instead of
   line numbers. `tests/test_select_ci_tests.py:663-667` is already correct and is not touched. Comment text
   only; if a selector guard goes red, revert and report.
4. List B of the issue, `docs/runbooks/production-ops/file-provider-refresh.md`, the four direct wrapper
   calls:
   - :90 `--dry-run`: stays a direct call (it publishes nothing; it does wait for a running pass like any
     wrapper run, so no sentence may say it does not wait).
   - :94 the real run of the first-install section, and :676 drill step 6: replaced by
     `systemctl --user start nhms-scheduler-file-provider-refresh.service` (line for line / in-line), with
     the note that a failed run leaves the unit `failed` and the installer's entry gate then needs
     `reset-failed` (:671-672). :682 (step 7 refers to step 6) stays consistent. The alternative "stop the
     scheduler timer first" is not used: it contradicts :48 and the drill's premise (:666) and adds lines.
   - :147 `--recover-emergency`: stays a direct call (it rebuilds a receipt and republishes no provider);
     one clause says so if it fits without growing the file.
   - The file has exactly 1000 lines, the hook refuses 1001 and the file is not exempt: the edit is net zero
     in lines (markdownlint line length 300, code blocks exempt).

## Must preserve

- Every other line and the order of the rollout and rollback blocks; the existing instruction texts ("let it
  finish naturally", "re-run this ... step once nhms-compute-scheduler.service is inactive") and `exit 1`.
- The other contract assertions of the gateway test.
- No script or unit changes; nothing is run on node-22.

## Out of scope

The fence mechanism; `known-issues-state-index.md:608` and `canonical-precip-mirror-backfill.md:84` (states
read by a person); executing a rollout.

## Evidence

- `uv run pytest -q tests/test_slurm_gateway_deployment_contract.py` and the refresh runbook/contract suites
  that read the edited runbooks; the extracted-guard test shown red on the old runbook text.
- `git grep -n 'is-active --quiet nhms-compute-scheduler.service'` outside `openspec/` : no hit.
- node-22 read-only reading above (already taken).
