# Design — file-lane retry submission failure → structured 503

Line cites against `origin/master` `af394fd1`; symbol names are authoritative.

## Risk triage

- **Fixture level: compact** (S-size contract fix; latent exposure; one read
  method + one Protocol + route tests). Repair intensity: standard. The issue
  carried no upstream `Suggested fixture level`; this is the orchestrator's
  triage and is recorded as such.
- **Must-preserve behaviour**: DB-lane 503 shape and its three route tests
  (`tests/test_retry.py:1783/1850/2308`) byte-for-byte; the file-lane 409
  mapping; `attempt_manual_retry`'s failure persistence
  (`_record_manual_retry_submission_failure` `:11667`,
  `_manual_retry_submission_failure_details` `:12493`); `get_retry_service`
  still builds the DB lane; `RetryService.submission_runtime_root_resolution`
  body unchanged; `_file_retry_event_runtime_root_candidates` (`:11517`) unchanged —
  the new reader mirrors its read path but MUST NOT inherit its
  `manual_retry_marker` / `_event_details_is_manual_retry_submission` filters
  (`:11538`, `:11552`), so no shared helper is extracted from it.
- **Seams under test** (upstream-declared): the route's single call site
  `pipeline.py:545`; `FileJournalRetryService`; `RetryService`;
  `_RetryExecutionContext`.
- **Risk packs selected**: Error handling (core: the fix is an error-surface
  contract), Security light (a new response surface carries journal-derived
  evidence; redaction parity with the DB lane), Legacy compat (DB lane must not
  move; the Protocol must be satisfied by both concrete classes without
  touching the DB lane's methods).
- **Not selected**: File IO / path safety (no new filesystem walk — the reader
  reuses `get_pipeline_job` + `_cycle_rows`, both existing read paths with
  their own fault boundaries); Concurrency (read-only second read of durable
  rows; no lock, no write); Performance (one cycle-rows read of the same cycle
  the retry just wrote; cache-warm).

## D1 — `FileJournalRetryService.submission_runtime_root_resolution`

### Today

`retry_run` `:530-553`: after `attempt_manual_retry` returns a
`submission_failed` namespace, the route builds `details` from the namespace
and then calls `context.service.submission_runtime_root_resolution(job.job_id)`
(`:545`), adding the mapping under `details["runtime_root_resolution"]` when
not `None`. DB lane (`retry.py:713-726`): latest-first scan of
`PipelineEvent(entity_type="pipeline_job", entity_id=job_id, event_type="submission")`
ordered by `event_id desc`, first `details["runtime_root_resolution"]` that is
a Mapping, returned through `_redacted_mapping` (`redact_payload`). File lane:
method absent → `AttributeError` → FastAPI 500.

What the file lane persists on failure (`:11238-11245` → `:11667-11686` →
`:12493-12508`): `update_pipeline_job_status(job_id, "submission_failed",
error_code, error_message)` plus one `submission` event whose `details` carry
`trigger="manual"`, `error_code`, `error_message`, and — only when
`_manual_retry_submission_request` resolved runtime roots before the gateway
raised — `runtime_root_resolution = _public_evidence(evidence)` (and
`runtime_root_contract`). `_resolve_file_retry_runtime_roots` returns `None`
(→ no evidence attached, key absent in the event) when no candidate resolves
completely and roots are not required (`job_type != download_source_cycle`
and not db-free-required).

### Change

New public method on `FileJournalRetryService`, placed beside
`_file_retry_event_runtime_root_candidates` (`:11517`) whose read path it
mirrors:

```
def submission_runtime_root_resolution(self, job_id: str) -> dict[str, Any] | None
```

1. `job = self.repository.get_pipeline_job(job_id)`; `None` or a blocked row
   → `None`. Blocked-row criterion: `job.get("file_journal", {}).get("status") == "blocked"`
   (`_blocked_query_job` `:13097-13113`). Two producers, two sentinels:
   `query_pipeline_jobs_by_run` (`:1881`) leaves `job_id` at the default
   `"file_journal_read_blocked"` (what `_manual_retry_source_for_run` `:11691`
   filters on), but `get_pipeline_job` (`:1874`) passes the real `job_id`
   through, so a `job_id` comparison never matches here. Without this check
   step 2's `_source_id_from_job` (`:12717` → `_required_safe_identity`
   `:14004`) raises on the blocked row's `cycle_id=None`.
