# Proposal: retry-stage-failure-classification

## Why

Issue #1462 (deferred from PR #1459 round-1 CAND-A): the SHUD
runtime family (`SHUD_FAILED`/`FAILED_RUN`/`RUNTIME_FAILED`, which
`failure_classifier` already names `shud_runtime_failure`) and the
minted `{STAGE}_FAILED` family sit on neither classification list,
so any recorded occurrence lands on the unknown-default audit branch
with a WARNING whose "add to classification list" advice this change
finally executes for those codes.

HONEST REACH STATEMENT (fixture-review P2-1 corrected the issue's
premise): this is a SPEC/CODE-ALIGNMENT + FUTURE-PROOFING change,
not a live audit-distribution fix. The reachability audit found 11
of the 13 codes have NO producer anywhere in the repo today; the
reader-synthesized `{STAGE}_FAILED` placeholders
(scheduler_state_failure.py:340/:395) are never written back to job
rows and therefore never reach `auto_retry_skipped_details` (which
is fed only `pipeline_job.error_code`); `CONVERT_FAILED`/
`FORCING_FAILED` are written to cycle/forecast rows, not job rows.
The unknown branch's REAL current population is `SLURM_JOB_FAILED`
(unknown by #1419 ruling), `PUBLISH_TILES_FAILED` (the publish
stage's actual job code), `NO_ACTIVE_BASINS`, and an ~82-code long
tail — recorded as the deliberately-unclassified remainder (design
decision 6) rather than implied-resolved. What this change buys:
the classifier-recognized SHUD family and the canonical stage
domain can never mint an unclassified mainline code in the future,
and the spec stops giving non-actionable advice for codes whose
classification is now executed.

SCOPE RULING (user-decided 2026-08-17, recorded): only issue item
(a). Items (b) third catch-all reason and (c) warning-wording split
are SUPERSEDED by #1419/PR #1508's same-day D2 ruling —
`SLURM_JOB_FAILED` is explicitly "true unknown, on neither list"
(its classifier branch is a signed-off default; PR #1417's anchor
depends on the dual absence) and the warning wording for it was
explicitly ACCEPTED as-is ("改文案要为一个码特判，得不偿失"). This
change does not touch that ruling; `SLURM_JOB_FAILED` keeps the
unknown reason and its warning.

FACT CORRECTION vs the triage discussion: the minted stage family
derives from `DOWNSTREAM_RESTART_STAGES`
(services/orchestrator/scheduler_state_types.py:34 — 7 stages:
convert, forcing, forecast, parse, state_save_qc, publish,
copyback), not the 5-entry `_FORECAST_STAGE_ORDER` cited during
triage: both minting sites (scheduler_state_failure.py:340/:395)
canonicalize through `_canonical_downstream_stage`, which returns
None outside that closed 7-stage domain. The user's ruling
("derive from the canonical stage constant") therefore yields 7
stage codes, not 5.

## What Changes

- `NON_TRANSIENT_ERROR_CODES` (services/orchestrator/retry.py:40)
  gains 13 members:
  - SHUD trio: `SHUD_FAILED`, `FAILED_RUN`, `RUNTIME_FAILED`
    (rerunning the same configuration does not converge; classifier
    already groups them as `shud_runtime_failure`);
  - stage family, DERIVED IN CODE from `DOWNSTREAM_RESTART_STAGES`
    (`f"{stage.upper()}_FAILED"` — cycle-free import,
    scheduler_state_types imports stdlib only): `CONVERT_FAILED`,
    `FORCING_FAILED`, `FORECAST_FAILED`, `PARSE_FAILED`,
    `STATE_SAVE_QC_FAILED`, `PUBLISH_FAILED`, `COPYBACK_FAILED` — a
    future canonical stage auto-classifies, no open-ended
    `endswith("_FAILED")` predicate (which would swallow
    `SLURM_JOB_FAILED` and the transient
    `SBATCH_SUBMISSION_FAILED`/`STORAGE_WRITE_FAILED`);
  - task codes: `STATE_SAVE_QC_TASK_FAILED`, `PARSE_TASK_FAILED`,
    `PUBLISH_TASK_FAILED` (scheduler_state_failure.py:377-379).
- BEHAVIOR INVARIANT (proof obligation, not a hope):
  `NON_TRANSIENT_ERROR_CODES` has exactly ONE production consumer —
  the audit-reason ternary at retry.py:150. `is_retryable_failure`
  == `is_transient_error` (transient list only), so permanence,
  backoff, downstream-resume and every gate are untouched; the 13
  codes already defaulted to non-transient behavior. What changes:
  their audit `reason` becomes `non_transient_error` and the
  non-actionable unknown-code WARNING stops firing for them.
- Spec delta (`job-retry-mechanism`): the 13 codes join the
  "Non-transient error codes block auto-retry" scenario's bullet
  list (the suite's `_spec_non_transient_error_codes()` parser reads
  exactly that window, auto-extending the end-to-end parametrized
  pins and keeping the spec↔code reconciliation test green); one new
  scenario pins the stage-family derivation to the canonical stage
  domain.
- Exemplar-rot repair (fixture-review P1-1): classifying
  `PARSE_FAILED` falsifies two live scenarios that cite it as an
  unknown-default exemplar. Second MODIFIED requirement in
  `job-retry-mechanism` ("Pre-Guard Evidence Channels Consult
  Permanence" — the downstream-resume scenario's exemplar moves
  `PARSE_FAILED` to the non-transient side, `SLURM_JOB_FAILED`
  stays the unknown-default exemplar) and a
  `multibasin-state-idempotency` MODIFIED ("Resumable downstream
  failures" — same swap). Both are wording-only; the refusal
  behavior is identical on both sides of the swap.
- Test anchors swapped, never deleted:
  `_UNLISTED_PRODUCTION_ERROR_CODES` (tests/test_retry.py:56) drops
  `SHUD_FAILED` and keeps `SLURM_JOB_FAILED` (the #1419 ruling's
  pin); a new derivation pin asserts the stage-family membership
  equals the canonical-domain derivation.

Non-goals: `FORECAST_TASK_{STATUS}` (closed at
FORECAST_TASK_FAILED/CANCELLED, written to hydro-run status not job
rows), `NO_ACTIVE_BASINS` (configuration condition),
`PUBLISH_TILES_FAILED` (the publish stage's REAL job code — its
transiency semantics were not part of the user ruling; left for a
future explicit ruling, recorded), and the ~82-code unclassified
long tail (adapters, copyback, reconcile families) all stay on the
unknown branch deliberately — recorded here and in the PR body,
still covered by the generic unknown scenario; `SLURM_JOB_FAILED` (all of #1419 D2 stands);
`failure_classifier` branches (classification strings are a separate
surface; SHUD family already classified); `map_slurm_error_code`
(#1419 delivered); the auto_retry_skipped event plumbing (#1314
delivered); the permanence gate (#1313/#1417).

## Capabilities

- `job-retry-mechanism`: MODIFIED "Retry Guard — Non-Transient Error
  Exclusion" (13 codes join the scenario list; stage-family
  derivation scenario added) + MODIFIED "Pre-Guard Evidence Channels
  Consult Permanence" (unknown-default exemplar swap, P1-1).
- `multibasin-state-idempotency`: MODIFIED "Resumable downstream
  failures" (same exemplar swap, P1-1).

## Impact

- `services/orchestrator/retry.py` (set members + derivation),
  `openspec/specs/job-retry-mechanism/spec.md` +
  `openspec/specs/multibasin-state-idempotency/spec.md` (deltas AND
  byte-identical live-spec parity edits in-PR — the suite's parser
  reads the live spec, so lockstep is mandatory; design decision 9;
  archive becomes an idempotent replace),
  `tests/test_retry.py` (anchor swap + derivation pin).
- Closes #1462. Verification per its `Verification:` field:
  `uv run pytest -q tests/test_retry.py
  tests/test_real_slurm_gateway.py`; `uv run ruff check .` (tracked
  form); `openspec validate retry-stage-failure-classification
  --strict --no-interactive`.
