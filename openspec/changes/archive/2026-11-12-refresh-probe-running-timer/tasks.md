# Tasks — refresh-probe-running-timer (#3132)

- [x] 1.1 `grade` step 5 in `scripts/node22_refresh_timer_health.py`: when `properties.get("SubState") == "running"`, do not require a next elapse (neither the empty/unparseable branch nor the too-far branch fires); update the step comment and the module docstring if it states the old rule.
- [x] 1.2 Tests (`tests/test_node22_refresh_timer_health_verdicts.py`, reuse its helpers):
  running + empty next elapse + fresh manifest -> `ok`, exit 0; `waiting` + empty -> `timer_not_scheduled`; running + stale manifest -> `manifest_stale`; running + `UnitFileState=disabled` -> `timer_not_enabled`; running + unparseable next elapse -> not `timer_not_scheduled`.
- [x] 1.3 Runbook: the section describing `timer_not_scheduled` for this probe gains one sentence on `SubState=running` (find it via `grep -rn timer_not_scheduled docs/runbooks`). No line may start with `#` + digits.
- [x] 2.1 Post-merge (operator): node-22 ff-pull; during the next refresh window a tick grades `ok`.

  Receipt 2026-11-13T03:41Z: node-22 ff-pull to 9c853c931; during the live refresh run (timer `SubState=running`) a manual tick graded `verdict=ok`; `nhms-node22-refresh-timer-health.timer` restarted; no failed nhms unit.