2. `rows = self.repository._cycle_rows(source_id=_source_id_from_job(job),
   cycle_time=_cycle_time_from_job(job), model_id=_optional_safe_identity(job, "model_id"))`.
3. Filter `rows.pipeline_events` to `entity_id == job_id and event_type == "submission"`,
   sort by `_optional_positive_int(event.get("event_id")) or 0` descending,
   return the first `details["runtime_root_resolution"]` that is a Mapping as
   `dict(evidence)`; else `None`.
4. **No trigger filter.** The sibling reader skips manual-retry events because
   it wants the *original* submission's roots; this reader wants the latest
   event, which after a failed manual retry *is* that retry's own failure
   event. The docstring says so.
5. **No second redaction.** Both lanes' evidence comes from the same
   `_runtime_root_resolution_evidence` (`retry.py:1805`), which already runs
   `_redacted_mapping` (= `redact_payload`) on the whole mapping (`:1844`) and
   `_bounded_redacted_text` (`:1889`) per source/value, and
   `_runtime_root_resolution_from_error` (`:1275`) redacts once more before
   persistence. The file lane then persists `_public_evidence(...)` of that
   (`:11644`, `:12506`); the DB lane reads back through `_redacted_mapping`
   (`retry.py:725`) because its stored mapping never passed the public scrub.
   Returning the persisted mapping unchanged is what
   makes the acceptance pin "response equals persisted event details"
   provable in one assertion (no leak, no double scrub). Write-side scrub
   parity, stated precisely (round-1 cand-04): `_sanitize_public_evidence`
   redacts sensitive keys; for keys ending `_path`/`_root` the key rule at
   `_sanitize_public_field` (`:13081`) replaces the **whole value** with
   `[local-path]` — so `resolved.object_store_root` (and any
   `published_artifact_root`) is a bare string in the file lane's persisted
   and returned evidence, while `resolved.workspace_dir` keeps
   `{present, source, value}` with `value` rendered `[local-path]` through the
   scalar path (`_sanitize_file_provider_scalar`,
   `scheduler_file_providers.py:2258-2270`); URI-shaped scalars go through
   `_sanitize_file_provider_evidence_scalar`, free text through
   `_safe_error_message` (= `redact_payload`). The file lane's scrub is
   therefore **wider than `redact_payload` but structurally lossy**: the DB
   lane keeps `{present, source, value, same_as_workspace}` for
   `object_store_root` (`retry.py:1840-1853`; `same_as_workspace=True` is
   unreachable from fresh resolution, `:1518-1528` rejects the pair earlier),
   the file lane drops `present`/`source` for every `_root` key. Fixture
   review checked `missing`/`rejected`/`candidate_counts`/`db_free_runtime.*`/
   identity fields: nothing narrower than `redact_payload` there. The collapse
   is pre-existing write-side behaviour (`_public_evidence` untouched by this
   PR) and is routed as #1965 (distinct from #1961); the DB lane's own
   asymmetry — returning `resolved.<field>.value` absolute local roots
   verbatim — is #1961
   (p3: reachable only from a `compute_control` + `DATABASE_URL` process by an
   authorised operator; display_readonly answers 409 before the 503 arm).
