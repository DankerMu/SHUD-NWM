# The provider refresh waits up to four hours for a running scheduler pass

Follow-up of #2749 (PR #2778), ordered by the owner on 2026-10-08. Fixture level: **expanded**.
Risk packs: **node-22 production unit / wrapper change**, **Ordering fail-closed**.

## Why

PR #2778 made the refresh wait for a running scheduler pass, at most 5400 s. On the day of its deployment
(node-22, 2026-10-08) one pass ran 126 minutes (04:03:17Z - 06:09:32Z): the first refresh start waited 5400 s
and refused (exit 3, nothing refreshed); the second start met the end of the pass and published. The stall
probe's notes record pass intervals up to 193.9 minutes. With the 5400 s bound a daily refresh that meets
such a pass is lost for that day. Receipt:
`openspec/changes/archive/2026-10-08-refresh-waits-for-scheduler-pass/evidence/node22-deploy-receipt.md`.

## What changes

1. `scripts/scheduler_file_provider_refresh_once.sh`: `scheduler_wait_bound_seconds=14400` (was 5400).
   Nothing else in the wait changes.
2. `infra/systemd/nhms-scheduler-file-provider-refresh.service`: `TimeoutStartSec=21600` (was 7200): the wait
   bound plus the unchanged 7200 s the refresh itself had before. The unit's comment says so.
3. `scripts/model_succession/model.py`: `REFRESH_START_TIMEOUT_SECONDS = 21900` (was 7500) and its comment:
   the blocking start is still given 300 s more than the unit's start timeout, so the tool never gives up on
   a start the unit is still allowed to finish. The tool starts the refresh only after it has seen the
   scheduler service not running, so its own starts still do not wait.
4. Timer-health probe: `DEFAULT_STOPPED_DWELL_HOURS` becomes 8 (was 6) in `scripts/node22_refresh_timer_health.py`;
   nothing else in the probe changes. The dwell tolerates the manual-publisher window (stop the timer, wait
   for a refresh in flight to end, publish, start the timer; `file-provider-refresh.md:446-452`). That window
   is as long as the refresh oneshot's start timeout plus the publish. It was justified as "three times the
   start timeout (2 h)"; it becomes "the start timeout (6 h) plus two hours for the publish". Keeping 6 h
   would make a legal window report `timer_stopped`; 18 h (the old factor) would delay the detection of a
   stopped and forgotten timer by twelve hours. The probe grades only the timer's own state and the manifest
   age (`scripts/node22_refresh_timer_health.py:513-541`); a refresh service that is `activating` for hours is
   invisible to it.
   - `tests/test_node22_refresh_timer_health_history.py:633-650`: renamed; asserts
     `DEFAULT_STOPPED_DWELL_HOURS * 3600 == seconds + 2 * 3600`, `seconds == 6 * 3600`, and the new runbook
     sentences.
   - Tests that pin the literal 6 (`tests/test_node22_refresh_timer_health_thresholds_and_receipt.py:65`, `:342`,
     and scenario texts such as `tests/test_node22_refresh_timer_health_runbook_and_env.py:677` "inside the 6 h
     default dwell") follow the new default (no existing scenario has an idle time between 6 h and 8 h).
   - `docs/runbooks/production-ops/file-provider-refresh.md:773` (the defaults table row, asserted by
     `tests/test_node22_refresh_timer_health_runbook_and_env.py:162-181`) shows 8; `:786-790` rewritten: the legal window, the 8 h,
     and that a longer window reports `timer_stopped` until the timer is started again.
   - node-22 has no threshold drop-in for the probe (read 2026-10-08), so the new default takes effect there
     with the pull.
5. Tests and texts that carry the old numbers:
   - `tests/test_scheduler_refresh_deployment_contract.py` (:107, :119, :126, :529 and message texts with 5400);
     `tests/test_node22_model_succession_timer_and_refresh.py` (:463-464).
   - Runbooks: the 5400 s at `file-provider-refresh.md:369`, `recalibration-and-archive.md:408`,
     `service-bringup.md:535`; every statement of when a refresh can be in flight becomes one wording
     (02:15Z + 30 min jitter + 6 h, about 08:45Z): `file-provider-refresh.md:613-614` ("最晚约 04:45Z"),
     `:671` and `:851` ("02:15-04:15Z").
   - Comments: `scripts/install_node22_refresh_timer_health.sh:15`,
     `tests/test_node22_refresh_timer_health_installer.py:188` (same window);
     `scripts/select_ci_tests.py:5747`, `:5760-5761` ("three times it") and `tests/test_select_ci_tests.py:665`
     (comment text only; if editing them trips the selector's own guards, leave them and report).
6. Deployment on node-22 (owner ordered the change knowing it needs the installer cycle). Unlike #2778, the
   pull alone is not safe to leave: after it the wrapper waits up to 14400 s under an installed unit that
   still has `TimeoutStartSec=7200`, so a timer-fired refresh behind a long pass would be killed by systemd
   at 7200 s instead of refusing cleanly. The pull, `--rollback`, `--install`, a refresh started through the
   unit and `--enable` are therefore done in one session, outside 02:15Z - 08:45Z, never left overnight
   between the pull and `--install`. The runbook's deployment note (`file-provider-refresh.md:605-614`) is
   rewritten to say that wrapper and installed unit must be changed together. The timer is disarmed between
   `--rollback` and `--enable`; in that span the hourly probe reports `probe_failed` or `timer_not_enabled`
   (expected, as on 2026-10-08).

## Must preserve

- Every behaviour of the wait other than the bound; `Before=`; no `ExecCondition`; the env handling.
- The probe's code other than the one default, its other defaults and the field set of its receipts.
- The relation "tool start timeout = unit start timeout + 300 s".

## Out of scope

Shortening scheduler passes; a lock between pass and refresh; the installer; #2779.

## Evidence

- `tests/test_scheduler_refresh_deployment_contract.py`: with the real bound and poll, a fake `sleep` and a fake
  `systemctl` that prints `activating` for more than 5400 s of polls (at least 361 queries) and then
  `inactive`, the wrapper refreshes and exits with the runner's status (fails on the old bound); the bound
  test still passes with substituted small values; the contract test pins 14400 and `TimeoutStartSec=21600`; a new assertion pins
  `TimeoutStartSec >= scheduler_wait_bound_seconds + 7200` read from the two files.
- succession: the start timeout equals the unit's `TimeoutStartSec` (read from the unit file) plus 300.
- probe history test as in item 4; the other probe suites change only the literals listed there and are green.
- node-22: installer read-backs, `systemctl --user show -p TimeoutStartUSec` of the refresh service = 6h,
  one refresh through the unit `published`, timer armed, probe `ok` after `--enable`.
