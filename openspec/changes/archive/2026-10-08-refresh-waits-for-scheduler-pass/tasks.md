# Tasks

## 1. Implementation

- [x] 1.1 Wrapper: wait for a running scheduler service (state lists, bound, poll, progress lines, fail-closed
      on an unknown state). bash 3.2 compatible (no `declare -A`, `${x,,}`, `mapfile`); every refusal is an
      explicit `if`/`case` with a message and `exit 3`, not a bare `[[ ]]` under `set -e`.
- [x] 1.2 Unit file: remove `ExecCondition`, add the comment (no comment line starts with `TimeoutStartSec=`).
- [x] 1.3 Succession failure text; fake refresh unit in `tests/model_succession_helpers.py`.
- [x] 1.4 Tests of the wait against a fake `systemctl`; state-list equality; unit-file contract.
- [x] 1.5 Runbooks (three places) and the deployment note.

## 2. Evidence Floor

- [x] 2.1 Local: `uv run ruff check .`; the wrapper/contract tests; the model succession suites; selector
      meta-guards after staging; `openspec validate refresh-waits-for-scheduler-pass --strict --no-interactive`.
- [x] 2.2 CI green on the PR.
- [x] 2.3 node-22, after the owner's go-ahead. Before the pull, prove that `systemctl --user is-active` answers
      from inside a user unit (the old guard treated a failed call as "not active", so its history proves
      nothing): `systemd-run --user --wait --pipe --collect /usr/bin/systemctl --user is-active
      nhms-compute-scheduler.service` must print one of the six known states. Then `git status --porcelain`,
      `git pull --ff-only`; then one
      timer-fired refresh read from `journalctl --user -u nhms-scheduler-file-provider-refresh.service` and
      `-u nhms-compute-scheduler.service` showing wait, pass end, refresh, next pass; `latest.json` outcome
      `published`. During the wait: `systemctl --user list-jobs` and `is-active` of the scheduler service
      once the running pass has ended (expected: its start job `waiting`, state `inactive`). Only
      `/scratch/frd_muziyao/NWM/.venv/bin/python` or `uv run --no-sync` for any Python.
- [x] 2.4 node-22, on a separate go-ahead (not needed for the behaviour; required before any later installer
      action): installer `--rollback`, `--install`, `--enable`
      outside 02:15-04:15Z to replace the installed unit file; read back that it has no `ExecCondition`.

Deviation: no node-27 path is touched; no node-27 receipt applies. Until 2.3 the ordering claim of design.md
rests on systemd semantics plus two observed days of the `ExecStart` phase, not on an observed wait.

Deviation at 2.3 (2026-10-08): the observed refresh was started by hand with `systemctl --user start` (the
installer cycle needs a fresh receipt next to `--enable`), not fired by the timer. It ran through the same
unit and wrapper. The first timer-fired refresh after the deployment (2026-10-09, about 02:35Z) was not
observed in this change.