6. **Second-read fault is fail-soft.** The call at `:545` runs *after*
   `attempt_manual_retry` returned, outside the route's `except RetryError`
   block. A `FileOrchestrationJournalError` raised by step 1-2 there would be
   the same 500 collapse this change removes. The reader therefore catches
   `FileOrchestrationJournalError` and returns `None`: the 503 is emitted with
   the evidence key absent, and the failure event is already durable in the
   journal for the operator to read. Honest classification: this is a
   **narrower fail-soft than the precedent** — `get_pipeline_job` (`:1769`)
   converts a typed fault into a blocked row that still carries
   `error_code=reason` and `file_journal.{status,reason,field}` tokens, while
   this reader returns a bare `None`, so on the HTTP surface "no evidence"
   (T2) and "evidence read faulted" (T3) are byte-identical. Accepted for this
   change (adding a discriminator to the 503 details would widen the route
   contract, out of scope); the reader emits one `logger.warning` carrying
   only `error.reason` and `error.field` — the latter either a journal-relative
   token such as `journal/gfs/2026072000.jsonl` (journal read/decode raise
   sites pass the path through `_relative_evidence`, `:14990-14994`, which
   falls back to `[local-path]` for out-of-root paths) or a bare column name
   such as `cycle_id` / `source_id` from the identity helpers
   (`_required_safe_identity`, `_normalize_file_source_id`) — never an
   absolute root, and no message text — so the fault is observable in logs
   (round-2 C1 wording). The `except` spans steps 1-3 as one block (the
   blocked-row branch, `_source_id_from_job`, and `_cycle_rows` can all raise
   `FileOrchestrationJournalError`), and catches nothing narrower or wider:
   `_JournalProbeContainmentError` (`:724`) is converted to
   `FileOrchestrationJournalError` inside `_read_cycle_segments` (`:10505`),
   so no other typed class escapes `_cycle_rows`. The DB lane is deliberately
   asymmetric: a SQL fault in its reader still surfaces as 500, unchanged by
   this PR (recorded, not fixed — out of scope).

## D2 — one seam for both lanes

`retry.py`, beside `RetrySubmitter(Protocol)` `:321`:

```
@runtime_checkable
class ManualRetryService(Protocol):
    def attempt_manual_retry(self, run_id: str, gateway: Any = None, *, policy_decision: Any = None) -> Any: ...
    def submission_runtime_root_resolution(self, job_id: str) -> dict[str, Any] | None: ...
```

(`gateway` positional-or-keyword with a default, matching both concrete
lanes; an earlier draft declared it keyword-only, which neither lane
satisfies literally — corrected during implementation, deviation 1.)

The concrete signatures are
`attempt_manual_retry(self, run_id, gateway=None, *, policy_decision=None, trusted_internal=False)`
on both lanes (`retry.py:516-523`, `file_orchestration_journal.py:11185-11192`):
`gateway` is positional-or-keyword with a default, `policy_decision` and
`trusted_internal` keyword-only with defaults. The Protocol declares the
route's call shape (`run_id`, `gateway=`, `policy_decision=`) and is
structurally satisfied by both. `retry.py`'s diff is this block plus the
`typing` import gaining `runtime_checkable` (`:11` is
`from typing import Any, Protocol` today); the module has no `__all__`, so
nothing is exported. T4's `isinstance` pin checks attribute presence only —
exactly #1945's failure mode; signature drift has no automatic oracle (CI runs
no mypy) and is left to review. `pipeline.py`: `_RetryExecutionContext.service: ManualRetryService`
(`:90-93`), `get_retry_execution_context`'s `service: ManualRetryService`
annotation (`:185-190`); `get_retry_service` (`:132`) keeps returning the
concrete `RetryService`. CI runs no mypy, so the annotation alone is
documentary; the oracle is a test asserting
`isinstance(RetryService(...), ManualRetryService)` and
`isinstance(FileJournalRetryService(...), ManualRetryService)` — the test that
would have caught #1945. The 409 test drops `# type: ignore[arg-type]` on
`service=`; the `gateway=` suppression stays unless the fake becomes
`SlurmGateway`-compatible (recorded either way).

## D3 — tests (all in `tests/test_retry.py`, imports inside the test bodies)

