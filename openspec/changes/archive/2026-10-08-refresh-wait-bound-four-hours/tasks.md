# Tasks

## 1. Implementation

- [x] 1.1 Wrapper bound 14400; unit `TimeoutStartSec=21600` and comment.
- [x] 1.2 `REFRESH_START_TIMEOUT_SECONDS = 21900` and comment.
- [x] 1.3 Tests: contract (numbers, relation between the two files, a wait longer than 5400 s on the real
      bound), succession (unit timeout + 300), probe default 8 with the history test renamed and re-derived.
- [x] 1.4 Runbooks and comments: wait bound, one wording for the in-flight window (to about 08:45Z) in all
      listed places, probe dwell default row and justification, deployment note (wrapper and unit together).

## 2. Evidence Floor

- [x] 2.1 Local: `uv run ruff check .`; `tests/test_scheduler_refresh_deployment_contract.py`;
      `tests/test_node22_model_succession*.py`; `tests/test_node22_refresh_timer_health*.py`;
      `tests/test_scheduler_refresh_installer*.py`; selector meta-guards after staging;
      `openspec validate refresh-wait-bound-four-hours --strict --no-interactive`.
- [x] 2.2 CI green on the PR.
- [x] 2.3 node-22, one session outside 02:15Z - 08:45Z, not left between the pull and `--install`:
      `git status --porcelain`, `git pull --ff-only`; installer `--rollback`, `--install`; a
      refresh started through the unit ends `published`; `--enable`; read back the four units,
      `TimeoutStartUSec`, the next elapse, the probe verdict after `--enable` (non-`ok` verdicts while the
      timer is disarmed are expected). Only `/scratch/frd_muziyao/NWM/.venv/bin/python`
      or `uv run --no-sync` for any Python.

Deviation: no node-27 path is touched. A wait longer than 5400 s is not expected to be observed at 2.3; it
is covered locally on the real bound with a fake `sleep` and a fake `systemctl`.

Deviation at 2.3 (2026-10-08): the session ran 07:49Z - 08:02Z, inside the stated 02:15Z - 08:45Z span. Before
it started the refresh service was `inactive` and the timer's next elapse was 2026-10-09 10:35:19 CST, so no
refresh was or could be in flight.
