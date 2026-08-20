# job-retry-mechanism Specification

## Purpose
TBD - created by archiving change m3-slurm-nationalization. Update Purpose after archive.
## Requirements
### Requirement: Manual Retry via API

The system SHALL support manual retry of failed runs through the monitoring API.

#### Scenario: Operator triggers manual retry

- **WHEN** a user with `operator`, `model_admin`, or `sys_admin` role sends `POST /api/v1/runs/{run_id}/retry`
- **THEN** the system SHALL:
  1. Identify the most recent `pipeline_job` records for the given `run_id` with `status` = `failed`
  2. For each failed job, create a new `pipeline_job` record with:
     - A new `job_id` (UUID)
     - The same `run_id` as the original job
     - A new `slurm_job_id` (assigned by Slurm upon submission)
     - `retry_count` incremented by 1 from the failed job's `retry_count`
     - `status` set to `submitted`
  3. Submit the new job to Slurm via `sbatch`
  4. Append a `pipeline_event` with `details_json` containing `{"trigger": "manual", "previous_job_id": "<failed_job_id>", "retry_count": <N>}`

#### Scenario: Manual retry allowed after max auto-retries exhausted

- **WHEN** a job has exhausted all automatic retries (i.e., `retry_count >= max_retries`) and a user with `operator+` role sends a manual retry
- **THEN** the system SHALL accept the retry request and create a new job (operator override)
- **THEN** the `pipeline_event.details_json` SHALL include `{"trigger": "manual_override", "note": "exceeded max_retries"}`

#### Scenario: Manual retry preserves run_id

- **WHEN** a manual retry is triggered for a `run_id`
- **THEN** the new `pipeline_job` SHALL use the same `run_id` as the original failed job
- **THEN** the new `pipeline_job` SHALL receive a new `slurm_job_id` from the fresh `sbatch` submission

---

### Requirement: Automatic Retry by Orchestrator

The Orchestrator SHALL automatically retry failed tasks up to a configurable maximum number of retries.

#### Scenario: Auto-retry on transient failure

- **WHEN** a `pipeline_job` transitions to `failed` with a transient error code and `retry_count < max_retries`
- **THEN** the Orchestrator SHALL schedule a retry after the configured backoff delay
- **THEN** a new `pipeline_job` record SHALL be created with `retry_count` incremented by 1
- **THEN** a `pipeline_event` SHALL be appended with `details_json` containing `{"trigger": "auto", "previous_job_id": "<failed_job_id>", "retry_count": <N>, "error_code": "<original_error_code>"}`

#### Scenario: Default max_retries configuration

- **WHEN** the `slurm_gateway` configuration does not specify `max_retries`
- **THEN** the system SHALL default to `max_retries: 3`

#### Scenario: Per-model max_retries override

- **WHEN** a model's configuration specifies a custom `max_retries` value
- **THEN** the Orchestrator SHALL use the model-specific value instead of the global default

---

### Requirement: Exponential Backoff Schedule

Retry delays SHALL follow a configured backoff schedule to avoid overwhelming Slurm with rapid resubmissions.

#### Scenario: Default backoff schedule

- **WHEN** the `slurm_gateway` configuration specifies `retry_backoff_seconds: [60, 300, 900]`
- **THEN** the Orchestrator SHALL delay:
  - 1st retry: 60 seconds after failure
  - 2nd retry: 300 seconds after failure
  - 3rd retry: 900 seconds after failure

#### Scenario: Retry count exceeds backoff array length

- **WHEN** `retry_count` exceeds the length of the `retry_backoff_seconds` array (e.g., 4th retry with a 3-element array)
- **THEN** the Orchestrator SHALL use the last element of the backoff array as the delay (i.e., 900 seconds)

#### Scenario: Backoff timer precision

- **WHEN** the Orchestrator schedules a retry with a backoff delay
- **THEN** the actual submission time SHALL be within +/- 5 seconds of the configured delay (accounting for scheduling jitter)

---

### Requirement: Retry Scope — Failed Basin Only

Retry SHALL target only the specific failed basin or task within an array job, not the entire array.

#### Scenario: Single basin failure in array job

- **WHEN** an array job for the `shud_forecast` stage completes with 1 of 128 basins failed
- **THEN** the Orchestrator SHALL retry only the failed basin's task, creating a single new `pipeline_job` for that basin
- **THEN** the Orchestrator MUST NOT resubmit the entire 128-basin array

#### Scenario: Multiple basin failures in array job

- **WHEN** an array job completes with 5 of 128 basins failed
- **THEN** the Orchestrator SHALL create 5 new `pipeline_job` records (one per failed basin) and submit them as individual jobs or as a new smaller array job
- **THEN** each retry job SHALL reference the original `run_id` and the specific `model_id` of the failed basin

#### Scenario: Cycle-level stage failure (non-array)

- **WHEN** a cycle-level stage (e.g., `download`) fails
- **THEN** the Orchestrator SHALL retry the entire stage as a single job since it has no per-basin granularity

---

### Requirement: Retry Audit Logging

Each retry attempt MUST be logged to `ops.pipeline_event` for audit traceability.

#### Scenario: Auto-retry event logged

- **WHEN** the Orchestrator triggers an automatic retry
- **THEN** a `pipeline_event` record SHALL be appended with:
  - `job_id` — the new retry job's ID
  - `from_status` — NULL (new job)
  - `to_status` — `submitted`
  - `details_json` — containing:
    - `trigger` — `"auto"`
    - `previous_job_id` — the failed job's `job_id`
    - `retry_count` — the current retry attempt number
    - `previous_error_code` — the error code from the failed job
    - `previous_error_message` — truncated error message (max 500 chars)
    - `backoff_seconds` — the delay applied before this retry

#### Scenario: Manual retry event logged

- **WHEN** a user triggers a manual retry via the API
- **THEN** a `pipeline_event` record SHALL be appended with:
  - `details_json` containing:
    - `trigger` — `"manual"`
    - `operator_id` — the authenticated user's ID
    - `previous_job_id` — the failed job's `job_id`
    - `retry_count` — the current retry attempt number

---

### Requirement: Retry Guard — Non-Transient Error Exclusion

The Orchestrator SHALL NOT automatically retry jobs that failed with non-transient error codes.

#### Scenario: Non-transient error codes block auto-retry

- **WHEN** a `pipeline_job` fails with one of the following error codes:
  - `INVALID_MANIFEST` — manifest file is malformed or missing required fields
  - `PERMISSION_DENIED` — insufficient permissions to access resources
  - `OUTPUT_INCOMPLETE` — output schema validation failed (data integrity error, not retryable)
  - `TEMPLATE_NOT_ALLOWED` — sbatch template rejected by security policy
  - `MANIFEST_SCHEMA_INVALID` — manifest file fails JSON schema validation
  - `OUT_OF_MEMORY` — Slurm OOM kill (configuration error: memory_gb too low for workload, not transient)
  - `SHUD_FAILED` — SHUD runtime failure; rerunning the same configuration does not converge (classifier: `shud_runtime_failure`)
  - `FAILED_RUN` — SHUD runtime failure, legacy spelling (same family as `SHUD_FAILED`)
  - `RUNTIME_FAILED` — SHUD runtime failure, legacy spelling (same family as `SHUD_FAILED`)
  - `CONVERT_FAILED` — downstream stage failure, minted as `{STAGE}_FAILED` over the canonical downstream stage domain
  - `FORCING_FAILED` — downstream stage failure (same minted family)
  - `FORECAST_FAILED` — downstream stage failure (same minted family)
  - `PARSE_FAILED` — downstream stage failure (same minted family)
  - `STATE_SAVE_QC_FAILED` — downstream stage failure (same minted family)
  - `PUBLISH_FAILED` — downstream stage failure (same minted family)
  - `COPYBACK_FAILED` — downstream stage failure (same minted family)
  - `STATE_SAVE_QC_TASK_FAILED` — task-level downstream failure code
  - `PARSE_TASK_FAILED` — task-level downstream failure code
  - `PUBLISH_TASK_FAILED` — task-level downstream failure code
- **THEN** the Orchestrator MUST NOT schedule an automatic retry
- **THEN** the Orchestrator SHALL mark the job as permanently failed immediately
- **THEN** a `pipeline_event` SHALL be appended with `details_json` containing `{"auto_retry_skipped": true, "reason": "non_transient_error", "error_code": "<code>"}`

#### Scenario: Transient error codes allow auto-retry

- **WHEN** a `pipeline_job` fails with one of the following error codes:
  - `SLURM_TIMEOUT` — Slurm walltime exceeded
  - `SLURM_DEADLINE` — Slurm `--deadline` scheduling window closed before completion (transient scheduling failure)
  - `NODE_FAILURE` — compute node crashed or became unreachable
  - `STORAGE_WRITE_FAILED` — transient storage I/O error
  - `SBATCH_SUBMISSION_FAILED` — sbatch command returned non-zero (transient Slurm scheduler issue)
  - `SLURM_UNAVAILABLE` — Slurm controller unreachable at submission time
- **THEN** the Orchestrator SHALL proceed with automatic retry (subject to `max_retries` and backoff)

#### Scenario: Unknown error code defaults to non-transient

- **WHEN** a `pipeline_job` fails with an error code not listed in the non-transient or transient lists
- **THEN** the Orchestrator SHALL treat it as non-transient and MUST NOT schedule an automatic retry
- **THEN** a `pipeline_event` SHALL be appended with `details_json` containing `{"auto_retry_skipped": true, "reason": "unknown_error_code_defaulted_non_transient", "error_code": "<code>"}`
- **THEN** the Orchestrator SHALL log a warning: `"unknown error_code '<code>' defaulted to non-transient — add to classification list"`

#### Scenario: Stage-failure codes track the canonical downstream stage domain

- **WHEN** the canonical downstream restart stage domain (`DOWNSTREAM_RESTART_STAGES`) contains a stage
- **THEN** the non-transient classification set SHALL contain that stage's minted failure code `{STAGE}_FAILED`, derived mechanically from the domain constant rather than maintained as a hand-copied list, so a future canonical stage cannot mint an unclassified production-mainline code

#### Scenario: Classification parity between this requirement and code is test-anchored

- **WHEN** the repository test suite runs
- **THEN** a test SHALL read this requirement's non-transient code list from the spec text and assert that `OUT_OF_MEMORY` appears there, is a member of the orchestrator's non-transient classification set, and is absent from every transient classification surface (`TRANSIENT_ERROR_CODES` and the scheduler-state transient retry-reason set), so that a reopened spec-code drift on this code fails the suite rather than surviving to review

#### Scenario: auto_retry_skipped payload rides the permanently_failed event

- **WHEN** the guard blocks automatic retry for a non-transient or unknown error code and marks the job permanently failed
- **THEN** the `auto_retry_skipped` payload is merged into the existing `permanently_failed` pipeline event's `details_json` (no separate event type), constructed by a single shared helper consumed by both the DB plane and the file-journal plane so the reason literals have exactly one source

#### Scenario: the auto_retry_skipped key appears iff a recorded error code is classification-blocked

- **WHEN** a job is marked permanently failed because its TRANSIENT error code exhausted the retry budget, or because it reached permanent failure with no recorded error code
- **THEN** the `permanently_failed` event's `details_json` does NOT contain the `auto_retry_skipped` key — the key appears if and only if a recorded error code was blocked by classification; the keyless cases (no recorded code vs. retries exhausted) are distinguished from each other by the existing `failure.limit_exhausted` / `failure.reason_code` fields
- **AND** a job with a NON-TRANSIENT recorded error code carries the key even when its attempt count also exceeds the retry limit (classification blocks first)

#### Scenario: db-free scheduler plane disposition

- **WHEN** the db-free scheduler plane handles a guard-blocked failure
- **THEN** the portion flowing through `FileJournalRetryService` emits the payload via the file-journal sink
- **AND** the pure-adjudication path (`scheduler_state_failure._failure_policy_payload`) remains sink-free per its `pipeline_event_writes_proven_absent` evidence contract and is NOT required to write pipeline events
- **AND** the unknown-code warning obligation is likewise exempted on the pure-adjudication path (it is re-evaluated every scheduler pass with no append anchor; the single warning is emitted where the mark event lands, at the file-journal sink)

### Requirement: Max Retries Exhausted — Permanent Failure

When all automatic retries are exhausted, the job MUST be marked as permanently failed and an alert triggered.

#### Scenario: All retries exhausted

- **WHEN** a `pipeline_job` fails and `retry_count >= max_retries`
- **THEN** the Orchestrator SHALL:
  1. Set the job `status` to `permanently_failed`
  2. Append a `pipeline_event` with `details_json` containing `{"permanently_failed": true, "total_attempts": <N>, "final_error_code": "<code>"}`
  3. Trigger an alert notification (via configured alerting channel) with the job details, error history, and affected basin/model

#### Scenario: Permanently failed job does not block manual retry

- **WHEN** a job is in `permanently_failed` status
- **THEN** the job SHALL still be eligible for manual retry via `POST /api/v1/runs/{run_id}/retry` (operator override)
- **THEN** the operator override SHALL reset the context but preserve the full retry history in `pipeline_event`

---

### Requirement: Retry Identity — Same run_id, New slurm_job_id

Retry jobs SHALL maintain continuity with the original run while obtaining fresh Slurm resources.

#### Scenario: Retry preserves run_id

- **WHEN** a retry job is created (either manual or automatic)
- **THEN** the new `pipeline_job` SHALL use the same `run_id` as the original failed job
- **THEN** the new `pipeline_job` SHALL use the same `cycle_id`, `source`, `stage`, and `model_id` as the original

#### Scenario: Retry gets new slurm_job_id

- **WHEN** a retry job is submitted to Slurm
- **THEN** a new `slurm_job_id` SHALL be assigned by Slurm and stored in the new `pipeline_job` record
- **THEN** the original failed job's `slurm_job_id` SHALL remain unchanged for audit purposes

#### Scenario: Retry gets new job_id

- **WHEN** a retry job is created
- **THEN** a new `job_id` (UUID) SHALL be generated for the retry job
- **THEN** the relationship between original and retry jobs SHALL be traceable via `pipeline_event.details_json.previous_job_id`

---

### Requirement: Concurrent Retry Protection

The system SHALL prevent duplicate retry jobs from being created for the same run.

#### Scenario: Concurrent manual retry requests for same run_id

- **WHEN** two operators simultaneously send `POST /api/v1/runs/{run_id}/retry` for the same `run_id`
- **THEN** only one retry job SHALL be created and submitted to Slurm
- **THEN** the second request SHALL receive HTTP 409 with `{"request_id": "...", "status": "error", "error": {"code": "RETRY_ALREADY_IN_PROGRESS", "message": "A retry for this run is already in progress"}}`
- **THEN** the system SHALL enforce this via a database unique constraint or optimistic lock on `(run_id, status NOT IN terminal_states)`

