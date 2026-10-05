# Tasks

Risk packs:
- Parser / reader / writer: selected - #2687 changes what the output parser accepts -> 2.1-2.4.
- Error handling / retry policy: selected - #2690 -> 3.1-3.4.
- Legacy compatibility: selected - existing parser fixtures and the transient re-parse shapes -> 2.4, 3.3.
- Documentation / migration notes: selected - #2501 -> 4.1-4.3.
- Schema / columns: not selected - `HydroRunContext.end_time` is an in-process field; no DB or file schema changes.
- Concurrency / shared state: not selected - no new state; the #2690 decline uses the existing decline record.
- Public API / CLI, Auth, Config: not selected - no such surface changes (the parser CLI's exit codes are unchanged).

## 1. (removed) #2727

Withdrawn from this change; see proposal.md "Removed from this change".

## 2. #2687 parser run window

- [x] 2.1 `HydroRunContext.end_time` filled on the DB path and the DB-free path; DB-free without `end_time` fails closed.
- [x] 2.2 Red-before tests: forecast row with `valid_time < cycle_time`; with `valid_time > end_time`; a negative
      relative-minutes token; a negative relative-days token; an absolute unix-minutes file starting less than
      one day before `cycle_time`. Each: named error code, run failed, zero rows written.
- [x] 2.3 Boundary tests: `valid_time == cycle_time` and `valid_time == end_time` pass.
- [x] 2.4 Preserve: analysis runs unchanged (regression test), including a DB-free analysis manifest without
      `end_time`; all existing parser fixtures still parse once the DB-free forecast manifest fixture carries
      `end_time`; every `HydroRunContext` constructor updated, `tests/test_e2e.py` included.

## 3. #2690 published compressed-chunk decline

- [x] 3.1 Red-before test: published run, re-parse fails with `OUTPUT_PARSE_COMPRESSED_CHUNK_BLOCKED` -> today no
      decline, tick rc=1. The existing pin in `tests/test_node27_autopipeline_published_reparse.py`
      (`test_only_a_bare_parser_code_is_deterministic`, `BLOCKED -> None`) flips; the `GUARD_FAILED -> None` pin stays.
- [x] 3.2 Green: a `PUBLISHED_REPARSE_FAILED` decline whose detail starts with the code; the run stays
      `published`; the tick does not return rc=1 for it; the residency observer reports it.
- [x] 3.4 Runbook: `docs/runbooks/production-ops/parse-failure-residency-alert.md` updated to the new behavior, and one line in `docs/runbooks/tier-node27-timeseries-storage.md` (the compressed-chunk decline
      paragraph) covering the parse-side code and its exit (`--force` or deleting the decline row); the
      `_deterministic_parse_error_code` docstring corrected.
- [x] 3.3 Preserve: `OUTPUT_PARSE_COMPRESSED_CHUNK_GUARD_FAILED` and the other transient shapes still retry;
      non-published runs unchanged.

## 4. #2501 text and test hygiene

- [x] 4.1 `fix-narrow-segment-read-index-applicability/design.md`: dated revision note on the cross product and on
      criterion 1's legacy passage; the stale `river_ts_render.py` line reference fixed or made line-independent.
- [x] 4.2 Same change's `tasks.md`: 1.3 and the legacy part of 1.4 marked superseded; receipts untouched.
- [x] 4.3 `tests/river_ts_plan_criteria.py`: `BRANCH_IDENTITY_COLUMNS` only `narrow`; docstring fixed; the three
      legacy-only tests and `_legacy*` fixtures removed from `tests/test_river_ts_plan_criteria.py`.
      `grep -rn '"legacy"' tests/river_ts_plan_criteria.py` prints nothing; `RECORDED_ROW_DIGESTS` byte-identical.

## 5. Verification

- [x] 5.1 `uv run ruff check .`
- [x] 5.3 `uv run pytest -q tests/test_output_parser.py tests/test_output_parser_dual_write.py tests/test_analysis_pipeline.py`
- [x] 5.4 `uv run pytest -q tests/test_node27_autopipeline_published_reparse.py tests/test_node27_parse_failure_residency_alert.py`
- [x] 5.5 `uv run pytest -q tests/test_river_ts_plan_criteria.py tests/test_river_ts_stats_harness_offline.py`
- [x] 5.6 `openspec validate parser-run-window-and-published-chunk-decline --strict --no-interactive` and
      `openspec validate fix-narrow-segment-read-index-applicability --strict --no-interactive`
- [ ] 5.7 node-27, no database: the test files of 5.3-5.5 (`TMPDIR=/home/nwm/tmp`, `uv run --no-sync`).

## Evidence Floor deviation

- #2690 acceptance asks for a node-27 real-database regression. node-27 cannot create test databases or run
  migrations while its RAID link is unstable; the CI real-db job covers any integration test, and the node-27
  real-database run is deferred until the link is repaired.
