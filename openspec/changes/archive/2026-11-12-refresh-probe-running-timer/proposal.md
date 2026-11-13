# refresh-probe-running-timer (#3132)

## Why

`scripts/node22_refresh_timer_health.py` grades `timer_not_scheduled` whenever the active refresh timer has no
parseable `NextElapseUSecRealtime`. systemd leaves that field empty while the timer's triggered unit is still active
(`SubState=running`, `NextElapseUSecMonotonic=infinity`, measured on node-22 2026-11-13). The daily 02:15Z
file-provider refresh runs ~2.7 h, so the hourly probe false-alarms ~3 times a day (11-12 03/04/05Z, 11-13 03Z) and,
since #3127, mails each time.

## What

- Step 5 of `grade` (running-but-unscheduled): skip the next-elapse requirement when the timer `SubState` is
  `running`. Everything else is unchanged: precedence order, `timer_stopped` / `timer_not_enabled` /
  `manifest_expired` / `manifest_stale` / `manifest_unavailable` still grade a running timer.
- Bounded blind spot: the refresh service has `TimeoutStartSec=6h` and its own `OnFailure=`; a refresh that never
  publishes is still caught by manifest age (steps 2/6).
- Tests in `tests/test_node22_refresh_timer_health_verdicts.py`: running + empty next elapse + fresh manifest -> `ok`;
  `waiting` + empty next elapse -> still `timer_not_scheduled`; running + stale manifest -> `manifest_stale`;
  running + not enabled -> `timer_not_enabled`.
- Runbook note where `timer_not_scheduled` is described (the probe's runbook section).

Fixture level: compact. Blast radius: a wrong predicate either keeps the false alarm (tests) or hides a real
unscheduled timer (the `waiting` test). Risk packs: Error handling (verdict precedence).
Evidence floor: `uv run pytest -q tests/test_node22_refresh_timer_health_verdicts.py
tests/test_node22_refresh_timer_health_thresholds_and_receipt.py tests/test_node22_refresh_timer_health_history.py
tests/test_node22_refresh_timer_health_runbook_and_env.py tests/test_select_ci_tests.py` (node-27 + local);
`uv run ruff check .`; `openspec validate refresh-probe-running-timer --strict --no-interactive`.

design.md omitted (compact). Owner authorization 2026-11-13 ("问题都授权你按最佳方案处理").