#### Scenario: Auto-retry skipped when manual retry already active

- **WHEN** the Orchestrator's auto-retry scheduler fires for a failed job but a manual retry for the same `run_id` has already been submitted (status = `submitted` or `running`)
- **THEN** the Orchestrator SHALL detect the existing active retry and skip the auto-retry
- **THEN** a `pipeline_event` SHALL be appended with `details_json` containing `{"auto_retry_skipped": true, "reason": "manual_retry_already_active", "active_job_id": "<existing_retry_job_id>"}`

### Requirement: Missing upstream artifacts SHALL demote failure-state retries to a stable repair-eligible blocker

When a candidate carries a failure signal and the forcing package its forecast stage references does not exist in the configured object-store root, the scheduler SHALL NOT emit another forecast retry from any decision branch — including the failure fallback and the permanent-failure branch — and SHALL instead emit the stable missing-forcing blocker (reason `missing_forcing_package_uri`, stable classifier, `artifact_exists` false, forecast restart stage) that the explicit single-cycle repair authorization channel accepts. Before treating a state with no forcing package reference as missing, the scheduler SHALL attempt provenance recovery through the witnessed read tiers (journal row, journal direct file, object-store forcing-version sidecar record derived from the candidate identity); a redaction placeholder standing in for a withheld URI is not a probeable package reference and SHALL take this recovery path rather than the recorded-URI probe (the public-read redaction boundary is never bypassed and the probe itself is never taught about placeholders). For the sidecar tier the existing artifact existence probe SHALL target the package manifest file key derived from the candidate-derived sidecar key (never the directory-shaped package URI, which the object-path validator rejects, and never the record's own manifest URI taken verbatim — that recorded URI serves as corroborating evidence only), so that a physically present package never produces the missing-forcing blocker, a witnessed-absent package produces exactly the unchanged `missing_forcing_package_uri` blocker, and a sidecar record pointing at a foreign manifest cannot stand in as this candidate's witness. A probe-layer store error after a successful sidecar witness SHALL be contained fail-closed (blocked, never an escaped exception aborting the scheduler pass) and SHALL classify as no-witness rather than as a determined-missing package, because an unreadable probe object is a read fault a package rebuild cannot clear. The sidecar read limit SHALL admit the provenance records the forcing producer actually writes in production, and a record exceeding that limit SHALL be reported with its own tier detail, distinct from a permission or I/O read failure. Only when no tier yields a witness SHALL the decision block with the distinct reason `forcing_version_row_absent` (error code and stable classifier `FORCING_VERSION_ROW_ABSENT`, null artifact reference, provenance source marked absent), which carries the same structural repair-eligible contract, and the single-cycle repair authorization channel — including its stable-classifier structural check — SHALL accept both blocker reason/classifier pairs. Provenance-tier failures (unreadable, malformed, oversized sidecar, unconfigured store, incomplete identity) SHALL classify as no-witness rather than as a determined-missing package, and SHALL never fail open into a retry. Failure classification produced inside the compute task SHALL survive the DB-free path to array accounting through a durable per-task outcome receipt, with generic `NODE_FAILURE` used only as the fail-safe when no receipt is readable. The effective retry attempt SHALL derive from the durable per-stage attempt record (job-identity retry suffix) on both the in-stage retry gate and the scheduler's cross-pass failure policy, so that the configured retry limit bounds retries even for misclassified failures. The copyback leg SHALL apply the same withheld-reference ruling: a redaction placeholder standing in for a copyback source reference is not a probeable reference and SHALL never reach the artifact existence probe; when copyback is required, the decision SHALL block with the distinct reason `copyback_source_withheld` (error code and stable classifier `COPYBACK_SOURCE_WITHHELD`, the placeholder itself carried as the artifact reference) rather than the determined-missing `missing_copyback_source` blocker, and a withheld copyback reference with no copyback requirement SHALL NOT block. The withheld-copyback blocker names a reference the public-read redaction boundary withheld: existence cannot be determined on that plane, and the blocker SHALL NOT enter the missing-forcing repair-authorization channel, whose forcing-rebuild remedy cannot clear a withheld reference; defining a clearing mechanism for it is deferred until a copyback write side exists to define one. The withheld ruling SHALL apply to the reference the alias resolution actually returned: when the first non-empty resolved value is a placeholder, the leg SHALL NOT continue scanning lower-priority aliases for a probeable substitute (a surviving unredacted echo is not a trustworthy stand-in for a withheld reference).

#### Scenario: Failure-state candidate with missing forcing blocks instead of retrying

- **WHEN** a candidate has a failure signal (failed pipeline status or
  failed hydro run) and its referenced forcing package is absent from
  the object store
- **THEN** the decision is blocked with reason
  `missing_forcing_package_uri` and the full artifact-guard evidence,
  satisfying the structural contract required by the explicit
  missing-forcing repair policy, and no forecast work is submitted

#### Scenario: Null journal provenance with a physically present package recovers instead of blocking

- **WHEN** a candidate has a failure signal, its restart stage is the
  forecast stage, the journal records null forcing provenance, and the
  object-store forcing-version sidecar record derived from the
  candidate identity names a package whose witnessed manifest file the
  existence probe finds present
- **THEN** the decision does not emit any missing-forcing blocker for
  that package, the recovery evaluation proceeds past the upstream
  artifact guard, and the evidence of the decision ultimately emitted
  for the candidate records the provenance source as the object-store
  sidecar tier

#### Scenario: Null journal provenance with a witnessed-absent package keeps the missing blocker

- **WHEN** the journal records null forcing provenance, the sidecar
  record names a package, and the existence probe finds the witnessed
  package manifest file absent from the object store
- **THEN** the decision is the unchanged stable missing-forcing
  blocker (reason `missing_forcing_package_uri`), with the provenance
  source marked as the sidecar tier, preserving the fail-closed
  semantics for a determined-missing package

#### Scenario: A production-scale sidecar record still yields a witness

- **WHEN** the sidecar record carries the per-station lineage the
  forcing producer writes in production, making it multiple megabytes,
  and its package manifest file is present
- **THEN** the sidecar tier reads and parses the record, the decision
  does not emit a missing-forcing blocker, and the record size alone
  never degrades the tier into a no-witness outcome

#### Scenario: No witness at any provenance tier blocks with the distinct row-absent reason

- **WHEN** a candidate has a failure signal, its restart stage is the
  forecast stage, and no forcing provenance witness exists at any read
  tier (no journal row, no journal direct file, and the sidecar record
  is absent, unreadable, malformed, oversized beyond the read limit,
  the probe object itself is unreadable, the object store is
  unconfigured, or the candidate identity is incomplete)
- **THEN** the decision is blocked with reason
  `forcing_version_row_absent` (artifact reference null in the guard
  evidence, provenance source marked absent with the tier-unavailable
  detail), regardless of the recorded failure code — including
  permanently-classified codes such as policy or manifest failures,
  whose original cause remains visible in the per-job evidence — the
  blocker keeps the same structural repair-eligible contract, and a
  manual retry request, which is evaluated before the guard, remains
  the operator escape hatch

#### Scenario: Repair authorization accepts both blocker reasons

- **WHEN** an operator authorizes the explicit single-cycle
  missing-forcing repair for a candidate blocked with either
  `missing_forcing_package_uri` or `forcing_version_row_absent`
- **THEN** the repair authorization channel — including its
  stable-classifier structural check — accepts the blocker and
  proceeds identically for both reason/classifier pairs, and the
  re-blocking echo path re-emits the decision token paired with the
  underlying blocker reason

#### Scenario: Permanently-classified failure with missing forcing remains repair-eligible

- **WHEN** the recorded failure code is non-transient (for example
  `ARTIFACT_NOT_FOUND`) and the referenced forcing package is absent
- **THEN** the decision is the same stable missing-forcing blocker —
  not a generic permanent-failure guard — so the single-cycle repair
  channel remains usable

#### Scenario: Task-produced classifier survives the DB-free path

- **WHEN** a DB-free SHUD array task fails with a classified runtime
  error and its per-task outcome receipt is readable in the object
  store
- **THEN** array accounting records that classifier for the task and
  the aggregation, and falls back to `NODE_FAILURE` only when the
  receipt is absent or unreadable

#### Scenario: Retry limit binds through the durable attempt suffix

- **WHEN** a forecast cohort job's identity carries a retry suffix at
  or beyond the configured retry limit
- **THEN** the in-stage retry gate refuses further resubmission and
  the next scheduler pass computes an exhausted retry policy instead
  of re-issuing the failure retry

#### Scenario: Withheld copyback reference is never probed and blocks with the distinct withheld reason

- **WHEN** a failure-state candidate's state carries a copyback source reference equal to a redaction placeholder (e.g. `[object-uri]`) and copyback is required (restart stage `copyback` or the state requires a copyback source)
- **THEN** the copyback leg SHALL NOT invoke the artifact existence probe for the placeholder
- **THEN** the decision SHALL block with reason `copyback_source_withheld` and error code `COPYBACK_SOURCE_WITHHELD`, never `COPYBACK_SOURCE_MISSING`
- **THEN** the artifact guard SHALL carry `artifact_type` `copyback_source` and the withheld placeholder as its artifact reference

#### Scenario: Withheld copyback reference without a copyback requirement does not block

- **WHEN** a failure-state candidate's state carries a redaction placeholder for its copyback source but copyback is not required
- **THEN** the copyback leg SHALL NOT probe the placeholder and SHALL NOT emit any copyback blocker

#### Scenario: Withheld copyback blocker stays outside the forcing repair channel

- **WHEN** a `copyback_source_withheld` blocker is evaluated by the stable missing-forcing blocker predicate
- **THEN** it SHALL NOT classify as a stable missing-forcing blocker, because the forcing repair authorization's rebuild remedy cannot clear a withheld copyback reference

### Requirement: Strict-warm-start terminal mismatch retries SHALL respect a stage-scoped budget

When the candidate ladder would emit `retry_strict_warm_start_terminal_init_state_mismatch` for a terminal-success candidate whose recorded init-state identity mismatches the strict warm-start resolution, the scheduler SHALL first evaluate the stage-scoped retry attempt against the configured retry limit. When the attempt has reached the limit, the scheduler SHALL emit the stable blocked decision `blocked_strict_warm_start_init_state_mismatch` carrying a retry-policy block (automatic retry not allowed, manual retry required, attempt, retry limit) instead of the retry decision, and the blocked decision SHALL NOT participate in forced terminal resubmission or replacement-retry scoping. When the attempt is below the limit, the retry decision and its evidence SHALL remain unchanged. The stage-scoped attempt SHALL bind in the production geometry where the reserved master row carries no retry count and attempts are recorded only as retry-suffixed pipeline job rows — **including the reverse geometry where the maximum-attempt retry-suffixed row is older than `job_limit` fresher rows of other stages**: on the file-journal candidate-state projection (the production path, which reads the cycle's rows unlimited before projecting), the projection SHALL derive, from the untruncated projection input, each canonical downstream stage's maximum effective retry attempt — through the same authoritative-stage-field and `effective_retry_attempt` chain the budget consumers use (including the `job_type` fallback for stage-less rows and the persisted-`retry_count` half for rows without a `_retry_<n>` suffix), never job-id substring parsing and never a locally forked stage-alias table — and carry these upper bounds across truncation so that the stage-matching row-scan component of stage-scoped attempt derivation over the truncated projection returns the true upper bound for every canonical downstream stage (the candidate-level flat retry-count aggregate remains window-sensitive exactly as before this change; its cross-stage contribution is a pre-existing behavior outside this guarantee). The carried upper bounds SHALL record their contributing rows' candidate-identity metadata, and candidate-identity/scope filtering SHALL narrow the carried upper bounds with the row population: a stage's upper bound survives a filtered state only while at least one of its contributing rows passes the same authority/scope predicates that filter the rows — an upper bound whose every contributor is judged non-authoritative for the candidate SHALL NOT reach that candidate's failure-policy, budget, or mint derivations. The truncated row selection itself SHALL remain the pure-freshness top-`job_limit` selection, element for element identical to the pre-change projection in every geometry — the carried upper bounds SHALL add no rows, evict no rows, and change no state key derived from the row population — and the stage-less flat-first attempt derivation SHALL remain byte-identical. The DB-backed candidate-state read path, which truncates in SQL upstream of the projection, is explicitly outside this guarantee (the shared projection computes the carried upper bounds over that path's `job_limit+1` window — a value-level improvement only; its row selection is likewise unchanged).

#### Scenario: Budget exhaustion demotes the retry to a stable blocked decision

- **WHEN** a completed cycle's candidate has a strict warm-start init-state mismatch and its stage-scoped retry attempt has reached the retry limit
- **THEN** the decision is `blocked_strict_warm_start_init_state_mismatch` with the retry-policy block, no forecast work is selected for resubmission, and the candidate is not re-selected on subsequent passes while the mismatch persists

#### Scenario: Below-budget behavior is unchanged

- **WHEN** the same mismatch is observed while the stage-scoped attempt is below the retry limit
- **THEN** the emitted decision and evidence are byte-identical to today's `retry_strict_warm_start_terminal_init_state_mismatch` shape

#### Scenario: The blocked decision is excluded from force-resubmit whitelists

- **WHEN** orchestration evaluates forced terminal resubmission and replacement-retry scoping against a candidate carrying the blocked decision
- **THEN** neither whitelist matches — their member sets are unchanged by this change — and no replacement submission occurs

#### Scenario: The budget binds against retry-suffixed journal rows

- **WHEN** the candidate state derives from a journal containing a reserved master row with zero retry count and stage-matching `*_retry_N` pipeline job rows mirroring the production wedge geometry
- **THEN** the stage-scoped attempt evaluates to at least N and the demotion triggers once N reaches the retry limit

#### Scenario: The budget binds in the reverse truncation geometry

- **WHEN** the `*_forecast_retry_N` row carrying the stage's maximum attempt is older than `job_limit` fresher rows of other stages and `N` has reached the retry limit
- **THEN** the truncated file-journal projection still yields stage-scoped attempt `N` and the demotion to `blocked_strict_warm_start_init_state_mismatch` triggers instead of an unbudgeted retry

#### Scenario: Projection row selection is unchanged in every geometry

- **WHEN** any input geometry is projected — including the reverse geometry, a geometry whose only completed-stage success row sits at the old end of the freshness window, a geometry with a stale active-status row outside the window, and a geometry whose flat `retry_count` carrier sits inside the window
- **THEN** `pipeline_jobs` equals the freshness-ordered top-`job_limit` selection element for element, and every state key derived from the row population (`pipeline_status`, `failed_stage`, `restart_stage`, completed-stage evidence, active-job scanning, the flat `retry_count` aggregate, `latest_job` derivation) is identical to the pre-change projection

#### Scenario: Zero-attempt stages carry no upper bound

- **WHEN** a canonical stage's maximum effective attempt in the input is zero
- **THEN** the carried upper bounds contain no entry for that stage and stage-scoped derivation still returns zero

#### Scenario: Upper bounds derive through the consumer chain on degenerate row shapes

- **WHEN** the input carries an out-of-window maximum-attempt `copyback` row, out-of-window `download` rows, a row whose attempt lives only in its persisted `retry_count` (no `_retry_<n>` suffix), and a row whose stage lives only in `job_type`
- **THEN** the `copyback`, persisted-`retry_count`, and `job_type`-only upper bounds are all carried (canonical stages via the consumer chain) while `download` rows contribute no upper bound (not a canonical downstream stage), matching the consumer-side canonical-stage table rather than any locally forked alias table or job-id substring parsing

#### Scenario: Carried upper bounds narrow with identity filtering

- **WHEN** a stage's only upper-bound contributor is a row that candidate-identity filtering judges non-authoritative for the candidate (for example a model-less suffixed cohort row) and the filtered row population drops it
- **THEN** the filtered state carries no upper bound for that stage, and the candidate's own first failure classifies exactly as it did before this change — retriable, attempt zero — instead of inheriting the foreign row's attempt

#### Scenario: The strict-warm-start budget reads the narrowed upper bounds

- **WHEN** the strict-warm-start terminal-mismatch budget evaluates a candidate whose only upper-bound contributor for the forecast stage is a non-authoritative cycle-cohort row truncated out of the window with its attempt at the retry limit
- **THEN** the budget derivation sees no upper bound for that stage — the raw projected state is narrowed by the same authority predicates before the read — and the decision remains the retry decision instead of the blocked demotion

#### Scenario: Tied contributors keep the upper bound alive through filtering

- **WHEN** a stage's maximum attempt is carried by two tied contributing rows outside the window — a fresher row that identity filtering judges non-authoritative and an older candidate-authoritative row
- **THEN** the filtered state still carries the stage's upper bound at that maximum, because at least one contributor survives the predicates

#### Scenario: The failure policy binds to the carried upper bound

- **WHEN** the reverse truncation geometry carries a stage upper bound `N` at the retry limit and the candidate's own in-window row fails with a transient error code on that stage
- **THEN** the failure policy reports attempt `N` with automatic retry disallowed and permanent/limit-exhausted set — the intended decision-level consequence of truthful attempt derivation — where the pre-change projection reported attempt zero and a retriable failure

#### Scenario: Manual retry mints from the carried upper bound when the stage is nameable

- **WHEN** the failed stage is nameable from the filtered state (a candidate-authoritative failed or cancelled row sits inside the window) while the stage's maximum-attempt row `N` sits outside the window, and an adopted manual-retry marker carries no explicit attempt
- **THEN** the mint derives `previous_attempt == N` and `new_attempt == N+1` instead of the pre-change window-local value

#### Scenario: Stage-less flat-first derivation is unaffected

- **WHEN** the stage-less attempt derivation runs against a projected state carrying non-empty stage upper bounds
- **THEN** it returns the flat-first value exactly as before — the carried upper bounds never leak into stage-less reads

### Requirement: Cycle-granularity manual retry markers require model attribution and cycle-scope markers cannot pin candidate attempts

A cycle-granularity manual retry marker SHALL be adopted by a model
candidate only with explicit model attribution: events of
`entity_type` `forecast_cycle` whose `details` (or event top level)
carry no `model_id` matching the candidate are not adopted by any
model candidate (fail-closed); an explicit matching `model_id` makes
exactly the named candidate adopt it. All other manual retry markers
— job-targeted events, events without an entity reference, and
cycle-scope job events — keep their existing adoption semantics, so
operator manual retries of cycle-level stages remain effective for
the cycle's candidates. Separately, the `retry_count` of a marker
that resolves to a cycle-scope pipeline job — a model-less job row
whose `run_id` carries the `cycle_<source>_<stamp>` grammar (a
model-less row with a candidate-run `fcst_...` id is NOT
cycle-scope) — SHALL pin the derived attempt only when that cycle
stage's failure is the repair target: the resolved job is still a
LIVE failure AND either the state's failed stage equals the
resolved job's stage or the candidate has no live candidate-scoped
failure of its own. The marker-target test and the candidate-scope
scan read the SAME row-level live-failure domain (the row-absent
arm for unresolvable marker entities reads the target row's
write-time shape off the MARKER'S OWN RECORD when the marker
carries it, reconstructing the target and running the same
resolved-row ROUTING over the reconstruction — a model-bearing
record short-circuits to a pin exactly as the router does, a
model-less record runs this row-level domain; only markers
written without that record fall back to state-level staleness
evidence alone, and only the target's POST-WRITE fate outside the
two state mappings remains a disclosed divergence — see the
record-borne scenario below): a status in the failure half of the
module's blocker STATUS domain — the failed-pipeline statuses plus
`cancelled`, a `cancelled` row being a first-class manual-retry
repair target on the marker side exactly as it is a live failure
on the candidate side — excluding ACTIVE statuses, with repaired
stage-evidence rows and unsubmitted auto-retry placeholders never
counting; the marker-target test and the candidate-scope scan
derive from one shared row predicate so the two sides cannot
drift. The candidate-side live-failure domain matches that same
failure half of the module's blocker STATUS domain, not the
narrower failed-pipeline status set alone — read from candidate-scope job
rows and the candidate's own hydro run only (the blocker scan's
state-level `pipeline_status` and pipeline-event sources are not
live-failure sources here: a top-level failed `pipeline_status`
records the cycle failure being repaired, and counting it would
make the only-failure-left arm unreachable; the module enforces
this exclusion itself with a row-identity predicate — an id-less
synthesized row (every legitimate production row carries a job id
on both read paths) is never a live-failure source, closing the
top-level `pipeline_status` leak on job-row-less synthesized and
compacted states, while the production projections' shape
guarantee — a surviving marker proves real job rows exist beside
it — remains as defence in depth; id-bearing rows, including the
single-mapping and flattened historical state shapes that embed a
job id, derive exactly as before — a flattened state that embeds
both a job id and a failing status still reads as a live failure,
exactly as today): a
candidate-scope (non-cycle-scope) job row in a failed or
`cancelled` status counts, and so does a hydro run whose status is
`failed`, `cancelled`, or `permanently_failed`; repaired
stage-evidence rows and unsubmitted auto-retry placeholders (rows
whose status is `pending` or `submission_failed` by that
placeholder's own definition) never count as live failures — a
placeholder-shaped row in a `cancelled` status is outside the
placeholder gate and counts, exactly as the blocker scan treats
it. In every other case — a candidate-scoped live failure
(pipeline failed or cancelled, or hydro) where the failed stage
does not name the resolved job's stage, or a marker whose resolved
job is no longer a live failure (stale — resolved/succeeded, still
ACTIVE, repaired stage evidence, or an unsubmitted auto-retry
placeholder; NOT a `cancelled` row, which stays a valid marker
target) — the derived
`new_attempt` falls back to
the candidate's own `previous_attempt + 1`, and the
attempt-derivation scan is terminal at the newest adopted marker:
absent a state-level manual-retry attempt payload (a top-level
`manual_retry` — or, by the same gate, `manual_retry_marker` —
mapping's `new_attempt`/`retry_count` short-circuits ahead of the
event scan; its semantics are outside this rule and unchanged by
it), that marker alone decides, whether or not it
carries a `retry_count` — a newest adopted marker whose
`retry_count` is absent or empty makes no operator attempt claim
and SHALL yield the same fallback instead of a walk-back to any
older marker's `retry_count` — so older adopted markers are never
consulted, while a newer marker-shaped event that is NOT adopted
by the candidate neither decides nor terminates the scan. Neither a refused pin
nor an absent attempt claim re-mints a
consumed attempt number: whenever the fallback's attempt derivation
resolves no canonical failed stage, it floors `previous_attempt` at
the candidate's own stage-scoped attempt record for each stage in
the restarted-stage family — the stages of the candidate's own live
candidate-scope failures (a row the live-failure exclusions above
exclude contributes no stage to the family) plus the canonical
forecast stage when the hydro run is the live failure. Within a
family stage that is itself a canonical downstream restart stage
the floor uses the same stage-scoped derivation the
resolved-stage path uses, counting id retry suffix or recorded
retry count regardless of row status — a repaired `_retry_3` row at
a family stage still proves attempt 3 was spent — while a consumed
suffix at a stage outside the family (a cross-stage forcing row or
a cohort stage counter) never contributes to this floor (the floor
only raises the caller's value). At a family stage that is NOT a
canonical downstream restart stage the floor uses the same
stage-scoped derivation by the row's raw authoritative stage name:
non-cycle-scope rows whose stage field equals the family stage
contribute their durable attempt through the same
effective-attempt chain, while model-less cycle-scope rows at that
same raw stage name (a cohort download counter) never contribute —
the non-canonical arm carries the candidate-scope discipline from
its first day, and the canonical arm's derivation is unchanged
byte for byte. The non-canonical arm reads only the projected row
window: the truncation-carried per-stage upper bounds cover
canonical downstream stages only, so a non-canonical stage's
maximum-attempt row truncated out of the `job_limit` window is not
restored — an explicit boundary of this rule. In the
unnameable-stage case a candidate whose visible family-stage row
already consumed attempt N therefore derives at least N + 1
whether or not that family stage is canonical; with no live
failure at all the fallback stays `previous_attempt + 1`. The same
non-canonical derivation reaches the scheduler's cross-pass
failure policy, not only the fallback floor: a candidate whose own
non-canonical row consumed attempt N is classified against N
instead of the reset flat count, so at a `retry_limit` of N or
below that classification reports the limit exhausted and the
candidate is permanently blocked where it was previously retried —
the configured limit binding retries even for misclassified
failures, as this capability's effective-attempt requirement
already mandates for the cross-pass failure policy. Marker-shaped events remain excluded from
blocker scanning regardless of attribution (a foreign marker must
never be treated as an active blocker suppressing the candidate's
own manual retry), and candidate-state event-row visibility on the
journal/DB read paths is unchanged for cycle-granularity markers and
model-less cycle-scope rows — those cycle-wide events stay visible in
every candidate's raw state for diagnostics. Candidate-state
membership for pipeline job rows on the journal (db-free) read path
SHALL align with the DB read path's candidate-state predicate: a row
whose run id is the candidate's own run id belongs to the candidate;
a row whose run id carries the cycle-scope run grammar belongs to the
candidate only when its `model_id` is empty (the model-less cohort
contract — such rows stay visible to every candidate in the cycle,
including the journal-only widening to rows whose run id extends the
cycle run id with a suffix, which this rule leaves in place) or names
the candidate itself; a row naming a foreign `model_id` is excluded
from the candidate's job rows, and a `pipeline_job`-entity event
resolving to an excluded row SHALL leave the candidate's event table
in the same filtering step as its row — an orphaned marker whose row
was excluded but whose event survived would re-enter the pinning
decision through the unresolvable cycle-scope entity grammar — so a
foreign model's manual retry marker can neither report
`manual_retry_requested` nor pin the candidate's derived
`new_attempt`. The candidate-state membership exclusion and the
cycle-level gates draw different lines: the duplicate-submission
gates (the active-pipeline and active-slurm-jobs scans) keep their
wider unconditional cycle-run visibility unchanged — the DB read
path's counterparts deliberately share that wider visibility — but
the completed-pipeline gate answers a candidate-scoped question
("has THIS candidate completed") and SHALL NOT count a
foreign-model named cycle-run row as the candidate's completion:
its job-row conjunction excludes a row whose `model_id` is
non-empty and names another model while its run id is exactly the
cycle run id, so completion is proven only by the candidate's own
rows (its own run id or its own `model_id`), by model-less
cycle-scope cohort completion rows (which stay cycle-wide — every
candidate completes through them), or by the candidate's own
completed hydro run. This aligns the journal verdict's direction
with the DB completed-pipeline gate, which reads `hydro.hydro_run`
under a source/cycle/model three-key restriction and never sees
another model's job rows; the exclusion lives in the
completed-pipeline gate's own conjunction, not in the shared
row-match predicate that feeds the duplicate-submission gates. On the
identity-filtered
decision state, preserving the attribution predicate fields makes a
self-declared MATCHING `model_id` a retention credential for a
non-authoritative marker event under shared-cycle scoping (foreign
model ids stay excluded; within one source-cycle aggregate a model id
maps to exactly one candidate), so a candidate-own marker that
sanitization previously stripped to anonymity is now retained and can
drive the retry decision it was written to request.

#### Scenario: Foreign-model named cycle-run_id row cannot enter the candidate state or pin its attempt

- **WHEN** on the journal (db-free) read path another model's
  pipeline job row is recorded with `run_id` equal to the cycle run
  id (`cycle_<source>_<stamp>`) and a non-empty `model_id` naming
  that other model, carrying `retry_count` 5, a manual retry event of
  `entity_type` `pipeline_job` targets exactly that row, and the
  candidate's own failed forecast row carries `retry_count` 0
- **THEN** the candidate's state contains neither the foreign model's
  job row nor the event targeting it, `manual_retry_requested` stays
  false from that marker, and the derived `new_attempt` is
  `previous_attempt + 1` (1 from 0) — not the foreign marker's 5
- **AND** a model-less row with the same cycle-scope run id, or with
  a run id extending it by a suffix, remains visible to every
  candidate in the cycle
- **AND** the candidate's own row with `run_id` equal to the cycle
  run id and the candidate's own `model_id` remains visible, and a
  marker targeting it keeps its adoption and pinning semantics
- **AND** the DB read path gives the same candidate-state membership
  verdict for these rows — the foreign-model named row is excluded
  there by the `model_id IS NULL` guard on its cycle-run clause and
  the candidate's own named row is included by its model clause —
  while the suffix-extended model-less row remains a journal-only
  widening that this change leaves in place
- **AND** with the foreign-model row and its marker in place, the
  cycle-level duplicate-submission gates (active-pipeline,
  active-slurm-jobs) answer exactly as before the exclusion — the
  row stays visible to those scans — while the completed-pipeline
  gate applies its own candidate-scoped conjunction (see the
  completion-gate scenario below)

#### Scenario: Foreign-model named cycle-run_id completion row does not complete the candidate

- **WHEN** on the journal (db-free) read path another model's
  pipeline job row is recorded with `run_id` equal to the cycle run
  id (`cycle_<source>_<stamp>`), a non-empty `model_id` naming that
  other model, `status` `succeeded`, and a completion stage —
  `state_save_qc`, `publish`, or `parse` under the default terminal
  contract, or `state_save_qc` under the production
  `forecast_state_save_qc` terminal contract
- **THEN** `has_completed_pipeline` answers `False` for every other
  candidate of the cycle in all of those stage/contract
  combinations — another model's completion is never this
  candidate's completion
- **AND** when the candidate's own hydro run is recorded with
  `status` `failed`, `cancelled`, or `created` and the candidate's
  own forecast row is `failed`, the foreign completion row still
  cannot flip the candidate's verdict to `True`
- **AND** a model-less cycle-scope cohort completion row (`run_id`
  equal to the cycle run id or extending it with a suffix, empty
  `model_id`) keeps answering `True` for every candidate of the
  cycle whenever its stage is a terminal completion stage under the
  active contract, and the candidate's own completion evidence
  keeps answering `True` on the same terms — its own named
  cycle-run row and its own-run-id rows at a terminal completion
  stage under the active contract, and its own completed hydro run
  under the default contract (the production
  `forecast_state_save_qc` contract derives completion from
  pipeline job rows alone and never consults the hydro completion
  arm)
- **AND** on the same fixture the active-pipeline and
  active-slurm-jobs answers are byte-for-byte unchanged, proving
  the shared row-match predicate was not narrowed
- **AND** the DB read path already answers `False` for the foreign
  shape — its completed-pipeline gate reads `hydro.hydro_run` under
  the source/cycle/model three-key restriction — so the journal and
  DB verdicts now agree in direction for this shape instead of
  diverging

#### Scenario: Unattributed cycle-granularity marker is fail-closed with an explicit escape

- **WHEN** a manual retry event of `entity_type` `forecast_cycle`
  exists in a cycle shared by several model candidates and carries
  no `model_id` in its `details` or at the event top level
- **THEN** no candidate reports `manual_retry_requested` from that
  marker
- **AND** if the same event explicitly names one candidate's
  `model_id` and that candidate's state carries at least one
  model-scoped job row (the derived model set is non-empty), exactly
  that candidate adopts it
- **AND** the gate holds on the identity-filtered decision state:
  event sanitization preserves the attribution predicate fields
  (`entity_type`, top-level and details `model_id`) so the
  fail-closed test and its explicit escape behave identically on the
  raw and filtered state
- **AND** the preserved `model_id` doubles as a shared-cycle
  retention credential on the decision state: a non-authoritative
  marker event self-declaring the candidate's own `model_id` is
  retained (and may flip the candidate's decision from a terminal
  guard to the requested retry), while one declaring a foreign
  `model_id` — or none — is dropped as before

#### Scenario: Cycle-scope job marker pins only when its stage is the repair target

- **WHEN** a manual retry event targets a cycle-scope pipeline job
  (`model_id` empty and `run_id` in the `cycle_<source>_<stamp>`
  grammar) that is still a live failure — a failed-pipeline or
  `cancelled` status, not ACTIVE, and not a repaired stage-evidence
  row or unsubmitted auto-retry placeholder — and that cycle stage's failure
  is what the candidate decision repairs — the failed stage matches
  the job's stage, or the candidate has no live candidate-scoped
  failure of its own (the production cohort-download shape)
- **THEN** the derived `new_attempt` pins the marker's
  `retry_count`, so the operator's cycle-level manual retry stays
  effective and the minted retry identity does not reuse a consumed
  attempt number
- **AND** a `cancelled` cycle-scope marker target pins exactly as a
  failed one does: with the marker's `retry_count` 5 and the
  candidate's `previous_attempt` 0, both the same-stage arm (failed
  stage `download` beside the candidate's own failed forecast) and
  the only-failure-left arm (no failed stage, own jobs all
  succeeded) derive `new_attempt` 5, and the manual-retry payload
  carries `new_attempt` 5; this holds even when the `cancelled`
  target row is placeholder-SHAPED (a retry-suffixed id with no
  Slurm id) — the placeholder gate is status-bound to
  `pending`/`submission_failed`, so a cancelled or failed
  placeholder-shaped row is outside the gate and stays a valid
  pinning marker target, exactly as the candidate-side scan counts
  it
- **AND** the candidate-state projection SHALL produce the repaired
  annotations (`repair_status`/`active_blocker`) and
  `repaired_stage_evidence` over that same repair-target status
  domain — the failed-pipeline statuses plus `cancelled` — so the
  stale-target refusal above is producible for every status the
  marker-target test reads: a `cancelled` row repaired by a later
  succeeded retry carries the annotations exactly as a failed one
  does, and every projection surface the widened domain makes
  reachable for `cancelled` rows behaves exactly as its `failed`
  twin already did (cancelled↔failed parity), including the
  active-failure exposure of an unrepaired cancelled cycle row and
  the evidence-selection paths a repaired cancelled row enters
- **AND** when the candidate's own live failure is at a different
  stage, or the marker's resolved job is no longer a live failure
  (stale — resolved/succeeded, ACTIVE, repaired stage evidence, or
  an unsubmitted auto-retry placeholder),
  the derived `new_attempt` falls back to `previous_attempt + 1`;
  the pin refusal itself charges nothing, and the attempt and
  failure-policy consumers resolve the candidate's own failed stage
  through a candidate-scoped derivation whose ROW SCAN skips
  cycle-scope rows — a multi-basin cohort row's stage never becomes
  the candidate's failed-stage axis through that scan, so the
  cohort's persisted retry counter is not charged to the
  candidate's attempt or retry-limit budget through it. Two
  channels stay outside that narrowing as declared boundaries of
  this rule: the explicit top-level stage-key branch is unchanged
  and can still be cast cycle-wide (from an active source-cycle
  download failure, or a `restart_stage` minted from
  completed-stage evidence scanned over the unfiltered rows), and
  the download acceptance this capability already carries depends
  on that branch; and the canonical stage-scoped derivation keeps
  its existing count of model-less cohort rows at the same
  canonical stage byte for byte (tracked separately in #1586). The
  stage-less flat-first derivation on a state carrying no flat
  retry record likewise still maxes over every row's recorded
  count — a window-sensitive pre-existing channel outside this
  rule, tracked with the flat-component boundary in #1579; the
  projection always writes a top-level `retry_count`, but the
  identity filter's top-level strip removes it and re-attaches the
  rows afterwards, so that channel remains reachable on the
  decision path. When the candidate itself has no nameable
  live failure the candidate-scoped derivation resolves no stage
  and those consumers fall back to the flat and family-floor paths,
  while the restart-routing and downstream-evidence consumers keep
  the unscoped derivation unchanged
- **AND** the candidate's own live failure that blocks the pin
  includes a `cancelled` model-scoped job row (a cancelled forecast
  with a cross-stage cycle-download marker of `retry_count` 5
  derives `new_attempt` 1 from `previous_attempt` 0, not 5) and a
  failed, cancelled, or permanently failed hydro run beside
  all-succeeded job rows (`previous_attempt` 2 derives 3, not 5) —
  the FAILURE half of the blocker scan's status domain only: an
  ACTIVE in-flight row (`pending`/`queued`/`submitted`/`running`)
  or an ACTIVE hydro run is not a repair target and never blocks
  the pin
- **AND** the refused pin's fallback floor comes from the durable
  record of the restarted stage family whenever no canonical failed
  stage resolves: a cancelled own forecast row whose job id carries
  the consumed `_retry_2` suffix (master `retry_count` reset to 0
  by the journal's clean-reservation invariant, no usable
  `failed_stage`) derives `new_attempt` 3 — not 1 (a replay of a
  consumed identity that would silently skip submission at the
  reservation boundary) and not the marker's 5 — and a consumed
  suffix at a stage outside the family (an own forcing `_retry_7`
  row, or a single-basin cohort `download`/`convert` counter)
  leaves that derivation untouched, while the emitted
  `previous_attempt` evidence fields keep reporting the unfloored
  stage-scoped derivation (only the derived `new_attempt` carries
  the floor)
- **AND** a repaired stage-evidence row or an unsubmitted
  auto-retry placeholder is not a live failure and does not block
  the pin, while a placeholder-shaped row in a `cancelled` status
  falls outside the placeholder gate and blocks the pin exactly as
  it blocks the blocker scan (same domain, same exclusions)
- **AND** the fallback is terminal even when the candidate has an
  older own-model marker: the stale marker's `retry_count` does not
  leak into `new_attempt`
- **AND** a model-less job row carrying a candidate-run `fcst_...`
  id is not cycle-scope — a marker targeting it keeps pinning
  `new_attempt` to its `retry_count`
- **AND** the rule survives candidate-state filtering with
  equivalent evidence: a marker whose entity cannot be resolved to
  any job row but whose entity id carries the cycle-scope
  pipeline-job grammar (`job_cycle_<source>_<stamp>_...`, the shape
  left behind when a non-authoritative cohort master row is dropped
  from the decision state or truncated from the row window) and
  that does NOT carry its target's write-time record (a marker
  WITH the record decides through the record-borne routing in its
  own scenario below) pins the
  candidate's attempt exactly when the id's cycle is the candidate's
  own cycle AND the marker's recorded stage is the repair target —
  the stage evidence is the marker's own `failed_stage` detail,
  with the id's stage token (read after stripping every stacked
  `_retry_<n>` suffix) as the backstop for markers written before
  the detail existed — AND neither the state-level repaired-stage
  evidence (its original failed job id) nor the state-level
  completed-stage evidence (its job id) names the marker's target
  (exact id comparisons — the staleness refusals delivered with the
  evidence the row-absent path actually has); a surviving marker
  whose stage is NOT the repair target falls through to the
  only-failure-left arm (the same widened live-failure domain, so a
  cancelled own row or a failed hydro run blocks this pin too)
  instead of refusing outright — so an operator's manual retry of
  the candidate's own cohort cycle stage stays effective even
  though the row is invisible and even on a retry-suffixed id,
  while a foreign-cycle counter or a stale repaired target still
  never pins the candidate's attempt; markers with other
  unresolvable entity ids keep their existing pinning behavior

#### Scenario: Unresolvable cycle-grammar marker pins with marker-record evidence

- **WHEN** a manual retry marker's entity cannot be resolved to any
  job row, its entity id carries the cycle-scope pipeline-job
  grammar with one or more stacked `_retry_<n>` suffixes
  (`job_cycle_<source>_<stamp>_<stage>_retry_1`, or the three-layer
  production shape `..._retry_1_retry_2_retry_3`), the id's cycle
  is the candidate's own, and the state's failed stage equals
  `<stage>`
- **THEN** the pin holds through BOTH row-absence mechanisms
  (identity-filter cohort deletion, and row-window truncation past
  a newer same-stage row) — stacked suffixes do not defeat the
  stage evidence, whether it comes from the marker's recorded
  `failed_stage` detail or from the loop-stripped id token backstop
- **AND** every cross-arm equivalence claim in this scenario reads
  within the DELIVERED DOMAIN, which is now split by what the
  marker itself recorded: a marker carrying its target's write-time
  record (the `target_*` details below) is decided by
  reconstructing the target from that record and running the SAME
  resolved-row routing over the reconstruction — a model-bearing
  target pins unconditionally exactly as the resolved-row router
  does, and a model-less target answers through the cycle-scope pin
  rule's shared row-level live-failure domain (placeholder and
  repaired-flag exclusions included) — so on everything the record
  captures the two arms are the same rule by construction; a marker
  WITHOUT the record keeps the previous delivered domain (failed-
  status targets that are neither unsubmitted auto-retry
  placeholders nor repaired-flagged, or targets a state mapping
  names with an exact entity-id match); the divergence classes
  left OUTSIDE both are the target's POST-WRITE fate beyond the
  two state mappings and the write-time shapes the record cannot
  carry (the projection-annotation keys the current writer never
  produces, #1482), enumerated as a permanent limitation below
- **AND** the journal marker event written by a manual repair
  carries the failed job's stage as a `failed_stage` detail AND the
  target row's write-time shape as `target_status`,
  `target_repair_status`, `target_active_blocker`,
  `target_model_id`, `target_slurm_job_id`, `target_retry_count`,
  `target_manual_retry_marker`, and `target_array_task_id` details
  — a key set that closes over EVERY row field the shared
  live-failure predicate's transitive closure reads (the
  placeholder predicate alone reads six; `target_repair_status`
  and `target_active_blocker` are gate-contract keys the CURRENT
  writer never fills — those flags are projection-time annotations
  absent from the persisted rows it reads, #1482), with key names
  chosen to
  avoid the candidate-state record-stage reader's
  `stage`/`job_type` keys and the attribution reader's `model_id`
  key (the target's model is a different semantic axis from the
  marker's attributed model), zero and false being recorded values
  rather than absences — all preserved by the
  identity-filter event sanitizer on retry events — so markers
  written from now on decide by record rather than id text
  wherever their details survive to adoption (on the journal read
  path the completion-stage compaction drops those details
  wholesale for model-less cycle-scope queue events at the
  completion stages, which un-adopts the marker event entirely
  rather than falling back to id text — the pin gate's journal-path
  live domain is the submission stages)
- **AND** for a model-less target, a marker whose target the
  state-level repaired-stage evidence names as its original failed
  job — or whose target the state-level completed-stage evidence
  names as its completed job — refuses the pin with the row absent
  exactly as the resolved-row rule refuses it with the row present
  (a model-bearing record short-circuits past both mappings,
  exactly as the resolved-row router does); those two mappings,
  plus the marker's own write-time record, are the row-absent
  staleness surfaces — and the target's POST-WRITE fate outside
  the two mappings is a PERMANENT LIMITATION, disclosed rather
  than delivered: a target that succeeded after the marker was
  written and was evicted from the completed-stage evidence by a
  later-stage winner, or whose success projected through the
  repaired-copy branch (no `job_id` key), or whose stage has no
  successor in the forecast stage order (`download`,
  `state_save_qc`, `publish` queue targets), or that was repaired
  after write without the repaired-stage evidence naming it, or
  that was already ANNOTATED repaired at write time (the
  projection-time annotation never reaches the persisted rows the
  writer reads), still
  pins here where the resolved-row rule would refuse — the
  completed-stage evidence producer is not widened to those stages
  because that mapping also drives restart routing; a target
  re-activated after write (resubmitted out of a non-terminal
  failure status back into the ACTIVE domain) belongs to the same
  limitation wherever that transition is producible
- **AND** with the marker's stage differing from the candidate's
  failed stage, a marker WITHOUT the record falls through to the
  only-failure-left arm — the same arm the resolved-row rule uses
  on a stage mismatch — and, within the delivered domain, for a
  failed-status target lands on the same verdict as the
  resolved-row rule on the same state (a model-bearing record
  pins on the stage mismatch itself, router parity)
- **AND** markers with non-cycle-grammar entity ids keep the
  historical fail-open, a foreign-cycle id still never pins, and a
  stage-less marker keeps deciding through the loop-stripped id
  token — a TEXT inference, not recorded evidence, capped to the
  legacy set of markers written before the `failed_stage` detail
  existed PLUS the half records the current writer still produces
  when the target row carries no stage (the empty value is not
  written and the sanitizer does not pass empties through): the
  token's stage may not be the stage the target row actually
  carried, and that ceiling is pinned as accepted behavior, not
  closed

#### Scenario: Record-borne target evidence gives the row-absent arm resolved-row parity

- **WHEN** a manual retry marker written by `record_manual_repair`
  carries its target's write-time record (`target_status` and the
  marker's `failed_stage` detail both present — a half record
  missing either falls back to the delivered backstop arm, id-token
  inference included; the remaining `target_*` keys present when
  the target row carried them) and the marker's target row is
  absent from the decision state (identity-filter deletion or
  row-window truncation)
- **THEN** for a MODEL-LESS record the pin verdict equals the
  resolved-row router's verdict on a row of exactly the recorded
  shape: an unsubmitted auto-retry placeholder record
  (`pending`/`submission_failed` status, `_retry_<n>` id, positive
  `target_retry_count`, no marker flag, no slurm or array id)
  refuses the pin, a repaired-flagged record
  (`target_repair_status` repaired or `target_active_blocker`
  false) refuses the pin — those two flags are projection-time
  annotations that never reach the persisted rows the
  manual-repair writer reads, so like the success values below
  this is the gate's contract on the record, not a shape the
  current writer produces; a target already annotated repaired at
  write time therefore still pins through its record, a disclosed
  permanent limitation alongside the post-write fates — and a
  record whose status is not in the
  live-failure domain (a succeeded
  `download`/`state_save_qc`/`publish` queue target included —
  no dependence on the completed-stage evidence, whose producer
  never names those stages; such success values lie outside the
  manual-repair writer's own source domain and this clause is the
  gate's contract on the record, not a claim the writer produces
  them) refuses the pin — each exactly as the row-present twin
  refuses the same shape
- **AND** a model-bearing record whose `target_model_id` names the
  candidate's OWN model — read off the tail of the state's own
  candidate run id (`fcst_<source>_<stamp>_<model_id>`, everything
  after the stamp, model ids carrying underscores of their own),
  never derived from the surviving job rows, so row-window
  truncation cannot blind the comparison — pins unconditionally,
  cross-stage and same-stage alike, even when a state staleness
  mapping names the target — exactly as the resolved-row router
  short-circuits a model-bearing row to a pin — so the
  operator-pinned `retry_count` is honored on both sides, while a
  record naming any OTHER model, or a state whose run id yields no
  model, fails closed and never pins
- **AND** the verdicts hold for stacked-suffix entity ids
  (`..._retry_1` and `..._retry_1_retry_2_retry_3` alike)
- **AND** a marker without the record — legacy markers, and every
  marker written by the SQL retry service — keeps the delivered
  backstop verdicts bit for bit
- **AND** the identity-filter event sanitizer preserves the
  `target_*` details on retry events end to end: a marker written
  by `record_manual_repair`, projected into the candidate state and
  filtered onto the decision state, still carries them at the pin
  gate

#### Scenario: Newest adopted marker without retry_count terminates the attempt scan

- **WHEN** the candidate's events contain an older adopted
  own-model marker carrying `retry_count` N whose pin would
  otherwise hold, followed by a newer adopted marker whose
  `retry_count` is absent or the empty string (a cross-stage
  cycle-granularity marker, or a marker written without the
  field), and the candidate's `previous_attempt` is N
- **THEN** the derived `new_attempt` is the fallback
  `previous_attempt + 1` — floored by the restarted-stage-family
  rule exactly as every other fallback arm — and never the older
  marker's consumed N
- **AND** the manual-retry payload reports the retry as requested
  from the newest marker and carries no `new_attempt` claim: the
  payload scan and the attempt-derivation scan terminate at the
  same newest adopted marker
- **AND** a newest adopted marker that itself carries a pinning
  `retry_count` keeps deciding with its value exactly as before,
  and a state with no adopted marker at all keeps its existing
  fallback semantics
- **AND** a marker-shaped event newer than the candidate's own
  pinning marker but NOT adopted by the candidate (for example a
  foreign-attributed marker) neither terminates the scan nor
  decides — the candidate's own newest adopted marker still pins
  its `retry_count`
- **AND** when the terminal fallback fires on the shape where no
  canonical failed stage resolves (a cancelled own row whose job id
  carries a consumed `_retry_<n>` suffix), the restarted-stage-family
  floor applies exactly as on the other fallback arms — the
  derivation returns the floored value, not a bare
  `previous_attempt + 1` replay of a consumed identity
- **AND** on a state carrying no state-level manual-retry attempt
  payload (no top-level `manual_retry` or `manual_retry_marker`
  mapping whose `new_attempt`/`retry_count` value is neither `None`
  nor `""`), absent an adopted marker whose `retry_count` pins (an
  operator's explicit attempt claim), the derivation never returns
  a value at or below `previous_attempt`

#### Scenario: Own-model markers and blocker exclusion keep their semantics

- **WHEN** a manual retry event targets one of the candidate's own
  model-scoped jobs, or a foreign marker-shaped event (e.g.
  `status_to` `pending`) coexists with the candidate's own marker
- **THEN** the own-model marker is adopted unchanged with
  `new_attempt` matching its `retry_count`, and the foreign
  marker-shaped event is not treated as an active blocker — the
  candidate's `manual_retry_requested` remains truthful

#### Scenario: A non-canonical family stage keeps its consumed attempt

- **WHEN** the candidate's only live candidate-scope failure is a
  model-scoped `cancelled` `download` row whose id carries a
  consumed `_retry_4` suffix, the state's flat `retry_count` is 0,
  and no failed stage is resolvable
- **THEN** the fallback attempt derivation floors at that row's
  stage-scoped attempt and mints `new_attempt` 5 instead of
  re-minting the consumed attempt 1

#### Scenario: The non-canonical arm never reads cycle-scope rows

- **WHEN** a model-less cycle-scope `download` row persisting
  `retry_count` 7 coexists with the candidate's own model-scoped
  `download_retry_4` row and the family resolves the `download`
  stage
- **THEN** the stage-scoped derivation reads 4 — the cohort
  counter contributes nothing to the candidate's floor

#### Scenario: A synthesized id-less row is not a live failure

- **WHEN** a state carries a failing top-level `pipeline_status`
  (`cancelled`, `failed`, or `permanently_failed`), no job rows
  (`pipeline_jobs` missing or empty), and an adopted marker whose
  `retry_count` is 5
- **THEN** the operator pin is honoured with `new_attempt` 5 —
  the synthesized row derived from the state's own top-level
  fields never closes the only-failure-left arm

#### Scenario: A cohort stage counter never charges the candidate's budget

- **WHEN** a multi-basin cycle's model-less cohort row at a
  canonical stage persists `retry_count` 7 (with or without a
  marker minted over it), the candidate's own live failure is a
  `cancelled` forecast row with a consumed `_retry_2` suffix, and
  the projected state resolves no top-level failed stage
- **THEN** the manual-retry attempt derivation yields 3 (the
  candidate's own record plus one) rather than inheriting the
  cohort's counter, and the automatic-retry policy classifies the
  candidate against its own attempt — a first-failure candidate is
  not demoted to `retry_limit_exhausted` by the cohort's counter

### Requirement: Forcing provenance SHALL be read through aligned witnessed tiers with a visible source

The DB-free journal read paths SHALL resolve forcing provenance for the same (source, cycle, model) identically: the candidate-state read SHALL apply the same journal-direct-file fallback the forcing-context read already applies when the journal row is absent, materializing the recovered provenance into the candidate state, and both reads SHALL agree on the recovered forcing version identity, with URI values in the candidate-state read subject to the public-read redaction boundary (a redacted URI is the documented placeholder, not a read disagreement, and downstream decision logic treats it as a withheld — not probeable — reference). Every consumer-visible provenance record SHALL carry a source marker naming the tier that produced it (journal row, journal direct file, or object-store sidecar; absent when no tier yielded a witness), so an operator can distinguish a missing journal row from a missing package. When no witnessed tier yields provenance, the reads SHALL report the absence honestly (null provenance, absent source marker) and SHALL NOT fabricate a synthetic provenance record on the recovery path.

#### Scenario: Candidate state applies the direct-file fallback the context read already has

- **WHEN** the journal has no forcing-version row for a cycle but the
  journal direct file for that (source, cycle, model) exists
- **THEN** the candidate state materializes the direct-file provenance
  (marked with the direct-file source) and the forcing-context read
  and the candidate-state read return the same forcing version
  identity, with the package URI in the candidate-state read subject
  to the public-read redaction boundary (a redaction placeholder there
  is the documented boundary value, not a disagreement between the two
  reads)

#### Scenario: Both journal tiers empty yields honest null

- **WHEN** neither a journal forcing-version row nor a journal direct
  file exists for the (source, cycle, model)
- **THEN** the candidate state carries null forcing provenance with no
  fabricated record, and the decision layer's sidecar tier is the only
  remaining witness source

#### Scenario: Evidence exposes the provenance source tier

- **WHEN** a failure-state decision on the DB-free read path consulted
  the provenance tiers (recovered a witness from one of them, or
  exhausted them)
- **THEN** the decision evidence names the provenance source tier as
  one of journal, direct, object_store_sidecar, or absent, readable by
  an operator triaging a blocked recovery (a decision whose probed
  reference came from another state container without any tier
  consultation carries no tier marker)

### Requirement: Artifact existence probes SHALL witness directory-shaped package URIs via a derived manifest file key and SHALL fail closed with distinguishable evidence when no object-store root is configured

The failure-state artifact existence probe SHALL never hand a
package-prefix-shaped object URI to the object store directly. A recorded
forcing package URI counts as prefix-shaped whenever the closed-world
object-path validator does not admit it as a FILE key — with or without a
trailing `/` (the producer's directory URI carries one; the handoff lane's
normalized copy of the same reference does not). For the forcing package legs —
the journal and direct tiers alike, matching the sidecar tier — the probe
target SHALL then be the package manifest FILE key derived from the recorded
package URI through the single producer-isomorphic derivation helper (package
URI joined with the producer's default package-manifest filename), and a
recorded URI the validator already admits as a file key SHALL be probed as-is,
never double-derived. The emitted blocker evidence SHALL keep the recorded
package URI as the artifact reference while surfacing the derived probe key as
provenance. The copyback leg, which has no canonical witness filename, SHALL
document its exemption from witness derivation at the decision site.

When no object-store root is configured (neither the candidate resource profile
nor the environment provides one), the object-URI branch of the probe SHALL
fail closed: the artifact is reported missing with the distinguishable unsafe
reason `object_store_root_unconfigured`, and no object URI — existent or bogus
— is ever silently reported as present. A store-side probe fault (a symlinked
or non-regular probe target, a stale or unreadable filesystem handle) SHALL be
contained fail-closed and SHALL never escape the decision path as an exception
— this includes faults raised while classifying the recorded URI's shape, not
only faults from the store probe itself. On the journal and direct tiers the
contained fault carries its own distinguishable unsafe reason
(`artifact_probe_error`); on the sidecar tier the same fault keeps that tier's
established no-witness contract (`forcing_version_row_absent` with a
`tier_status` read-fault detail, repair-eligible per the #1203 ruling — the
`tier_status` field, not `unsafe_reason`, is what tells the operator the
rebuild cannot clear it). An "absent" verdict with a null unsafe reason SHALL therefore arise
only from (a) a probe that actually ran against a resolvable file key in a
configured object store and determined absence, or (b) a recorded reference
that the closed-world validator rejects as unresolvable even after witness
derivation — a known residual where re-recording the reference via the
authorized rebuild remains an effective remedy, which is why such blockers stay
repair-eligible.

#### Scenario: Prefix-shaped forcing package URI with the package physically present is not reported missing

- **GIVEN** a candidate whose recorded `forcing_package_uri` is the canonical
  package prefix (`forcing/<source>/<cycle>/<basin_version_id>/<model_id>`,
  with or without the trailing `/`, bare key or `s3://` form) and an
  object-store root configured with the package manifest file present under
  that prefix
- **WHEN** the failure-state recovery leg probes artifact existence
- **THEN** the probe targets the derived package manifest file key, reports the
  artifact as present, and the decision does not emit
  `FORCING_PACKAGE_URI_MISSING`
- **AND** with the manifest file absent the decision emits the unchanged
  `missing_forcing_package_uri` blocker with a null unsafe reason (probed,
  determined absent) and the recorded package URI as `artifact_uri`

#### Scenario: Unconfigured object-store root fails closed with a distinguishable reason

- **GIVEN** a candidate with no object-store root in its resource profile and
  no `OBJECT_STORE_ROOT` in the environment
- **WHEN** the probe evaluates any object-shaped artifact URI, including a
  nonexistent bogus key
- **THEN** the probe reports missing with unsafe reason
  `object_store_root_unconfigured` (never a silent pass), and the resulting
  blocker evidence carries that reason so an operator can distinguish "no probe
  ran" from "probed, absent"

#### Scenario: Store-side probe faults are contained fail-closed and never abort the scheduler pass

- **GIVEN** a candidate on the journal or direct tier with a configured
  object-store root whose derived witness manifest key hits a symlinked leaf, a
  symlinked ancestor, or a stale filesystem handle — or whose recorded URI is
  malformed enough to make shape classification itself raise
- **WHEN** the failure-state recovery leg probes artifact existence
- **THEN** the decision returns a fail-closed blocker (never an escaping
  exception), the scheduler pass continues evaluating other candidates, a
  store-probe fault carries the unsafe reason `artifact_probe_error`
  (distinguishable from both "probed, absent" and "store unconfigured") and is
  rejected by the authorized repair channel (a forcing rebuild cannot clear a
  filesystem fault), while a malformed unresolvable reference stays on the
  repair-eligible null-reason residual (re-recording via rebuild is its remedy)

#### Scenario: Root-unconfigured blockers are non-repairable via the authorized repair channel while probed-absent blockers stay repair-eligible

- **GIVEN** a missing-forcing blocker whose unsafe reason is
  `object_store_root_unconfigured`
- **WHEN** the operator-authorized single-cycle repair channel evaluates it
- **THEN** the repair is rejected as `forcing_artifact_reference_unsafe`
  (a forcing rebuild cannot cure a missing store configuration; the remedy is
  configuration)
- **AND** a blocker produced by a probe that ran in a configured store and
  determined the package absent (null unsafe reason) remains accepted by the
  repair channel for both existing blocker reason/classifier pairs

### Requirement: Permanent-Failure Marking Covers Master-Row Geometry

The permanent-failure mark required for non-transient failures and for exhausted retry budgets SHALL land on file-journal master-row geometry through every exit that declines automatic retry for rows whose persisted status is a markable failure source (`failed`, `submission_failed`; carve-outs below), with the same observable semantics as non-master rows, without weakening the journal's master-row write restrictions.

This extends the existing "Retry Guard — Non-Transient Error Exclusion" and
"Max Retries Exhausted — Permanent Failure" requirements to master-row
geometry; within the markable domain it does not alter their code
classification or budget semantics. Two master-row shapes are not "failed
jobs" in the sense of those base requirements and are carved out by
scenarios below: `partially_failed` masters (the cohort retains partial
success and stays governed by the partial-advance contract) and
`reservation_lost` masters (reclaim-pending, not permanently failed).

#### Scenario: Master row declined for auto-retry is marked permanently failed

- **WHEN** a file-journal `pipeline_job` row whose accepted-submit row kind
  is `master` fails and automatic retry is declined — whether because the
  error code is non-transient (e.g. `OUT_OF_MEMORY`, `INVALID_MANIFEST`),
  unknown and defaulted non-transient, or transient with the retry budget
  exhausted
- **THEN** if the row's persisted status is a markable failure source
  (`failed`, `submission_failed`), it SHALL transition
  to `status="permanently_failed"` through a typed journal authority
  transition, regardless of which decline exit ran (lost reservations and
  partially failed cohorts are carved out below)
- **THEN** the transition SHALL preserve the row's accepted-submit
  accounting evidence (reconciliation decision, submit outcome, matched
  Slurm job id) unchanged
- **THEN** a `pipeline_event` with `event_type="permanently_failed"` and the
  row's real prior status SHALL be appended exactly once per actual status
  transition
- **THEN** the decline outcome SHALL remain: no automatic retry scheduled,
  no new `pipeline_job` rows created, and the cycle's `PipelineResult`
  status remains `"failed"`
- **THEN** a journal write failure while marking SHALL NOT alter the decline
  outcome — the decline exits still return their pre-marking results, the
  failure is surfaced as an operator-visible signal rather than an exception
  escaping into the orchestration cycle, and the idempotent mark is
  re-attempted on a later pass

#### Scenario: The mark survives subsequent cohort projection passes

- **WHEN** a later orchestration pass resumes or re-projects the cohort of a
  master row already marked `permanently_failed`
- **THEN** the row SHALL remain `permanently_failed` (projection updates its
  evidence fields without reverting the terminal status) and no additional
  `permanently_failed` or reverting status-change event SHALL be appended

#### Scenario: Marking is idempotent against stale callers

- **WHEN** a decline runs again for a row already persisted as
  `permanently_failed` (including callers holding a stale job snapshot), or
  runs with a stale snapshot claiming `permanently_failed` while the
  persisted row is still `failed`
- **THEN** the idempotency decision SHALL consult the persisted row: an
  already-marked row is returned unchanged with no duplicate event, and a
  not-yet-marked row is still marked despite the stale snapshot

#### Scenario: The typed transition only accepts markable failure sources

- **WHEN** the typed permanent-failure transition is invoked on a master row
  whose persisted status is outside the markable set
  `{failed, submission_failed}` (e.g. `running`, `reserved`, `succeeded`,
  `cancelled`, `partially_failed`, `reservation_lost`)
- **THEN** the row SHALL remain unchanged, no event SHALL be appended, and
  the call SHALL report a stale/no-op outcome rather than raising

#### Scenario: Partially failed cohorts keep partial-advance semantics

- **WHEN** a mixed-outcome forecast cohort projects its master row to
  `partially_failed` and a failed member's error declines automatic retry
  through the nested partial-array-retry exit (whether non-transient or
  retry-budget exhausted)
- **THEN** the master row SHALL NOT be marked `permanently_failed` — the
  mark declines as stale with zero writes and zero events — and a subsequent
  resume pass SHALL behave exactly as before marking existed: the cohort's
  succeeded members keep advancing through downstream stages, the cycle's
  terminal status stays the partial outcome, and no failure terminal is
  written in place of the partial one

#### Scenario: Lost reservations are not mark sources

- **WHEN** a decline exit or any caller invokes the mark on a master row
  whose persisted status is `reservation_lost` (either durable sub-shape:
  `absence_retry_permitted` or `identity_mismatch_released`)
- **THEN** the mark SHALL be declined as stale with zero writes and zero
  events and SHALL NOT raise — a lost reservation is reclaim-pending, not a
  permanently failed job, and both reclaim doors (the reservation reclaim
  predicate and the reconcile-verified retry shortcut) SHALL remain open

#### Scenario: Marked master rows do not resurrect via upstream refresh

- **WHEN** a master row already marked `permanently_failed` would otherwise
  qualify for the upstream-refresh resubmission path available to `failed`
  rows
- **THEN** no resubmission SHALL occur — matching the semantics non-master
  rows already receive from their permanent-failure mark, for both the
  non-transient and the exhausted-budget domains

#### Scenario: Master row with transient code and remaining budget keeps existing retry identity behavior

- **WHEN** a master row fails with a transient error code and
  `should_auto_retry` is true
- **THEN** the existing master-row retry-identity flow SHALL proceed
  unchanged and the row SHALL NOT be marked permanently failed

#### Scenario: Marking is scoped to retry services with journal capability

- **WHEN** the decline logic runs with a retry service that lacks
  file-journal persistence capability
- **THEN** the decline SHALL keep its existing behavior — no scheduling, no
  marking, and no raising

### Requirement: Pre-Guard Evidence Channels Consult Permanence

Every db-free decision-ladder evidence channel that can emit an automatic-retry decision before the permanent-failure guard — except the output-absence recompute channel, recorded exempt below — SHALL consult a single shared permanence judgement before overwriting a permanent failure classification, and SHALL refuse the overwrite when the failure's classification proves the channel's remedy cannot address the cause.

The permanent-failure guard remains consulted at emitting return points
(never as an unconditional pre-pass). The recorded-code scoping governs the
downstream-resume channel's unknown-code clause only: reader-synthesized
placeholder codes (defaults fabricated when the state records no error code at all)
are not evidence under that clause and keep their existing behavior,
including the existing classifier-based refusals; the raw-manifest and
model-package channels consult the judgement for every permanent
classification, recorded code or not. The clause's recorded-code domain is
scoped to codes recorded by the failing stage itself — stale codes from
recovered stages or retry-history keys are not evidence for the domain split,
even though they still supply the reason-code text. The fabrication condition
and the domain condition are therefore different sets: a failure whose own
stage recorded nothing sits in the placeholder domain even when a stale code
elsewhere supplies its classification.

This requirement carves out deliberate, recorded exceptions to "Retry
Guard — Non-Transient Error Exclusion" (and, where noted, to the
unknown-code default and max-retries clauses) for three geometries whose
structural evidence proves the remedy causal — these are recorded
exceptions to those clauses' blanket prohibitions, not reinterpretations:

- **Output-absence recompute** (ruled in #1161): when durable forecast
  output is absent, the recompute channel may schedule an automatic restart
  from the forecast stage for its approved code set (including
  `OUT_OF_MEMORY`), including with an exhausted retry budget (the channel
  carries no budget gate), and is exempt from the consultation obligation
  above (it gates on its own approved code set instead).
- **Raw-manifest repair and post-repair downstream retry**: when the
  geometry itself evidences an input defect (a manifest probed missing
  after a previously successful download, or a repair download newer than
  the failure), the channels may re-emit their repair/retry decisions for
  any recorded code outside the remedy-non-causal classes — input-defect
  codes (e.g. `INVALID_MANIFEST`), unknown-default codes (e.g.
  `SLURM_JOB_FAILED`), and other listed non-transient codes (e.g.
  `OUTPUT_INCOMPLETE`) — including with an exhausted retry budget.
- **Model-package refresh** (ruled in #1161): when the model package
  genuinely changed, the refresh channel may claim codes outside its own
  refusal set (e.g. `TEMPLATE_NOT_ALLOWED`), because the changed package is
  itself the causal remedy for policy/template rejections, including with
  an exhausted retry budget.

Manual-retry paths are out of scope: their emitted decision, reason, and
retry policy are unchanged (the `failure.retryable` evidence field narrows
with the shared classification).

#### Scenario: Raw-manifest repair channels refuse remedy-non-causal permanent codes

- **WHEN** a candidate's failure state matches the missing-raw-manifest
  repair geometry (or the repaired-raw-manifest downstream geometry) and the
  recorded failure classifies as permanent with a classification proving the
  input-repair remedy non-causal (resource/configuration or policy/permission
  class — at minimum `OUT_OF_MEMORY`)
- **THEN** the channel SHALL NOT emit a retry decision — the ladder falls
  through to the remaining channels and, absent another legitimate claim,
  to the permanent-failure guard with `automatic_retry_allowed: false`

#### Scenario: Raw-manifest geometry evidence keeps other codes repairable

- **WHEN** the same raw-manifest geometries match with any other recorded
  code — input-defect codes (e.g. `INVALID_MANIFEST`) or unknown codes
  defaulted non-transient (e.g. `SLURM_JOB_FAILED`), including with an
  exhausted retry budget
- **THEN** the repair/downstream-retry decision SHALL be emitted exactly as
  before — the geometry itself (a manifest probed missing after a previously
  successful download, or a repair download newer than the failure) is the
  causal evidence that re-ingesting input is on point, and the repair remedy
  SHALL NOT be retired for production code shapes

#### Scenario: Downstream resume refuses recorded permanent and unknown-default codes

- **WHEN** a candidate with durable SHUD output fails a downstream stage
  with a genuinely recorded code that is non-transient (e.g.
  `OUTPUT_INCOMPLETE`, or a recorded `PARSE_FAILED` since the
  stage-failure family joined the non-transient list) or unknown and
  defaulted non-transient (e.g. a recorded `SLURM_JOB_FAILED`), or whose
  retry budget is exhausted
- **THEN** the downstream-resume channel SHALL NOT emit a resume decision;
  a recorded transient code within budget SHALL keep the existing resume
  behavior unless the state explicitly marks the failure permanent
  (top-level `permanent: true`, which forces permanence — see the top-level
  key scenario below)

#### Scenario: Synthesized placeholder codes keep existing downstream behavior

- **WHEN** a downstream failure state records no error code for the failing
  stage itself (stale codes elsewhere in the state — recovered stages,
  retry-history keys — do not count, even when such a stale code still
  supplies the classification's reason code), so the classification rests on
  no code this failure recorded — a stage-derived placeholder the reader
  synthesizes when the state carries no code at all
- **THEN** the downstream-resume decision SHALL behave exactly as before
  this change — the unknown-code clause governs recorded codes, not
  reader-fabricated defaults

#### Scenario: Top-level state retryable cannot whiten a permanent code

- **WHEN** a candidate state carries a top-level `retryable: true` key while
  its failure classifies as permanent (e.g. `OUT_OF_MEMORY`)
- **THEN** the permanence classification SHALL stand and the decision falls
  to the permanent-failure guard; the top-level key MAY only reassert
  retryability for codes whose classification is already retryable, and an
  explicit top-level `permanent: true` still forces permanence

#### Scenario: Model-package refresh and output-absence recompute rulings unchanged

- **WHEN** a candidate matches the model-package refresh geometry (permanent
  failure + changed package, refusing the resource-configuration class and
  `OUT_OF_MEMORY`), or the missing-forecast-output recompute geometry with a
  code in its approved recompute set
- **THEN** both channels SHALL behave exactly as before this change — the
  refresh channel's refusal list moves to the shared judgement source with
  zero semantic change, and a code refused by the raw-manifest channels MAY
  still be legitimately claimed by the refresh channel when the package
  genuinely changed

### Requirement: Local Artifact Allowed-Roots Normalization Survives Symlink Loops

The failure-state local artifact guard SHALL normalize its containment bases (the candidate resource-profile artifact roots and their environment fallbacks) and the probed artifact path without relying on symlink-loop-unsafe resolution, SHALL return the same verdict on every supported CPython version, and SHALL never let a fault inside that canonicalization (the strict-realpath normalization and its fallback) escape the decision path as an exception.

A root that fails strict resolution with `ENOENT` keeps its existing admitted
semantics via non-strict realpath normalization (a root may legitimately point
at a not-yet-created directory or an unmounted share). This admission is
deliberately errno-scoped, not loop-free: a root whose strict resolution hits
a missing component before any loop (such as a `<missing>/../<loop>` form)
stays admitted even though the admitted base still contains the loop — a
known, recorded residual. Such a root never raises the root-fault reason (it
is admitted, so no root fault is flagged); the resulting verdicts depend on
how the probed path itself normalizes: a path that also normalizes through
the ENOENT fallback and lands under the phantom base keeps the admitted-root
null-reason verdict, a path that resolves straight into the loop reports
`local_artifact_path_unresolvable`, and a path outside the base reports
`local_artifact_path_outside_allowed_roots`. A root that fails
for any other reason (a symlink loop, a permission fault) is excluded from the
containment bases, and when the probed artifact is not contained by any
remaining resolvable root the guard SHALL report the artifact missing with the
distinguishable unsafe reason `local_artifact_root_unresolvable` — non-null,
and therefore refused by the operator-authorized repair channel under the same
doctrine as `artifact_probe_error`: a rebuild cannot clear a filesystem fault.
The existing reasons keep their meanings, with root faults taking priority:
`local_artifact_path_outside_allowed_roots` is reserved for a candidate whose
every configured root normalized successfully (resolved, or `ENOENT`-admitted
as above) and whose path is genuinely outside them, and
`local_artifact_path_unresolvable` for a probed path that itself fails
resolution while every configured root normalized successfully — whenever any
configured root is unresolvable and no resolvable root contains the path, the
root fault reason wins, so root faults and path faults stay distinguishable.
On this local leg, an "absent" verdict with a null unsafe reason SHALL arise
only from a path contained by a successfully normalized root (resolved, or
`ENOENT`-admitted as above) after that path was actually probed for
existence — the parallel of the object-branch null-reason clause, stated here
so the two legs read as a matched pair.

#### Scenario: A symlink-loop root never aborts the scheduler pass

- **GIVEN** a candidate whose object-store, copyback, or published-artifact
  root (resource profile or environment) is a symlink loop
- **WHEN** the failure-state local artifact guard evaluates any local artifact
  URI for that candidate
- **THEN** the guard returns a fail-closed verdict without raising on any
  supported CPython version, the scheduler pass continues evaluating the
  remaining candidates, and per-tick evidence is still written

#### Scenario: Artifacts judged against a loop root carry the distinguishable root fault reason

- **GIVEN** a candidate whose only configured artifact root fails strict
  resolution with an errno other than `ENOENT` (a symlink loop reached before
  any missing component, a permission fault)
- **WHEN** the guard evaluates a local artifact path — whether lexically under
  or outside the loop root
- **THEN** the artifact is reported missing with unsafe reason
  `local_artifact_root_unresolvable` (never a null-reason absent verdict that
  would feed the authorized rebuild channel, and never
  `local_artifact_path_outside_allowed_roots`, which would route the operator
  to artifact placement instead of the faulty root), and the
  operator-authorized repair channel refuses the resulting blocker

#### Scenario: Resolvable roots keep their existing admitted and containment semantics

- **GIVEN** a candidate with a mix of resolvable roots (including a root that
  does not exist yet) and an unresolvable loop root
- **WHEN** the guard evaluates an artifact path under one of the resolvable
  roots
- **THEN** containment succeeds exactly as before this change — the
  not-yet-created root stays admitted via non-strict realpath normalization
  and the loop root does not poison the verdict; on a candidate whose every configured
  root resolves successfully, an artifact genuinely outside all of them still
  reports `local_artifact_path_outside_allowed_roots`

#### Scenario: A probed path that itself fails resolution keeps the path-fault reason

- **GIVEN** a candidate with resolvable artifact roots and a probed local
  artifact path that is itself a symlink loop
- **WHEN** the guard evaluates that path
- **THEN** the artifact is reported missing with the existing unsafe reason
  `local_artifact_path_unresolvable`, keeping path faults distinguishable from
  root faults

### Requirement: Retry Runtime-Root Safety Survives Symlink Loops

The retry submission path SHALL normalize local runtime roots (`workspace_dir`, `object_store_root`, `published_artifact_root`) without relying on symlink-loop-unsafe resolution, SHALL return the same safety verdict on every supported CPython version, and SHALL never let a fault inside that normalization (tilde expansion, canonicalization, or its fallback) escape the safety helper as an exception.

A root that fails strict resolution with `ENOENT` is re-admitted through
non-strict realpath normalization only after a loop-filtering re-check: the
fallback value is strictly re-resolved, and only a second `ENOENT` (the root
genuinely does not exist yet — a not-yet-created directory or an unmounted
share) or a clean strict re-resolution (a `<missing>/../<real>` form) keeps
the admitted verdict, byte-compatible with the pre-change resolved value. A fallback that still fails for any other reason — including
the `<missing>/../<loop>` phantom form whose fallback retains a symlink loop
— is rejected. A root that fails strict resolution for any reason other than
`ENOENT` (a symlink loop, a permission fault, a stale file handle, a
non-directory component) is likewise rejected. Every rejection arising from
strict resolution or its fallback uses the existing reason
`unresolvable_local_root`; the non-absolute arm keeps its existing
`relative_local_root` / `parent_traversal_local_root` reasons. Every
rejection feeds the existing unsafe-rejection wiring. Rejection is
per-candidate: the rejected root SHALL
NOT enter that candidate's resolved set, comparable-roots overlap baseline,
or submission-manifest contribution, and the rejection SHALL be recorded in
the evidence's bounded `rejected` list — or accounted in the rejection
counters when the evidence cap elides the entry; when no complete candidate
remains, the submission fails with the structured error code
`RETRY_RUNTIME_ROOTS_UNSAFE` — absent a higher-precedence secret-bearing
rejection, which keeps its existing `RETRY_RUNTIME_ROOTS_SECRET_BEARING`
code. A value whose tilde expansion cannot be
completed (an unknown user home) SHALL fail closed through the existing
non-absolute rejection arm instead of raising.

#### Scenario: A symlink-loop runtime root never escapes as an exception

- **GIVEN** a retry candidate whose `workspace_dir`, `object_store_root`, or
  `published_artifact_root` is a symlink loop, or a value whose tilde
  expansion cannot resolve a home directory
- **WHEN** the retry submission path (the DB manual-retry leg or the db-free
  journal leg) resolves its runtime roots
- **THEN** the safety helper returns a fail-closed verdict without raising on
  any supported CPython version, the rejection is recorded in the
  `runtime_root_resolution` evidence naming the rejected field and reason,
  and when no other complete candidate resolves the submission fails with
  error code `RETRY_RUNTIME_ROOTS_UNSAFE` — never the degraded
  `SBATCH_SUBMISSION_FAILED` attribution

#### Scenario: A loop root never enters the manifest or the overlap baseline

- **GIVEN** a retry candidate with a symlink-loop local runtime root
- **WHEN** that candidate's runtime roots are resolved on any supported
  CPython version
- **THEN** the loop root is absent from that candidate's resolved root set,
  absent from its submission-manifest contribution, and absent from the
  comparable-roots baseline that feeds the workspace/object-store overlap
  guard — the `unresolvable_local_root` rejection is recorded instead

#### Scenario: A not-yet-created root keeps its admitted semantics

- **GIVEN** a retry candidate whose local runtime root fails strict
  resolution with `ENOENT` and whose non-strict fallback also strictly
  re-resolves to `ENOENT` (a not-yet-created directory, an unmounted share)
  or resolves cleanly (a `<missing>/../<real>` form)
- **WHEN** the candidate's runtime roots are resolved
- **THEN** the root stays admitted with verdict "ok" and a value
  byte-compatible with the pre-change resolved value, and no rejection is
  emitted

#### Scenario: A phantom loop-carrying root is rejected on every version

- **GIVEN** a retry candidate whose local runtime root is a
  `<missing>/../<loop>` form — strict resolution fails with `ENOENT` at the
  missing component, but the non-strict fallback still contains a symlink
  loop
- **WHEN** the candidate's runtime roots are resolved
- **THEN** the loop-filtering re-check rejects the root with
  `unresolvable_local_root` on every supported CPython version — the
  admitted-root arm never returns a loop-carrying value into the manifest or
  the overlap baseline

#### Scenario: A permission-fault root is rejected like a loop root

- **GIVEN** a retry candidate whose local runtime root fails strict
  resolution with an errno other than `ENOENT` (for example `EACCES` on an
  untraversable parent)
- **WHEN** the candidate's runtime roots are resolved
- **THEN** the root is rejected with `unresolvable_local_root` exactly as a
  symlink loop is, on every supported CPython version

### Requirement: Local Artifact Path Tilde Expansion Never Raises

The failure-state local artifact guard SHALL expand a leading tilde in a
probed local artifact value without ever letting the expansion escape the
decision path as an exception: when the home directory cannot be determined
(an unknown `~user` prefix, or a plain `~` with no usable home-directory
source), the unexpanded value SHALL flow on as an ordinary path into the
existing containment verdicts and produce a deterministic missing-status
tuple under those rules instead of aborting the scheduling pass. Because
the unexpanded value is a relative path, its verdict is anchored at the
process working directory: with every configured root normalized
successfully and the working directory outside all of them the guard
reports the existing outside-allowed-roots containment reason, and with
the working directory under a configured root the existing
contained-and-probed verdicts apply unchanged. The existing root-fault
priority is untouched — when any configured root is unresolvable and no
resolvable root contains the anchored path, the root-fault reason still
wins exactly as the existing requirement reserves it. Values whose tilde does expand, and values without a tilde,
keep their existing behavior byte-for-byte, and the guard's root-side and
path-side normalization SHALL treat the same unexpandable-tilde input
consistently — neither side raises, and each side follows the existing
containment rules from the same working-directory anchor.

#### Scenario: An unknown-user tilde path fails closed instead of crashing the pass

- **GIVEN** a candidate whose probed artifact value is
  `~nosuchuser/output/summary.json`, whose configured artifact roots
  resolve normally, and a process working directory outside every
  configured root
- **WHEN** the failure-state artifact guard evaluates the value
- **THEN** no exception escapes and the guard returns the deterministic
  missing-status tuple with the existing outside-allowed-roots containment
  reason — the pass continues and evidence for the candidate is written

#### Scenario: A plain tilde with no determinable home directory fails closed

- **GIVEN** an environment where the home directory cannot be determined
  (no `HOME` and no password-database entry for the current uid), a probed
  artifact value of `~/output/summary.json`, and a process working
  directory outside every configured root
- **WHEN** the failure-state artifact guard evaluates the value
- **THEN** no exception escapes and the guard returns the same
  deterministic fail-closed missing-status shape as the unknown-user form

#### Scenario: Expandable and tilde-free values keep their behavior

- **GIVEN** a probed artifact value whose tilde expands against a real
  home directory, or a value with no tilde at all
- **WHEN** the failure-state artifact guard evaluates the value
- **THEN** the returned verdict is byte-for-byte identical to the
  pre-change behavior

#### Scenario: Root side and path side agree on the same unexpandable input

- **GIVEN** the same unexpandable `~user` string supplied both as a
  configured artifact root and as the probed artifact value
- **WHEN** the guard normalizes each side
- **THEN** neither side raises, both sides anchor the unexpanded literal
  at the same working directory, and the equal anchored paths yield the
  existing contained-but-absent verdict (a missing-status tuple with a
  null unsafe reason) rather than a containment rejection

### Requirement: Raw-Manifest Repair Legs Consult the Unified Artifact Probe

Both raw-manifest repair legs SHALL determine manifest presence through
the unified artifact probe rather than the bare object-manifest check —
this covers the missing-manifest repair channel and the downstream
retry-after-repair channel — and SHALL act only on a probe verdict whose
unsafe reason is null. When the probe reports a non-null unsafe reason
(no object-store root configured, or a contained probe fault), both legs
SHALL abstain: neither leg asserts manifest existence or absence, grants
an automatic retry, nor lets an exception escape; the candidate flows to
the remaining decision ladder, which alone determines the terminal — the
legs invent no manual channel of their own, a transient failure within
its retry budget whose state engages no higher ladder rung keeps its
automatic retry from the generic rung, and geometries the fail-open
verdict previously shielded from the ladder's own rungs take those
rungs' existing terminals (a native-SHUD restart stage whose forcing
reference the artifact guard fail-closes, a permanent failure code
including remedy-permitted ones, a cancelled run, or an exhausted retry
budget — each lands on the rung that already owned it) — and the rest
of the scheduling pass keeps running. When the probe determines
presence or absence with a null unsafe reason, both legs keep their
existing behavior byte-for-byte, including the recorded residual where
a reference the probe cannot resolve counts as absent for the repair
channel (a re-ingestion re-records it).

#### Scenario: An unconfigured store no longer vouches for manifest existence

- **GIVEN** a candidate whose resource profile carries no object-store
  root and no `OBJECT_STORE_ROOT` environment fallback, with a failure
  state that satisfies the downstream retry-after-repair structural gates
- **WHEN** the downstream leg evaluates the raw-manifest URI
- **THEN** no evidence claiming `manifest_exists: true` or granting
  `automatic_retry_allowed: true` is produced by the raw-manifest legs;
  the candidate's decision comes from the remaining ladder

#### Scenario: Abstention does not convert guard-free transient failures to manual outcomes

- **GIVEN** the same unconfigured-store geometry where the candidate's
  failure classification is transient and within its retry budget, its
  restart geometry engages no ladder guard of its own (a convert-stage
  restart with no forcing or copyback requirement), and no higher
  ladder rung (the permanent guard, the cancelled rung) claims the
  state
- **WHEN** the scheduler decides the candidate's state
- **THEN** the decision remains an automatic retry from the generic
  retry rung — the legs' abstention itself invents no manual channel

#### Scenario: Abstention un-shadows the ladder's own guards rather than overriding them

- **GIVEN** the unconfigured-store geometry where the fail-open verdict
  previously let the downstream leg claim the candidate, and either the
  restart stage is the native-SHUD forecast stage (so the artifact guard
  probes its forcing reference) or the failure code is permanent but
  remedy-permitted
- **WHEN** the scheduler decides the candidate's state
- **THEN** the terminal is whichever ladder rung owns the geometry — for
  these two pinned cases the guard's existing blocked outcome
  (`missing_forcing_package_uri` — carrying the unsafe reason when the
  fault is process-wide, an unconfigured root; a single-leaf probe fault
  leaves the guard's own probe verdict intact — or
  `permanent_failure_guard`; a cancelled run or an exhausted retry
  budget likewise keeps its own rung's terminal, the latter pinned by
  the pass-containment scenario) — the same terminal the identical
  candidate already received whenever the legs' structural gates did not
  hold, not a new terminal introduced by the legs

#### Scenario: An unconfigured store does not let the repair leg invent a verdict

- **GIVEN** the unconfigured-store geometry with a failure state that
  satisfies the missing-manifest repair structural gates and a manifest
  object that genuinely does not exist
- **WHEN** the repair leg evaluates the raw-manifest URI
- **THEN** the repair leg abstains without asserting the manifest
  present or absent; the repair channel stays untriggered until a store
  root is configured, which is the recorded limitation of the abstention
  design and identical to the pre-change outcome for this leg

#### Scenario: A probe fault degrades to abstention instead of aborting the pass

- **GIVEN** a raw-manifest probe whose object store raises the contained
  probe fault (a symlinked probe target or a stale filesystem handle)
- **WHEN** the scheduler evaluates a batch containing that candidate and
  a healthy sibling
- **THEN** no exception escapes the scheduling pass: the faulted
  candidate receives a terminal from the remaining decision ladder, and
  the sibling is still evaluated and submitted (a submitted count of
  exactly the sibling)

#### Scenario: Configured-store geometries keep their behavior

- **GIVEN** a candidate with a configured, resolvable object-store root
- **WHEN** either raw-manifest leg evaluates its URI
- **THEN** the produced evidence is byte-for-byte identical to the
  pre-change behavior for both the present and the absent manifest cases

### Requirement: DB-free selector and object-store probe lanes survive unexpandable tildes

The db-free selector adjudicators and the object-store probe lane SHALL
never let a home-directory-determination failure escape as a bare
RuntimeError. Specifically: (a) the db-free selector allowed-roots and
selector-path adjudicators SHALL treat an unexpandable tilde value as an
ordinary relative path and return their existing structured rejections
through the existing non-absolute arms; (b) constructing a local object
store with an unexpandable tilde root SHALL raise the domain
`ObjectStoreError` from within the constructor's existing error-conversion
boundary — never a bare RuntimeError and never a literal `~…` directory
created on disk — so the unified artifact probe and the sidecar provenance
tier, which already catch `ObjectStoreError`, keep producing their existing
distinguishable fail-closed attributions instead of crashing the scheduling
pass. Expandable and tilde-free values keep their existing behavior
byte-for-byte in all these lanes, with one recorded acceptance: a
`.`-prefixed value whose first surviving component is a tilde (the `./~/x`
class) moves from expand-and-admit to the relative-path rejection at the
str-input sites — a fail-closed direction pinned as an explicit carve-out
in the byte-compatibility tests.

#### Scenario: unexpandable tilde in db-free selector values yields structured rejections

WHEN a db-free runtime-manifest allowed-roots entry or selector path value
is `~nosuchuser/…` (or a plain `~/…` with no determinable home directory)
THEN the adjudicators return their existing relative-path rejection shapes
(`db_free_allowed_root_*` / `db_free_selector_path_*` families) without any
exception escaping

#### Scenario: unexpandable tilde object-store root converts to the domain error

WHEN a local object store is constructed with an unexpandable tilde root
THEN the constructor raises `ObjectStoreError` (not a bare RuntimeError)
and creates no directory for the literal tilde value

#### Scenario: probe lanes keep their fail-closed attributions under a tilde root

WHEN the configured object-store root is an unexpandable tilde value and a
candidate's artifact probe or forcing-sidecar provenance runs
THEN the unified artifact probe returns the existing probe-error
missing-status attribution and the sidecar tier returns its existing
unreadable attribution — the scheduling pass continues and evidence is
written

#### Scenario: expandable and tilde-free values keep their behavior

WHEN selector values or the object-store root have no tilde or expand
normally
THEN adjudication results and constructor behavior are byte-for-byte
identical to the pre-change behavior, except the recorded `./~/x`
acceptance (that class now takes the fail-closed relative-path rejection
at the str-input sites)

### Requirement: Released identity-blocked reservation rows SHALL remain outside automatic retry classification

A pipeline-job row produced by identity-blocked reservation release (`status="reservation_lost"`, `identity_mismatch_released` sub-shape) SHALL carry no `error_code` — the reservation writes that seed the row, including the reclaim re-seed of a lost reservation, SHALL keep setting `error_code` to null, and the release transition SHALL NOT introduce one — and `should_auto_retry`/`classify_failure` SHALL therefore evaluate it as non-retriable. Tests SHALL pin both the written row shape (driving the real reserve-then-release sequence — and the real reserve-permit-reclaim-release sequence — not a hand-built row) and the `should_auto_retry` verdict, so that any future edit stamping a transient error code onto the reservation, reclaim, or release writes fails the pin instead of silently opening a duplicate-submission route. This requirement governs only the automatic-retry classification decision: the reservation reclaim predicate and the reconcile-verified retry shortcut (the two reclaim doors that the existing "Lost reservations are not mark sources" requirement keeps open) are explicitly unaffected.

#### Scenario: Release row shape carries no error code

- **WHEN** an identity-blocked reservation is reserved and then released through the real transition sequence
- **THEN** the resulting accounting row has `status == "reservation_lost"` and a null `error_code`

#### Scenario: Release after reclaim carries no error code

- **WHEN** a reservation is reserved, permitted for retry after an ambiguous submit (`absence_retry_permitted`), reclaimed back to reserved through the real reclaim transition, and then released as identity-blocked
- **THEN** the released row still has a null `error_code` and `should_auto_retry` is false — the reclaim re-seed did not introduce a transient code

#### Scenario: Released row is not auto-retriable

- **WHEN** automatic retry classification evaluates the released reservation row
- **THEN** `should_auto_retry` is false, and the pin fails the moment any transient error code is introduced on the reservation, reclaim, or release writes

#### Scenario: Reclaim doors stay open

- **WHEN** the reservation reclaim predicate or the reconcile-verified retry shortcut evaluates the released row
- **THEN** their existing behavior is unchanged by this requirement

### Requirement: Retry-attempt claims from persisted manual-retry markers SHALL be honoured only under an active manual-retry decision

A retry-attempt claim that originates in a candidate's persisted `manual_retry` marker SHALL reach the forecast chain's attempt targeting only when the same scheduler-emitted `state_evidence`'s decision face carries an ACTIVE manual-retry decision — the evidence `decision` is `manual_retry` or its `reason` is `manual_retry_requested`. This judgement SHALL be applied at the candidate-manifest minting boundary, where the scheduler projects `state_evidence.manual_retry` into the basin payload's `manual_retry_attempt`/`retry_attempt` fields (the production channel that otherwise shadows every downstream read), and again at the chain's own `state_evidence` read as defence in depth, both consuming ONE shared predicate. Without an active manual-retry decision the manifest SHALL NOT mint those fields from the marker and the chain SHALL fall through to deriving the next unused `_retry_<n>` suffix for the stage; each dropped claim SHALL leave queryable evidence (a structured record carrying the searchable token `MANUAL_RETRY_ATTEMPT_CLAIM_IGNORED` and stating that no active manual-retry decision accompanies the claim — the claim is not thereby asserted to be stale, since a higher-priority decision lane may lawfully preempt a live marker — naming the basin, the claimed attempt, and the decision actually present, and claiming nothing about what the stage then does, which the emitting site cannot know) rather than degrade silently. That record SHALL be emitted once per dropped claim per pass at each of the two judgement points; the repetition is this rule's own shape ("each dropped claim", a per-candidate data condition that persists while the candidate stays admitted) and SHALL NOT be collapsed into a once-per-process notice. When the evidence carries neither a `decision` nor a `reason` key the claim SHALL be treated the same way — degrading to the next free attempt still submits, while honouring a wedged claim against an occupied terminal `_retry_<n>` row blocks the stage forever. Operator-supplied direct fields passed into an invocation from outside the scheduler projection are that invocation's own input and SHALL keep being honoured without this judgement. A dropped claim SHALL leave the candidate where a markerless candidate of the same decision stands on the three faces this rule governs — the resubmit-vs-resume decision, the derived attempt value, and whether a submission actually happens — in particular, for a decision outside the forced terminal-resubmit set with a terminal failed row present, the stage resumes that row exactly as it would with no marker at all; the claim judged inactive changes nothing on those faces, and this rule neither widens nor narrows any decision lane's own resubmission policy. Equivalence is claimed on those three faces only, because the same marker keeps readers that this judgement deliberately does not gate: chain-side `_manual_retry_scoped_cycle_execution`, whose two consumers are `_candidate_scoped_cycle_execution` (it only helps choose the job set fed to the next-free-suffix derivation, and a production single-basin candidate's `orchestration_run_id` reaches the same answer there) and `_replacement_retry_scoped_cycle_execution` (its first-line short-circuit feeds the duplicate-orchestration conflict gate, where `orchestration_run_id` does NOT reach the same answer — a marker-carrying candidate can clear a conflict at which its markerless twin is held); and scheduler-side `_candidate_state_is_candidate_scoped_retry`, which widens candidate admission on the same marker triple. Those readers stay outside this requirement — they mint no attempt, job-id derivation remains run-id-namespaced, and this rule leaves their behaviour unchanged (a recorded pre-existing boundary). This rule is the chain/manifest-side complement of the cycle-granularity marker requirement's attempt-pin discipline: that rule gates EVENT-derived markers through its pin test, while the state-level `manual_retry` mapping is copied into evidence whole — this rule closes that remaining channel at its consumers.

#### Scenario: A wedged marker claim no longer blocks the stage

- **WHEN** a candidate's `state_evidence` carries a persisted `manual_retry`
  claim (`new_attempt` 1) under a non-manual-retry decision and the stage's
  `_retry_1` job id is already occupied by a terminal row bound to a
  `slurm_job_id`
- **THEN** the candidate's basin manifest carries no
  `retry_attempt`/`manual_retry_attempt` minted from the marker, the forced
  resubmission targets the next free suffix (`_retry_2`) and actually submits
  — no `skipped_duplicate_submission` — and a structured record logs the
  dropped claim with the decision actually present

#### Scenario: An active manual-retry decision keeps its precise attempt identity

- **WHEN** the evidence decision is `manual_retry` (or its reason is
  `manual_retry_requested`) with `new_attempt` N, and that decision face still
  stands when the manifest is built
- **THEN** the manifest mints the attempt fields and the chain targets exactly
  `_retry_<N>`, byte-identical to today's fresh-marker behaviour

#### Scenario: A manual-retry decision superseded in flight degrades to the derived attempt

- **WHEN** a fresh manual-retry decision (marker `allowed`, `new_attempt` N) is
  preempted by a higher-priority in-flight evidence transform that rewrites the
  decision face while keeping the marker block — e.g. the strict-warm-start
  manifest upgrade replacing it with
  `retry_strict_warm_start_retry_run_manifest_mismatch`
- **THEN** the claim is handled exactly as one with no active decision: the
  manifest mints nothing from it, a drop record is left, and the stage targets the
  superseding lane's own derived attempt (a decision inside the forced
  terminal-resubmit set still submits) — markerless equivalence governs and the
  operator's pinned attempt is not honoured on that pass

#### Scenario: Unjudgeable evidence fails safe

- **WHEN** a `manual_retry` payload appears on evidence carrying neither a
  `decision` nor a `reason` key
- **THEN** the claim is dropped exactly as one without an active decision, and
  the derivation falls through to the next free attempt

### Requirement: DB-free selector containment survives symlink loops at both the root and the path level

The db-free retry submission lane SHALL normalize its selector containment
bases (the `NHMS_SCHEDULER_ALLOWED_ROOTS` entries and their equivalent
profile sources) **and** the selector path values judged against them without
relying on symlink-loop-unsafe resolution, SHALL return the same verdict on
every supported CPython version, and SHALL never let a fault inside that
normalization escape the adjudicator as an exception. Neither level may reach
`Path.resolve()` in either form: the non-strict form stopped raising on
symlink loops in CPython 3.13+, and the strict form raises an errno-less
`RuntimeError` on 3.12 and earlier, so neither is a usable loop predicate on
the supported interpreter range. Normalization SHALL go through
`os.path.realpath`, whose strict form raises `OSError` carrying an errno on
every supported version.

At **both** levels a value that fails strict resolution with `ENOENT` keeps
its existing admitted semantics — a configured root may legitimately point at
a not-yet-created directory or an unmounted share, and a selector path is
deliberately not existence-checked at submission time — but that admission
SHALL be loop-filtered rather than errno-scoped alone: the non-strict
fallback value is strictly re-resolved, and only a second `ENOENT` (the
target genuinely does not exist yet) or a clean strict re-resolution (a
`<missing>/../<real>` form) keeps the admitted verdict. A fallback that still
fails for any other reason — including the `<missing>/../<loop>` phantom
form, whose strict resolution stops at the missing component but whose
fallback still carries a symlink loop — SHALL be rejected. This loop-filtered
admission is deliberately the same doctrine the runtime-root lane already
carries and deliberately **not** the admitted-phantom residual recorded for
the local artifact guard: the selector path level consumes the selector root
level's output, so a bare fallback at either level would reproduce at the
next level the very containment fail-open the other level rejects.

A value that fails strict resolution for any reason other than `ENOENT` (a
symlink loop, a permission fault, a stale file handle, a non-directory
component) SHALL likewise be rejected, and a value that cannot be resolved at
all because it is not a valid path string (an embedded NUL, which raises
`ValueError` rather than `OSError`) SHALL fall into the same rejection rather
than escaping. Rejections SHALL use the lane's existing reasons —
`db_free_allowed_root_unresolvable` at the root level and
`db_free_selector_path_unresolvable` at the path level — with no new reason
vocabulary and no change to the rejection record shape or to the
adjudicators' signatures. Rejection remains per-value: a rejected root SHALL
NOT enter the containment bases, and the existing cascade SHALL be preserved
— when every configured root is rejected the path-level adjudicator still
reports `db_free_allowed_roots_missing`, and a path that **resolves cleanly**
yet lies outside the surviving bases still reports
`db_free_selector_path_outside_allowed_roots`. Resolution now precedes the
containment comparison, so a value that is *both* unresolvable and outside the
bases SHALL report the resolution reason rather than the containment one: that
re-ordering is deliberate — an unresolvable value has no trustworthy location
to compare against — and it changes the reported reason only within the
already-rejected class, never a verdict from admitted to rejected or back.

Values that resolve cleanly, and values whose strict resolution fails with
`ENOENT` and whose fallback re-resolves cleanly or to a second `ENOENT`,
SHALL keep their existing verdicts and their existing resolved values
byte-for-byte. Because both db-free legs (the retry submission leg and the
file-orchestration journal leg) consume the same pair of adjudicators, this
requirement governs both.

#### Scenario: A phantom loop-carrying selector root is rejected on every version

- **GIVEN** a db-free allowed-roots entry of the `<missing>/../<loop>` form —
  strict resolution fails with `ENOENT` at the missing component, but the
  non-strict fallback still resolves onto a symlink loop
- **WHEN** the db-free selector allowed-roots adjudicator normalizes it
- **THEN** no root is admitted and exactly one
  `db_free_allowed_root_unresolvable` rejection is recorded, on every
  supported CPython version — the same verdict the direct loop form already
  receives, so one physical target can no longer draw two opposite verdicts

#### Scenario: A not-yet-created selector root keeps its admitted semantics

- **GIVEN** a db-free allowed-roots entry that fails strict resolution with
  `ENOENT` and whose non-strict fallback either strictly re-resolves to a
  second `ENOENT` (a not-yet-created directory, an unmounted share) or
  resolves cleanly (a `<missing>/../<real>` form)
- **WHEN** the adjudicator normalizes it
- **THEN** the root stays admitted with a value byte-compatible with the
  pre-change resolved value and no rejection is emitted

#### Scenario: A symlink-loop selector path is rejected instead of lexically admitted

- **GIVEN** a db-free selector path value that is a symlink loop, or lies
  under one, while every configured allowed root resolves cleanly
- **WHEN** the selector path adjudicator judges it
- **THEN** it is rejected with the existing
  `db_free_selector_path_unresolvable` reason on every supported CPython
  version — the value never reaches the resolved selector fields or the
  submission manifest through a purely lexical containment comparison, and no
  `RuntimeError` escapes to the submission path's broad exception handler,
  so the structured rejection evidence is preserved instead of the attribution
  degrading to `SBATCH_SUBMISSION_FAILED`

#### Scenario: A phantom loop-carrying selector path is rejected

- **GIVEN** a db-free selector path of the `<missing>/../<loop>` form lying
  lexically under a cleanly resolving allowed root
- **WHEN** the selector path adjudicator judges it
- **THEN** the loop-filtered re-check rejects it with
  `db_free_selector_path_unresolvable` — the path level carries the same
  admission doctrine as the root level it consumes

#### Scenario: A not-yet-created selector path stays admitted

- **GIVEN** a db-free selector path under a cleanly resolving allowed root
  whose final components do not exist yet
- **WHEN** the selector path adjudicator judges it
- **THEN** no rejection is produced and the normalized value is byte-identical
  to the pre-change resolved value — submission-time adjudication still
  performs no existence check

#### Scenario: An unrepresentable selector value is rejected rather than raising

- **GIVEN** a db-free allowed-roots entry or selector path value carrying an
  embedded NUL, for which resolution raises `ValueError` rather than `OSError`
- **WHEN** the corresponding adjudicator normalizes it
- **THEN** it takes the lane's existing `*_unresolvable` rejection and no
  exception escapes the adjudicator

#### Scenario: An unresolvable out-of-root value reports the resolution reason

- **GIVEN** a db-free selector path that is a symlink loop and that also lies
  outside every configured allowed root
- **WHEN** the selector path adjudicator judges it
- **THEN** it reports `db_free_selector_path_unresolvable` rather than
  `db_free_selector_path_outside_allowed_roots`, while a cleanly resolving
  out-of-root value keeps the containment reason unchanged

#### Scenario: The all-roots-rejected cascade is preserved

- **GIVEN** a db-free configuration in which every configured allowed root is
  rejected by the root-level adjudicator
- **WHEN** a selector path is judged against the resulting empty bases
- **THEN** the path-level adjudicator reports the existing
  `db_free_allowed_roots_missing` reason, unchanged

