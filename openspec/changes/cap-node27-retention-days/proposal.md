## Why

Neither retention period in `scripts/node27_raw_retention.py` has an upper bound (#3089, deferred from #3088 review C2):

- `NODE27_RAW_RETENTION_FORCING_DAYS` is parsed strictly with a floor of 14 (`scripts/node27_raw_retention_lanes.py:_forcing_retention_days`, :97-121) but no ceiling;
- `NODE27_RAW_RETENTION_DAYS` (and `--retention-days`) goes through `_env_int` (`:53-61`) and only rejects values ≤ 0 (`node27_raw_retention.py:232-234`).

A huge value such as `1000000` passes preflight. `reference_time - timedelta(days=...)` then raises `OverflowError` (`node27_raw_retention.py:570-571`, `:747-748`): a traceback, rc ≠ 0, the `OnFailure=` alert, and **no summary**. Nothing is deleted, but the operator gets no receipt. The #3088 runbook tells operators to raise `NODE27_RAW_RETENTION_FORCING_DAYS` to pause the forcing lane (`docs/runbooks/production-ops/artifacts-and-storage.md:580-583`), which invites exactly such a value.

## What Changes

- **Ceiling constant** `RETENTION_DAYS_CEILING = 3650` in `scripts/node27_raw_retention_lanes.py`, next to `FORCING_RETENTION_FLOOR_DAYS`.
- **Forcing period:** `_forcing_retention_days` returns a `forcing_retention_days` blocker with `reason: "above_ceiling"`, `value`, `floor` and `ceiling` for a digit string above the ceiling. Only this new blocker carries `ceiling`; the existing `not_an_integer` / `must_be_positive` / `below_floor` blockers are unchanged.
- **Raw period:** preflight appends `{"field": "retention_days", "reason": "above_ceiling", "value": <int>, "ceiling": 3650}` when the resolved `retention_days` (CLI or env) exceeds the ceiling. The existing lenient `_env_int` parsing and the `must_be_positive` check are unchanged.
- **Behaviour of a blocked run is unchanged:** `preflight_blocked` summary written when a summary path is configured, rc 2, nothing deleted (`node27_raw_retention.py:874-929`).
- **Runbook:** the rollback section says the forcing pause value must stay ≤ 3650 (recommend 3650), and the preflight bullet lists `above_ceiling`.
- Values within `[floor or 1, 3650]` behave exactly as before.

## Triage

```text
Issue type: bugfix
Fixture level: standard
Upstream suggested level: absent
Blast radius: node-27 retention preflight only; a too-low ceiling would block a legitimate config (3650 is 10 years, far above any operational value); no deletion-path change
Selected risk packs: Config (env parsing); Error handling (preflight blocker instead of traceback); Documentation (runbook)
Evidence floor: uv run ruff check . ; uv run pytest -q tests/test_node27_raw_retention.py
```

## Impact

- **Code:** `scripts/node27_raw_retention_lanes.py`, `scripts/node27_raw_retention.py` (stays under 1000 lines).
- **Tests:** `tests/test_node27_raw_retention.py`.
- **Docs:** `docs/runbooks/production-ops/artifacts-and-storage.md`; `infra/env/node27-raw-retention.example` (the forcing blocker reason list gains `above_ceiling` / 3650).
- **Spec:** `node27-raw-retention`.
