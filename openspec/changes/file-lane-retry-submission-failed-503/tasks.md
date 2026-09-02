# Tasks

Fixture level: compact · repair intensity: standard · issue: #1945. Line cites
against `origin/master` `af394fd1`; symbol names are authoritative.

## 0. Evidence Floor

Oracles: local pytest (macOS) for red/green; node-27 for the issue's
Verification command; CI status read at every head (recorded per round).

- [x] Red proof (pre-change, scratch `red_1945.py` + implementer's pre-change run of the new tests: 6 FAILED, 5 with the `pipeline.py:545` AttributeError, T4 ImportError): T2/T1 setup at HEAD → HTTP 500, `AttributeError: 'FileJournalRetryService' object has no attribute 'submission_runtime_root_resolution'`
- [x] `uv run pytest -q tests/test_retry.py tests/test_retry_cancel_consistency.py` green locally (202 passed at 1f3e96ed; 201 at 1140b72d)
- [x] `uv run pytest -q tests/test_select_ci_tests.py` green locally (458 passed; `_top_level_imported_module_names('tests/test_retry.py')` has no file-lane entry)
- [x] `uv run ruff check .` clean
- [x] node-27 receipt (host ghdc, HEAD 1140b72d: 201 + 712 passed; 1f3e96ed: 202 + 712 passed; command lines recorded in `.workplans/pr-1945/node27-receipt-<sha>.txt`) for `uv run pytest -q tests/test_retry.py tests/test_retry_cancel_consistency.py` (+ api/journal suites)
- [x] Frozen surfaces diff-empty vs `origin/master`: `RetryService.submission_runtime_root_resolution` body; `get_retry_service`; `attempt_manual_retry`'s `except Exception` block, `_record_manual_retry_submission_failure`, `_manual_retry_submission_failure_details`; the 409 mapping in `retry_run`; `_file_retry_event_runtime_root_candidates`. `retry.py` diff is the Protocol block plus `runtime_checkable` on the `typing` import line, nothing else. (Checked at implementation: all diff-empty; `tests/test_retry.py` has exactly one deleted line, the `type: ignore`.)
- [x] CI green on every pushed head so far (1140b72d run 33633144326, 1f3e96ed run 33646603046: Unit Tests success) and recorded in the round ledger; the final head's run is the merge gate
- [x] `openspec validate file-lane-retry-submission-failed-503 --strict --no-interactive`

## 1. File-lane reader (D1)

- [x] 1.1 `FileJournalRetryService.submission_runtime_root_resolution(job_id) -> dict[str, Any] | None` beside `_file_retry_event_runtime_root_candidates` (no helper extracted from it): `get_pipeline_job` → `None` or blocked row (`job.get("file_journal", {}).get("status") == "blocked"`, NOT a `job_id` sentinel compare — `get_pipeline_job` keeps the real id) → `None`; `_cycle_rows` → the job's `submission` events latest-first by `event_id` → first Mapping `details["runtime_root_resolution"]` returned as `dict(...)` unchanged (no trigger filter, no second redaction — docstring states both and why)
- [x] 1.2 `except FileOrchestrationJournalError` spanning steps 1-3 as one block → `logger.warning` with only `reason`/`field` → `return None` (D1 step 6), docstring stating it is a narrower fail-soft than `get_pipeline_job`'s blocked-row conversion, why T2/T3 responses are identical, and the DB-lane asymmetry

## 2. One seam (D2)

- [x] 2.1 `@runtime_checkable class ManualRetryService(Protocol)` in `retry.py` beside `RetrySubmitter`, two methods with the route's call shapes; `typing` import gains `runtime_checkable`; no `__all__` exists, nothing exported
- [x] 2.2 `pipeline.py`: `_RetryExecutionContext.service: ManualRetryService`; `get_retry_execution_context` annotation; `get_retry_service` untouched
- [x] 2.3 `tests/test_retry.py` 409 test: remove `# type: ignore[arg-type]` on `service=`; `gateway=` suppression kept or removed with the reason recorded

## 3. Tests (D3)

- [x] 3.1 T1 evidence present (`_clear_runtime_root_env` first; `WORKSPACE_ROOT` + `OBJECT_STORE_ROOT` distinct realpaths; gateway with `submit_job` only; response equals persisted event details; redaction assertions incl. both injected roots absent); `error.code` also equals the persisted event's `error_code`
- [x] 3.1b (round-1 cand-01, P2 coverage) `test_retry_api_file_lane_second_retry_reports_its_own_evidence`: two POSTs on one run mint `_retry_active` then `_retry_2`; differ-guard on the two persisted mappings; second response equals its own event and not the first's; direct reader call on the first job returns the first's (kills the filter-only mutant; the sort-order half has no in-domain oracle — one submission event per job id)
- [x] 3.2 T2 evidence absent (key absent, not `null`)
- [x] 3.3 T3 on T1's baseline, three tests: (a) literal `_cycle_rows` fault (lands on the blocked branch via `get_pipeline_job`), (b) hardened: pin `get_pipeline_job` to the real row then fault `_cycle_rows` with the documented journal-relative field `journal/gfs/2026072000.jsonl` (round-1 cand-05) → reader's `except` + one redacted WARNING (`caplog`), (c) blocked row from `get_pipeline_job` → key absent, 503 intact, and NO `evidence unreadable` WARNING (negative `caplog`, round-1 cand-02 — the sole discriminator between the guard and the `except`)
- [x] 3.4 T4 Protocol `isinstance` pin for both lanes
- [x] 3.5 Existing DB-lane 503 tests unchanged (diff-empty on those functions)

## Risk packs

- Error handling — selected (the change is an error-surface contract; D1 step 6 boundary).
- Security light — selected (new response surface with journal-derived evidence; write-side scrub parity, T1 equality against the persisted event).
- Legacy compat — selected (DB lane byte-unchanged except the Protocol; both concrete classes satisfy it without edits to the DB lane's methods).
- File IO / path safety — not selected (no new walk; existing read paths with their own fault boundaries).
- Concurrency — not selected (read-only second read; no lock, no write).
- Performance — not selected (one cycle-rows read of a cache-warm cycle).

## Non-goals

See proposal: swallow semantics, 409 mapping, production wiring, namespace-carry alternative, redaction widening.
