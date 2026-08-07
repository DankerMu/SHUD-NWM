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
- **THEN** the Orchestrator MUST NOT schedule an automatic retry
- **THEN** the Orchestrator SHALL mark the job as permanently failed immediately
- **THEN** a `pipeline_event` SHALL be appended with `details_json` containing `{"auto_retry_skipped": true, "reason": "non_transient_error", "error_code": "<code>"}`

#### Scenario: Transient error codes allow auto-retry

- **WHEN** a `pipeline_job` fails with one of the following error codes:
  - `SLURM_TIMEOUT` — Slurm walltime exceeded
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

---

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

When a candidate carries a failure signal and the forcing package its forecast stage references does not exist in the configured object-store root, the scheduler SHALL NOT emit another forecast retry from any decision branch — including the failure fallback and the permanent-failure branch — and SHALL instead emit the stable missing-forcing blocker (reason `missing_forcing_package_uri`, stable classifier, `artifact_exists` false, forecast restart stage) that the explicit single-cycle repair authorization channel accepts. Failure classification produced inside the compute task SHALL survive the DB-free path to array accounting through a durable per-task outcome receipt, with generic `NODE_FAILURE` used only as the fail-safe when no receipt is readable. The effective retry attempt SHALL derive from the durable per-stage attempt record (job-identity retry suffix) on both the in-stage retry gate and the scheduler's cross-pass failure policy, so that the configured retry limit bounds retries even for misclassified failures.

#### Scenario: Failure-state candidate with missing forcing blocks instead of retrying

- **WHEN** a candidate has a failure signal (failed pipeline status or
  failed hydro run) and its referenced forcing package is absent from
  the object store
- **THEN** the decision is blocked with reason
  `missing_forcing_package_uri` and the full artifact-guard evidence,
  satisfying the structural contract required by the explicit
  missing-forcing repair policy, and no forecast work is submitted

#### Scenario: Failure-state candidate with no recorded forcing provenance blocks the same way

- **WHEN** a candidate has a failure signal, its restart stage is the
  forecast stage, and NO forcing package reference is recorded in its
  state at all (the incident geometry: forcing was never produced, the
  journal records null provenance)
- **THEN** the decision is the same stable missing-forcing blocker
  (artifact reference null in the guard evidence), regardless of the
  recorded failure code — including permanently-classified codes such
  as policy or manifest failures, whose original cause remains
  visible in the per-job evidence — and a manual retry request, which
  is evaluated before the guard, remains the operator escape hatch

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

### Requirement: Strict-warm-start terminal mismatch retries SHALL respect a stage-scoped budget

When the candidate ladder would emit `retry_strict_warm_start_terminal_init_state_mismatch` for a terminal-success candidate whose recorded init-state identity mismatches the strict warm-start resolution, the scheduler SHALL first evaluate the stage-scoped retry attempt against the configured retry limit. When the attempt has reached the limit, the scheduler SHALL emit the stable blocked decision `blocked_strict_warm_start_init_state_mismatch` carrying a retry-policy block (automatic retry not allowed, manual retry required, attempt, retry limit) instead of the retry decision, and the blocked decision SHALL NOT participate in forced terminal resubmission or replacement-retry scoping. When the attempt is below the limit, the retry decision and its evidence SHALL remain unchanged. The stage-scoped attempt SHALL bind in the production geometry where the reserved master row carries no retry count and attempts are recorded only as retry-suffixed pipeline job rows.

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
stage's failure is the repair target: the resolved job is still in
a failed status AND either the state's failed stage equals the
resolved job's stage or the candidate has no failed model-scoped
job row of its own. In every other case — a candidate-scoped
failure of a different stage, or a marker whose resolved job is no
longer failed (stale) — the derived `new_attempt` falls back to
the candidate's own `previous_attempt + 1`, and the fallback is
terminal: the newest retry-count-bearing adopted marker decides;
older markers are not consulted. Marker-shaped events remain excluded from
blocker scanning regardless of attribution (a foreign marker must
never be treated as an active blocker suppressing the candidate's
own manual retry), and candidate-state event-row visibility on the
journal/DB read paths is unchanged — cycle-wide events stay visible in
every candidate's raw state for diagnostics. On the identity-filtered
decision state, preserving the attribution predicate fields makes a
self-declared MATCHING `model_id` a retention credential for a
non-authoritative marker event under shared-cycle scoping (foreign
model ids stay excluded; within one source-cycle aggregate a model id
maps to exactly one candidate), so a candidate-own marker that
sanitization previously stripped to anonymity is now retained and can
drive the retry decision it was written to request.

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

- **WHEN** a manual retry event targets a still-failed cycle-scope
  pipeline job (`model_id` empty and `run_id` in the
  `cycle_<source>_<stamp>` grammar) and that cycle stage's failure
  is what the candidate decision repairs — the failed stage matches
  the job's stage, or the candidate has no failed model-scoped job
  row of its own (the production cohort-download shape)
- **THEN** the derived `new_attempt` pins the marker's
  `retry_count`, so the operator's cycle-level manual retry stays
  effective and the minted retry identity does not reuse a consumed
  attempt number
- **AND** when the candidate's own failure is at a different stage,
  or the marker's resolved job is no longer failed (stale), the
  derived `new_attempt` falls back to `previous_attempt + 1` — the
  cycle job's counter is never charged to the candidate's
  forecast-level budget
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
  from the decision state or truncated from the row window) pins the
  candidate's attempt exactly when the id's cycle is the candidate's
  own cycle AND the id's stage is the repair target (the id ends
  with the state's failed stage, or with no failed stage the
  candidate has no live failure of its own) — so an operator's
  manual retry of the candidate's own cohort cycle stage stays
  effective even though the row is invisible, while a
  foreign-cycle or cross-stage cycle counter still never charges
  the candidate's budget; markers with other unresolvable entity
  ids keep their existing pinning behavior

#### Scenario: Own-model markers and blocker exclusion keep their semantics

- **WHEN** a manual retry event targets one of the candidate's own
  model-scoped jobs, or a foreign marker-shaped event (e.g.
  `status_to` `pending`) coexists with the candidate's own marker
- **THEN** the own-model marker is adopted unchanged with
  `new_attempt` matching its `retry_count`, and the foreign
  marker-shaped event is not treated as an active blocker — the
  candidate's `manual_retry_requested` remains truthful

