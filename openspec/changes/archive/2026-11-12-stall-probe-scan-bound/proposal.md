# stall-probe-scan-bound (#3120)

## Why

`nhms-node22-scheduler-stall-health.service` has returned `verdict=probe_failed` on every 15-minute tick since the
evidence root passed 4096 entries (6185 measured 2026-11-13; 4695 dated 2026-10, 1475 dated 2026-11). The evidence
retention job is alive (last run success, 90-day window) and every entry is inside that window: the surplus is the
catch-up pass density (≈150-245 entries/day in October/early November vs ≈15-25/day in steady state). The probe is
blind and, since #3127 wired OnFailure, mails every tick (42 mails in 24 h; the timer is stopped as a stop-gap).

Owner authorization 2026-11-13: fix with the best option. Chosen: direction 1 of the issue (raise the bound), because
a full listing of the root is cheap (`ls -f` of 6185 entries on the node-22 NFS mount: 11 ms) and the bound's
purpose — refusing to grade a *truncated* listing — is preserved unchanged. Directions 2 (shorter evidence retention:
loses audit evidence) and 3 (name-directed lookup: larger change to a load-bearing ordering argument) rejected.

## What

- `scripts/node22_scheduler_stall_health.py`: `DEFAULT_MAX_ENTRIES_SCANNED` 4096 -> 65536 (≈3x the worst measured
  90-day volume, 245/day x 90 ≈ 22k); comment updated with the 2026-11-13 measurement. Reaching the bound stays an
  error (`probe_failed`), never a silent truncation; the env override and the `>= SCAN_LIMIT` check are unchanged.
- Test: an evidence root with more than 4096 (old bound) entries and the default config is graded (not
  `probe_failed`); the existing over-bound test (explicit small override) keeps proving fail-closed.
- `docs/runbooks/production-ops/stuck-detection.md`: note the new default and the catch-up cause.

Non-goals: evidence retention policy; probe grading logic; unit files (no `Environment=` change, so no manifest drift).

Fixture level: compact. Blast radius: a too-high bound only costs listing time (bounded, measured); a regression would
re-blind the probe (test-guarded). Risk packs: Config (default value), Error handling (fail-closed bound preserved).
Evidence floor: `uv run pytest -q tests/test_node22_scheduler_stall_health.py tests/test_select_ci_tests.py`
(node-27 oracle + local); `uv run ruff check .`; `openspec validate stall-probe-scan-bound --strict --no-interactive`.

design.md omitted (compact).
