# Tasks — stall-probe-scan-bound (#3120)

- [x] 1.1 `DEFAULT_MAX_ENTRIES_SCANNED = 65536` in `scripts/node22_scheduler_stall_health.py` with the comment above it updated (measured 6185 entries 2026-11-13, catch-up density, 90-day worst ≈22k; the bound still refuses a truncated listing).
- [x] 1.2 Test in `tests/test_node22_scheduler_stall_health.py`: with NO `NHMS_SCHEDULER_STALL_MAX_ENTRIES_SCANNED` override, an evidence root holding 4097+ non-pass filler entries plus a healthy pass window is graded (verdict is not `probe_failed`, no "more than" error); assert the default constant is 65536. Existing small-override over-bound tests stay green (fail-closed proof).
- [x] 1.3 Runbook `docs/runbooks/production-ops/stuck-detection.md` stall-probe-failed section: one sentence that the default is 65536 since #3120 and that a catch-up burst (not a dead retention job) caused the 2026-11 overrun.
- [ ] 2.1 Post-merge (operator): node-22 ff-pull, `systemctl --user start nhms-node22-scheduler-stall-health.timer`, one tick graded not `probe_failed`, no new alert mail.
