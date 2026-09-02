## Risk Triage

```text
Issue type: data-loss guard (bugfix) + regression test
Project profile: NHMS (openspec/project-profile.md)
Blast radius: medium (one library write point used by the CLI and every autopipe tick)
Fixture level: expanded
Repair intensity: high
Upstream suggested level: absent (hand-written issues; expanded forced by overwrite/data-loss trigger)
Why:
- overwrite of production display coverage rows (publish/delete/overwrite trigger)
- semantic change from idempotent recompute to conditional refusal needs spec rows + red-proof
OpenSpec change: display-coverage-refresh-guard
Evidence floor:
- uv run ruff check .
- uv run pytest -q tests/test_display_coverage_parallel.py tests/test_display_coverage_refresh.py tests/test_node27_connection_attribution.py tests/test_node27_connection_attribution_delegated.py + the CLI test file
- node-27: uv run pytest -q tests/test_river_ts_read_path_surrogate_keys_integration.py (real DB)
- openspec validate display-coverage-refresh-guard --strict --no-interactive
```

## Risk Packs

| Pack | 选择 | 理由 |
|---|---|---|
| Public API / CLI / script entry | selected | new `--force`; refusal exit 3 + structured stderr line on `--run-id`; library kwarg; summary dict gains a key |
| Config / project setup | not selected | no config |
| File IO / path safety / overwrite | selected | the whole issue is an overwrite guard |
| Schema / columns / units / field names | not selected | no schema change; `segment_count` semantics unchanged |
| Auth / permissions / secrets | not selected | same role, same DSN handling |
| Concurrency / shared state / ordering | selected | single-statement atomic skip, no read-modify-write window; two worker paths |
| Resource limits / large input / discovery | selected | refused runs stay stale and are rescanned by cron every tick (D4): cost measured on node-27 and bounded by the stale-legacy count |
| Legacy compatibility / examples | selected | legacy NULL-key cohort is the protected population; cron loop unchanged |
| Error handling / rollback / partial outputs | selected | refusal vs failure counters; no partial write |
| Release / packaging / dependency compatibility | not selected | none |
| Documentation / migration notes | selected | runbook prohibition + CLI docstring |
| 已发布 NHMS 制品 / display 身份 | selected | coverage feeds `/api/v1/layers` |
| PostGIS / TimescaleDB 域行为 | not selected | scan SQL unchanged |
| 其余 domain packs | not selected | not touched |

## Tasks

- [x] T1 (#1446) `_REFRESH_SQL` conditional `DO UPDATE … WHERE` with a `force` parameter at `_refresh`; `_refresh` returns `RefreshOutcome(refreshed, refused)` with `refused = candidates − returned` in single-run mode, `([], [])` on the non-candidate early return, and `refused=[]` by contract in the all-runs form (protection only, no classification); `refresh_run_display_coverage` raises `DisplayCoverageRefreshRefused` (existing count fetched for the message, rollback first) only when its run is in `refused`; non-candidate run with an old populated row still returns `False`; `tests/test_display_coverage_refresh.py:79,82,93-101,104,110` updated.
- [x] T2 (#1446) `refresh_all_run_display_coverage`: `force` passthrough; `refresh_one` maps `RefreshOutcome.refused` to a `"refused"` outcome counted under `refused` (nothing written, committed, closed), a non-candidate stays `"no-row"`/`skipped`; `tests/test_display_coverage_parallel.py:54` rewritten to the four-key dict (no other consumer breaks — verified fact, see design Context); red-proof: a legacy populated run in the batch -> `refused: 1` (fails before the change).
- [x] T3 (#1446) CLI `--force`; `--run-id` refusal -> exit 3 + `DISPLAY_COVERAGE_REFRESH_REFUSED run_id=… existing_segment_count=… advice=…` on stderr (test); `--all` report prints `refused`; docstring; runbook :126-128 rewritten as guard-backed with the D4 cost and the two remedies.
- [x] T4 (#1446) Real-DB tests (red first): legacy populated row refused + unchanged; `force` zeroes; first refresh and existing-zero still write 0; pin test at :739 rewritten to guard semantics.
- [x] T5 (#1725) Parametrized isolation test (workers 1/2, N=3, second run fails), dict + commit/rollback/close assertions, hoist-point coverage; red-proof recorded.
- [ ] T6 node-27 (queued session): run the integration suite from a detached worktree against the production DB with the integration DSN; capture pass output as the receipt; run the D4 count query (stale AND key-NULL AND populated runs) and record the number in the PR.

## Non-goals (explicit)

No backfill/drain of legacy keys, no #1342 work, no change to the station-side scan SQL (station columns of a refused row are frozen with the row, by D1), no change to `scripts/node27_autopipe_cron.sh`.
