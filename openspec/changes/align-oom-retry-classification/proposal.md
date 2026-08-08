# Proposal: align OUT_OF_MEMORY retry classification to the approved spec (direction A)

## Why

Approved spec `job-retry-mechanism` (`spec.md:151-153`) classifies
`OUT_OF_MEMORY` as NON-transient — "MUST NOT schedule an automatic
retry", "SHALL mark the job as permanently failed immediately" — while
the orchestrator classifies it transient and auto-retries on the backoff
schedule (issue #1161). The contradiction violates a MUST NOT clause,
wastes Slurm quota on deterministic re-failures (OOM = `memory_gb` too
low), and delays configuration-fault visibility by ~21 minutes (default
backoff 60+300+900s).

## Ruling (AC1: recorded here and in design D1) — Direction A

**Code aligns to spec.** Grounds:

1. The spec is the approved artifact and its rationale holds under
   Slurm's standard cgroup-enforced memory accounting (this repo's
   sbatch templates all set `#SBATCH --mem={{memory_gb}}G`;
   `infra/sbatch/*.sbatch:9`): the OOM kill fires against the job's OWN
   limit, so neighbor pressure manifests as node-level failure
   (`NODE_FAILURE`), not this job's `OUT_OF_MEMORY` — weakening
   direction B's contention argument. (Caveat recorded: on a
   non-cgroup-constrained node the global OOM killer can also produce
   this code; the repo's sole producer is
   `real_backend.py:150-151`. Grounds 2-4 stand independently.)
2. Direction B's "deliberately pinned in production" hypothesis is
   refuted by the issue's own archaeology: spec text and code set appear
   SIMULTANEOUSLY in the 2026-06-23 bulk-import commit `35ae1b96`
   (`git log -S` finds no earlier introduction) — the drift existed from
   the visible history origin; the pinning tests pin the drift, not a
   decision.
3. Consistency with the spec's conservative baseline: unknown codes
   default to non-transient (`spec.md:166-171`).
4. The compromise (per-code retry budget) introduces a new mechanism
   the flat two-set model does not have — new design, not alignment;
   rejected per the issue's own caution.

## What Changes

- `services/orchestrator/retry.py`: `OUT_OF_MEMORY` moves from
  `TRANSIENT_ERROR_CODES` (`:27`) to `NON_TRANSIENT_ERROR_CODES`;
  `failure_classifier` (`:167`) reclassifies it out of
  `transient_slurm_runtime` (design D2 fixes the target class).
- `services/orchestrator/scheduler_state_types.py`:
  `TRANSIENT_RETRY_REASON_CODES` (`:61`) drops `OUT_OF_MEMORY` — the
  db-free scheduler's retryable-reason surface follows the same ruling
  (consumed at `scheduler_state_failure.py:205` and `:291`).
- `services/orchestrator/scheduler_state_failure.py`
  `_downstream_failure_restartable` (`:603-611`): the refusal sets gain
  `OUT_OF_MEMORY` / `resource_configuration` — without this the
  downstream-resume channel overwrites the failure to retryable BEFORE
  the permanent check and OOM keeps auto-retrying whenever durable SHUD
  output exists (fixture review P1-1, measured; design D2 row 5).
- `services/orchestrator/scheduler_state_failure.py`
  `_MISSING_FORECAST_OUTPUT_RECOMPUTE_CODES` (`:266-274`): **OOM STAYS,
  with recorded justification** (AC3's "说明为何某处不需变更" branch).
  That set is not a transience classification — it already contains
  explicitly non-transient codes (`PARSE_TASK_FAILED`,
  `PUBLISH_TASK_FAILED`, `STATE_SAVE_QC_TASK_FAILED`) and gates a
  different remedy: "the durable forecast output is missing, restoring
  it requires recomputing the forecast stage". Membership answers
  output-absence remediation, not auto-retry eligibility (design D3).
- Tests: the 5 pinning assertions rewritten to the spec behavior; three
  NEW anchors (design D4): a parity anchor locking "spec classification
  == code classification" against the spec file text, the two-sided
  recompute-boundary anchor (D2 row 4), and the downstream-resume block
  anchor (D2 row 5).
- Spec delta: the Retry Guard requirement gains one scenario making the
  spec-code classification parity an explicit, test-anchored obligation.
- No journal/DB backfill: classification acts at failure time; historical
  `error_code=OUT_OF_MEMORY` rows keep their recorded outcomes (issue
  ruling, restated).

## Impact

- Affected specs: `job-retry-mechanism` (one requirement modified — one
  scenario added; classification lists unchanged: the spec was already
  right).
- Affected code: `retry.py`, `scheduler_state_types.py`,
  `scheduler_state_failure.py` (`_downstream_failure_restartable` guard
  only; `scheduler_state_compat.py:27` re-export untouched), tests
  `test_retry.py`, `test_real_slurm_gateway.py`,
  `test_production_scheduler.py` (+ new anchors per design D4).
- Behavior delta: an OOM-failed job now goes `permanently_failed`
  immediately (manual retry remains available) on the DB plane, the
  db-free scheduler plane (verdicts flip to `manual_retry_required`,
  including the durable-output downstream-resume geometry — D2 row 5),
  and the file-journal retry plane (follows `classify_failure`
  automatically); the missing-forecast-output recompute channel is
  intentionally unchanged (D2 row 4); the production-closure
  retry-cancel evidence field `"transient"` truthfully flips to `False`
  for OOM (indirect consumer, disclosed intended).
- Known limit (pre-existing, repo-wide, tracked separately per the
  issue): the `auto_retry_skipped` pipeline_event payload required by
  `spec.md:154,170` is implemented nowhere for ANY non-transient code;
  this change does not add it for OOM either — routed at closeout
  (tasks 5.0) instead of half-implementing it for one code.
- Out of scope: #1160 (NODE_FAILURE fallback), other error codes' audit,
  the compromise per-code budget design.