Top-level imports of `file_orchestration_journal` in `tests/test_retry.py`
would change the CI selector's importer index and re-open
`tests/test_select_ci_tests.py` governance (PR #1951 lesson); every new test
imports the file lane inside its body exactly like the 409 test does, and the
selector suite runs locally before the first push. Recorded trade-off: the
in-body import keeps the importer index unchanged, at the price that a
journal-only or `pipeline.py`-only diff does not select `tests/test_retry.py`
in the PR-targeted CI lane (`select_tests` on `file_orchestration_journal.py`
→ 30 targets, none of them `test_retry.py`; on `pipeline.py` → 3 API suites;
on `retry.py` → yes). T1-T3 are therefore backstopped by the master full run
for such diffs, the same pre-existing blind spot the DB-lane 503 tests live
under. Not fixed here: a selector rule change would reopen
`tests/test_select_ci_tests.py` governance.

Fixture shape (mirrors the 409 test): `FileOrchestrationJournalRepository(tmp_path/"journal")`,
an in-scope `failed` forecast row minted through the public
`upsert_pipeline_job`, a real `FileJournalRetryService`, a gateway whose
`submit_job` raises `RuntimeError("no execution path")`
(→ `_retry_submission_error_code` → `SBATCH_SUBMISSION_FAILED`), injected
into `_RetryExecutionContext` via `app.dependency_overrides`.

- **T1 evidence present**: `_clear_runtime_root_env(monkeypatch)` first
  (`tests/test_retry.py:2757-2776`; it covers every key
  `_runtime_root_env_candidate` `retry.py:1427-1449` reads, including the
  db-free selector env — a stray `NHMS_SCHEDULER_DB_FREE_REQUIRED` would flip
  `_candidate_batch_db_free_required` `:1611` and turn the code into
  `RETRY_RUNTIME_ROOTS_UNRESOLVED`), then `WORKSPACE_ROOT` and
  `OBJECT_STORE_ROOT` set to two **distinct-realpath** tmp directories
  (`_REQUIRED_RUNTIME_ROOT_FIELDS` `retry.py:95` needs only these two;
  `object_store_prefix` defaults `:1508`; a same-realpath pair is rejected as
  `resolves_to_workspace_dir` `:1490-1500` and T1 would silently degrade into
  T2). The fake gateway defines `submit_job` only (an object with
  `submit_job_array` is preferred by `_submit_file_manual_retry_job` `:751-755`
  for `run_shud_forecast_array`). Assert 503;
  `error.code == "SBATCH_SUBMISSION_FAILED"`; `details.status ==
  "submission_failed"`; `details.run_id`/`details.job_id` present;
  `details.runtime_root_resolution == <the persisted submission event's
  details["runtime_root_resolution"]>` read back from the journal; the 409
  test's redaction assertions (`Traceback`, `/journal/pipeline-jobs`,
  `str(journal_root)` absent) **plus** `str(workspace_root)` and
  `str(object_store_root)` absent — the roots T1 actually injects (the write
  side renders `workspace_dir.value` as `[local-path]` via
  `_sanitize_file_provider_scalar` `scheduler_file_providers.py:2258-2270` and
  collapses the whole `object_store_root` sub-mapping to `[local-path]` via the
  key rule at `:13081`; this pins both).
- **T2 evidence absent**: `_clear_runtime_root_env(monkeypatch)`, no prior
  submission events → resolver returns `None` → event has no key → 503 with
  `"runtime_root_resolution" not in details` (absent, not `null`).
- **T3 second-read fault**: built on **T1's** setup (evidence persisted), so
  a missing key is attributable only to the faulted read — on T2's baseline
  the assertion would be vacuous. Seam (no production change): wrap the
  service instance's `attempt_manual_retry` so that after the real call
  returns (failure durably recorded) it replaces
  `service.repository._cycle_rows` with a function raising
  `FileOrchestrationJournalError("file_journal_unreadable", field="journal")`;
  the route's post-return read is then the only faulted call. "Let the first
  call through" counting does not work: one `attempt_manual_retry` reads
  `get_pipeline_job`/`_cycle_rows` several times (`:11480-11489`,
  `:11523-11529`). `get_pipeline_job` is not the fault point: it never raises
  (`:1773` converts to a blocked row). **Implementation finding (deviation 2)**:
  `get_pipeline_job` itself calls `_cycle_rows` (`_pipeline_job_for_id_unlocked`
  `:1786`, always for a row with `model_id`), so faulting `_cycle_rows` alone
  is swallowed there into a blocked row and the reader takes D1 step 1, never
  step 6 — the literal T3 and the blocked-row sister are the same path and
  step 6 had no oracle. T3 is therefore three tests:
  (a) `test_retry_api_file_lane_evidence_read_fault_keeps_503` — the literal
  seam, documented as landing on the blocked branch; (b)
  `test_retry_api_file_lane_evidence_read_fault_reaches_reader_fail_soft` —
  pins `get_pipeline_job` to the real row read from a healthy repository, then
  faults `_cycle_rows`, so the fault reaches the reader's own `except`;
  `caplog` asserts exactly one `services.orchestrator.file_orchestration_journal`
  WARNING carrying `file_journal_unreadable` and the injected journal-relative
  field `journal/gfs/2026072000.jsonl` (the documented real shape, round-1
  cand-05) and none of the three roots — the only oracle for step 6 (it pins
  the documented shape; it is not a leak guard for raise sites); (c)
  `test_retry_api_file_lane_blocked_job_row_keeps_503` — the sister:
  `_blocked_query_job(...)` row (asserting it carries the real `job_id`) →
  key absent, 503 intact (pins D1 step 1 / Invariant 10). Three tests, not
  one: a second POST on the same run hits the retry conflict.
