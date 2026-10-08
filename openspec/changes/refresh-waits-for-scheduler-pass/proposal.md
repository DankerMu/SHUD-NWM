# The provider refresh waits for a running scheduler pass instead of running beside it

Issue: #2749. Fixture level: **expanded**.
Risk packs: **node-22 production unit / wrapper change**, **Ordering fail-closed**.

## Why

`nhms-scheduler-file-provider-refresh.service` is meant not to run while a scheduler pass runs. Both guards
ask `systemctl --user is-active --quiet nhms-compute-scheduler.service`: the unit's `ExecCondition` and line
20 of `scripts/scheduler_file_provider_refresh_once.sh`. The scheduler service is `Type=oneshot`; during a
pass its state is `activating`, for which `is-active --quiet` exits 3, so neither guard ever holds.

node-22, read-only, 2026-10-08 (the journal held two days):

| day | scheduler pass | refresh started | ran beside the pass for |
|---|---|---|---|
| 10-07 | 10:12:01 - 10:19:42 | 10:15:21 | 4 min 21 s |
| 10-08 | 10:40:12 - 10:48:11 | 10:40:28 | 7 min 43 s |

Passes run back to back, so this happens on every daily refresh. No `SCHEDULER_REGISTRY_MIRROR_MISMATCH` was
found in the last 200,000 lines of the scheduler logs; earlier logs were not read.

The other direction already works: `Before=nhms-compute-scheduler.service` held the next pass until the
refresh had finished on both days (next pass started 10-07 10:25:07 and 10-08 10:51:05, the instants the
refresh finished).

A guard that merely recognised `activating` and skipped would make the daily refresh never run, because a pass
is nearly always in flight. The refresh therefore waits.

## What changes

1. `scripts/scheduler_file_provider_refresh_once.sh`: the `is-active --quiet` check is replaced by a wait.
   - State = stdout of `<systemctl> --user is-active nhms-compute-scheduler.service` (exit status ignored,
     as `scripts/model_succession/systemd.py::unit_state` does).
   - `inactive` or `failed`: proceed. `active`, `activating`, `deactivating`, `reloading`: running; wait.
     Anything else (empty output, the binary missing, another word): exit 3 with a message on stderr naming
     what was printed; nothing is refreshed.
   - While running: one line on stderr at the start of the wait and one per minute (state, seconds waited),
     poll every 15 s, and after 5400 s exit 3 with a message naming the state and the time waited.
   - The two state lists are written once in the wrapper with a comment naming
     `scripts/model_succession/systemd.py` (`NOT_RUNNING_STATES`, `RUNNING_STATES`); the wrapper does not
     import Python for this. The binary path, the bound and the poll interval are fixed assignments at the top
     of the wrapper (`/usr/bin/systemctl`, 5400, 15), not environment overrides.
   - The wait happens where the old check was: after the env-file regular-file test, before anything else.
2. `infra/systemd/nhms-scheduler-file-provider-refresh.service`: the `ExecCondition` line is removed. `Before=`
   and `TimeoutStartSec=7200` stay. A comment states that the wrapper waits for a running pass and that
   `Before=` holds the next pass while the refresh is starting.
3. `scripts/model_succession/scheduler.py:137-140`: the failure text for "start returned zero without a newer
   `latest.json`" no longer names a start condition. It keeps the words `Possible causes` and
   `was already running` (asserted at `tests/test_node22_model_succession_timer_and_refresh.py:443`) and names
   one cause: a refresh was already running and the start returned with that one. No behaviour change.
4. `tests/model_succession_helpers.py`: the fake refresh unit drops the `!= "active"` branch (:123-124) and
   the "Like the real unit, it skips" sentences (:18, :123); a start in `mode: run` always writes the receipt.
   `mode: skip` stays (it models "zero exit, no new receipt" for `REFRESH_FAILURES`).
   `test_the_refresh_unit_skips_while_the_service_is_active_like_the_real_one`
   (`tests/test_node22_model_succession_timer_and_refresh.py:475-484`) is deleted: the real unit has no such
   branch any more and the fake cannot model a wait; the wait is tested on the real wrapper (Evidence).
