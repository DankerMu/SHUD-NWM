# Design: auto-retry-skipped-event

Fixture level: expanded (mandatory trigger: retry path)
Repair intensity: medium
Project profile: NHMS

## Change surface

- `services/orchestrator/retry.py`: new pure helper
  `auto_retry_skipped_details(error_code) -> dict | None`; wiring inside
  `mark_permanently_failed` (~:409-436, event `permanently_failed`,
  details currently `{final_retry_count, last_error, failure,
  automatic_retry_stopped}`).
- `services/orchestrator/file_orchestration_journal.py`
  (`FileJournalRetryService` class at ~:6712, `mark_permanently_failed` at
  ~:6900) — the plane has TWO `permanently_failed` details production
  points, both must merge the helper output:
  1. non-master branch (~:6930-6939, direct `insert_pipeline_event`);
  2. master branch: `mark_permanently_failed` diverts current-contract
     master rows (~:6903-6912) to `_mark_master_permanently_failed`
     (~:6943-6969), which builds `event_details` (~:6959-6968) handed to
     `FileOrchestrationJournalRepository.mark_pipeline_job_permanently_failed`
     (~:2417-2483; the repository appends the event at ~:2481 and may
     instead return `missing`/`stale`/`idempotent` WITHOUT appending).
  Both import the helper from `retry.py`.
- Tests in `tests/test_retry.py`, `tests/test_file_orchestration_journal.py`.

## Key decisions

1. **Reuse `permanently_failed` event** (issue's recommended arm, ruled in
   proposal): merge the payload into existing `details`; no new event type,
   no journal-consumer/display changes.
2. **Discrimination rule is the invariant**: the `auto_retry_skipped` key
   appears iff the block is classification-caused. Concretely: present when
   `error_code` is non-transient OR unknown (neither list); ABSENT when a
   transient code exhausted its retry budget (`limit_exhausted`), and absent
   on manual/permanent-marker paths that reach `mark_permanently_failed`
   without a classification block. The helper returns `None` for
   transient codes; call sites merge only non-None. `classify_failure`'s
   existing fields (`retryable`, `limit_exhausted`) stay untouched.
   Edge ruled: a NON-TRANSIENT code always carries the key even when its
   attempt count also exceeds the limit (classification blocks first —
   matches `should_auto_retry` short-circuit semantics); a transient code
   at the limit never carries it.
   Edge ruled: `error_code=None`/empty (job failed without a recorded code)
   is NOT on either list — but the spec scenario is about "an error code not
   listed"; a missing code is not a code. Rule: no key for None/empty
   (nothing to audit-classify), and a test pins this. NOTE the resulting
   contract wording: the key appears iff a RECORDED error code is
   classification-blocked; both no-code and limit-exhausted cases are
   keyless and are distinguished from each other by the existing
   `failure.limit_exhausted` / `failure.reason_code` fields — "key absent ⇒
   retries exhausted" is deliberately NOT claimed (classify_failure
   normalizes None to UNKNOWN_FAILURE with retryable=False,
   limit_exhausted=False, so a no-code job is also classification-blocked
   yet carries no key).
3. **Warning log** (spec.md:171): exact text
   `unknown error_code '<code>' defaulted to non-transient — add to
   classification list`, module logger. Placement rule: the warning (and
   the payload) accompany an event that is ACTUALLY APPENDED — emit exactly
   one warning per appended `permanently_failed` event on the unknown-code
   branch, never on classification queries. Master-branch consequence: the
   repository's `mark_pipeline_job_permanently_failed` may return
   `missing`/`stale`/`idempotent` without appending (~:2449-2460); the
   implementation must not leave an orphan warning in those outcomes
   (log after the append outcome is known, or gate on it). DB plane and
   file non-master branch append unconditionally once reached, so their
   warning is inherently paired with an append; the residual edge — a
   caller re-marking from a stale snapshot produces a second event AND a
   second warning — mirrors the pre-existing duplicate-event semantics
   (caller contract: re-read the row before marking) and is pinned by a
   test rather than guarded.
4. **Single source**: file-journal plane imports the helper; a test asserts
   the reason literals appear exactly once in `services/` (no dual
   maintenance).
5. **db-free plane disposition** (documented in the spec delta, no code):
   scheduler_core's retry decisions flow through `FileJournalRetryService`
   (covered); `scheduler_state_failure._failure_policy_payload` is pure
   adjudication with `pipeline_event_writes_proven_absent: True` evidence
   contract — it stays sink-free by design. The spec.md:171 WARNING is
   likewise exempted on this path (deliberate, recorded): the payload/
   warning obligation binds where the mark event lands (decision 3's
   append-gated rule); `_failure_policy_payload` is re-evaluated every
   scheduler pass with no append anchor, so a warning there would repeat
   per pass without an event to anchor audit — the guard-blocked failure
   reaching permanent failure still produces its single warning at the
   file-journal sink.
6. **Known unknown-default codes accepted knowingly** (recorded, not
   remediated here): the production catch-all `SLURM_JOB_FAILED` is
   test-pinned OFF both classification lists
   (tests/test_real_slurm_gateway.py:1029-1036), and several
   classifier-recognized stage codes (e.g. `SHUD_FAILED`, minted
   `{STAGE}_FAILED` codes) are likewise on neither list. Under spec.md's
   literal rule they land in the `unknown_error_code_defaulted_non_transient`
   branch and emit the "add to classification list" warning — for
   `SLURM_JOB_FAILED` that advice is unactionable by design. This is the
   spec-mandated behavior and this change implements it verbatim; tests pin
   `SLURM_JOB_FAILED`/`SHUD_FAILED` → unknown reason + warning so the
   dominance is visible, not accidental. Changing the classification sets or
   the warning text is out of #1314 scope and tracked in issue #1462.

## Must preserve

- Existing `permanently_failed` event fields (`final_retry_count`,
  `last_error`, `failure`, `automatic_retry_stopped`) — additive only;
  existing consumers/tests keep passing.
- Retry classification behavior byte-identical: `should_auto_retry`,
  `classify_failure`, both classification sets untouched.
- File-journal event schema remains insert-compatible (details is JSON).

## Seams under test

- DB plane: `RetryService.handle_failed_job` / `mark_permanently_failed`
  via the existing fake store pattern in tests/test_retry.py.
- File plane: `FileJournalRetryService.mark_permanently_failed` via the
  existing journal fixtures in tests/test_file_orchestration_journal.py.
- Spec alignment: parse the non-transient list from
  `openspec/specs/job-retry-mechanism/spec.md` (reuse the existing parse
  pattern at tests/test_retry.py:40-58) and parameterize the full set.

## Risks to watch

- The `failure` sub-dict inside details already carries
  `retryable`/`limit_exhausted`; the new key must not contradict it
  (assert consistency in at least one test).
- caplog-based warning assertion must pin logger name/level, not substring
  of unrelated output.