- **T4 Protocol pin**: `isinstance` for both lanes.
- **T5 red proof** (pre-change, scratch, recorded in the PR): T1's setup at
  HEAD returns 500 with `AttributeError: 'FileJournalRetryService' object has
  no attribute 'submission_runtime_root_resolution'`.
- DB-lane 503 tests and `tests/test_retry_cancel_consistency.py` unchanged.

## Invariant Matrix

| # | Invariant | Pinned by |
|---|---|---|
| 1 | File-lane `submission_failed` → HTTP 503 with `code == error_code`, `details.status == "submission_failed"`, `run_id`/`job_id` present | T1, T2 |
| 2 | `details.runtime_root_resolution` equals the persisted event details byte-for-byte when present | T1 |
| 3 | Key absent (not `null`) when the failure event carries no evidence | T2 |
| 4 | Typed journal fault on the second read → key absent, still 503, no traceback; reader's own `except` reached and one redacted WARNING logged | T3 (a) + (b) |
| 5 | Response carries no traceback, journal path, or any local root text (`journal_root`, `WORKSPACE_ROOT`, `OBJECT_STORE_ROOT`) | T1-T3 |
| 6 | Both lanes satisfy one `@runtime_checkable` Protocol; the 409 test needs no `type: ignore` on `service=` | T4 + the 409 test diff |
| 7 | DB-lane 503 tests unchanged; `RetryService.submission_runtime_root_resolution` body diff-empty | Evidence Floor |
| 8 | `attempt_manual_retry` failure persistence and the 409 mapping diff-empty | Evidence Floor |
| 9 | No top-level file-lane import added to `tests/test_retry.py`; selector suite green; coverage-direction blind spot recorded (D3) | Evidence Floor |
| 10 | Blocked row from `get_pipeline_job` on the second read → key absent, 503 intact | T3 sister assertion |

## Boundary-surface checklist

- `.large-file-guard.json` gains two exclusions, `services/orchestrator/retry.py`
  (1899 lines at base) and `apps/api/routes/pipeline.py` (2323 at base): both
  already exceed the local commit hook's `maxLines: 1000`, so annotation-level
  edits cannot be committed without them (precedent `553d2e6a`, `da58bd81`).
  The hook is local tooling, not a CI gate; the structural-burndown spec's
  direction is shrinkage of that list, so this is recorded as an entropy
  surface, disclosed here and in the PR (round-2 C3).

- `retry_run` is the only consumer of `submission_runtime_root_resolution`
  (grep: `pipeline.py:545`, `retry.py:713`); `cancel_run` uses `PipelineStore`.
- Other `FileJournalRetryService` constructors (`scheduler_core.py:71-75`,
  `scripts/node22_manual_retry_failed_runs.py`) do not go through HTTP; the
  new method is additive for them.
- Deployment wiring unchanged: `get_retry_service` → DB lane.

## Review focus

Error handling: the fail-soft boundary in D1 step 6 — is it the file lane's
existing contract or a new swallow, and is T3 discriminating? Security light:
write-side scrub parity (D1 step 5); the T1 equality assertion must be against
the persisted event, not a hand-built expectation. Legacy compat: the DB lane
diff must be the Protocol block only.