5. Runbooks: `docs/runbooks/production-ops/recalibration-and-archive.md` (5.7.1 "触发手动 refresh 的坑"),
   `file-provider-refresh.md` (manual trigger step), `service-bringup.md` (hop 4): a manual
   `systemctl --user start` of the refresh now blocks until the running pass has ended and the refresh has
   finished, exits non-zero when the wait runs out, and the criterion "a newer `latest.json started_at`" stays.
   The file-provider runbook also gets the deployment note of item 6.
6. Deployment on node-22 is two separate steps, both only on the owner's go-ahead:
   - `git pull --ff-only` makes the wrapper live (the unit executes it from the checkout). The installed unit
     still carries the old `ExecCondition`; it is inert (it passes for `activating`, `inactive` and `failed`).
   - Replacing the installed unit file needs the installer cycle `--rollback`, `--install`, `--enable`
     outside the 02:15-04:15Z window. The behaviour does not need it, but operations do: after the pull the
     installed unit differs from the repository's, and the installer's `--enable` compares the two (`cmp -s`,
     `scripts/install_node22_scheduler_file_provider_refresh.sh:355-358`) and refuses until `--install` has
     run again. The lane is armed now and stays armed; the next installer action of any kind must start with
     this cycle. The runbook says so.

## Must preserve

- `Before=nhms-compute-scheduler.service`, `TimeoutStartSec=7200`, the DB-free environment handling, the
  env-file allowlist parsing and every existing wrapper refusal and its exit status.
- `REFRESH_START_TIMEOUT_SECONDS` of the succession tool (7500): the tool starts the refresh after its own
  wait for the pass, so the new wait is zero there.
- No new environment variable is read by the wrapper.
- The installer and the two health probes are not changed.

## Out of scope

Locking between the scheduler pass and the refresh inside Python; the manual publisher CLI's concurrency
(#1104 prohibition stays); changing `TimeoutStartSec`; a pass longer than 90 minutes (the refresh of that day
fails visibly and the timer-health probe covers a stale manifest).

## Evidence

Local, all in `tests/test_scheduler_refresh_deployment_contract.py` (already routed for the wrapper and the
unit; no new test file). The wrapper's `systemctl` path, bound and poll interval are single literal
assignment lines at its top. `_write_wrapper_execution_fixture` (:480-483) substitutes the path with a fake
`systemctl` that prints `inactive` by default, so the five existing execution tests keep reaching the runner
on hosts without `/usr/bin/systemctl` or a user bus; every substitution asserts that it matched. The wait
code runs under `/bin/bash` 3.2 (macOS) as the existing tests do.

- `inactive` and `failed`: the runner is executed, no wait line.
- `activating` twice then `inactive`: the runner is executed after the third query; a wait line is on stderr.
- `active`, `deactivating`, `reloading` each count as running.
- empty output, an unknown word, a missing binary: exit 3, the runner is not executed, stderr names the output.
- still `activating` when the bound runs out (bound and poll shortened by text substitution in the test copy,
  as the existing wrapper tests substitute `repo=`): exit 3, runner not executed, stderr names state and time.
- the wrapper's two state lists equal `NOT_RUNNING_STATES` and `RUNNING_STATES` of
  `scripts/model_succession/systemd.py`.
- the unit file has no `ExecCondition`, keeps `Before=` and `TimeoutStartSec=7200`.
- the succession suites pass.

node-22 (after the owner's go-ahead for the pull): one timer-fired refresh observed in the journal and the
wrapper's stderr: refresh starts while a pass is `activating`, logs the wait, the pass finishes, the refresh
runs and publishes (`latest.json` newer, outcome `published`), the next pass starts after the refresh
finished.
