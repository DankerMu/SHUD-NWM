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
resolved job's stage or the candidate has no live candidate-scoped
failure of its own. The live-failure domain matches the failure
half of the module's blocker STATUS domain, not the narrower
failed-pipeline status set alone — read from candidate-scope job
rows and the candidate's own hydro run only (the blocker scan's
state-level `pipeline_status` and pipeline-event sources are not
live-failure sources here: a top-level failed `pipeline_status`
records the cycle failure being repaired, and counting it would
make the only-failure-left arm unreachable; on the production read
paths this exclusion is enforced by projection shape — a surviving
marker proves real job rows exist beside it — rather than by an
in-module filter, and hardening the module against synthesized
job-row-less shapes is tracked as #1299): a
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
job is no longer failed (stale) — the derived
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
only raises the caller's value). In that unnameable-stage case a
candidate whose visible family-stage row already consumed attempt
N derives at least N + 1 whenever that family stage is itself a
canonical downstream restart stage; at a non-canonical family
stage the derivation degenerates to the candidate's flat record
and the floor adds nothing (tracked as #1298); with no live
failure at all the fallback stays `previous_attempt + 1`. Marker-shaped events remain excluded from
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
`new_attempt`. The exclusion applies to candidate-state membership
only: the cycle-level duplicate-submission and completion gates (the
active-pipeline, completed-pipeline, and active-slurm-jobs scans)
keep their wider unconditional cycle-run visibility unchanged by this
rule — the DB read path's active-pipeline and active-slurm-jobs gates
deliberately share that wider visibility, while its
completed-pipeline gate reads the candidate's own hydro run alone and
so has no job-row counterpart to align with here. On the
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
  completed-pipeline, active-slurm-jobs) answer exactly as before
  the exclusion — the row stays visible to those scans

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
  the job's stage, or the candidate has no live candidate-scoped
  failure of its own (the production cohort-download shape)
- **THEN** the derived `new_attempt` pins the marker's
  `retry_count`, so the operator's cycle-level manual retry stays
  effective and the minted retry identity does not reuse a consumed
  attempt number
- **AND** when the candidate's own live failure is at a different
  stage, or the marker's resolved job is no longer failed (stale),
  the derived `new_attempt` falls back to `previous_attempt + 1`;
  the pin refusal itself charges nothing, but when the candidate's
  own failed stage resolves to a canonical downstream stage the
  caller's `previous_attempt` is already that stage's stage-scoped
  derivation, so a multi-basin cohort row at that same stage is
  still counted there — pre-existing failed-stage cycle-blindness,
  unchanged by this change and tracked as #1300
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
  from the decision state or truncated from the row window) pins the
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
  within the DELIVERED DOMAIN, stated once here as a literal
  transcription of the two delivered claim families: model-less
  (cycle-scope) targets — a model-bearing `job_cycle`-grammar row
  short-circuits the resolved-row router to a pin, and no
  row-absent evidence surface carries model-ness — that EITHER are
  failed-status targets that are neither unsubmitted auto-retry
  placeholders nor repaired-flagged, OR are targets a state mapping
  names with an exact entity-id match (the repaired-stage
  evidence's original failed job id, or the completed-stage
  evidence's job id — the latter existing only for stages with a
  successor in the forecast stage order); every shape outside this
  domain is a disclosed residue, not a delivered identity
- **AND** the journal marker event written by a manual repair
  carries the failed job's stage as a `failed_stage` detail — a key
  the candidate-state record-stage reader does not consume (so
  terminal-stage gating never drops the marker event itself) and
  one the identity-filter event sanitizer preserves on retry
  events — so markers written from now on decide by record rather
  than id text wherever their details survive to adoption (the
  journal read path's completion-stage compaction domain keeps the
  disclosed id-token backstop)
- **AND** a marker whose target the state-level repaired-stage
  evidence names as its original failed job — or whose target the
  state-level completed-stage evidence names as its completed job —
  refuses the pin with the row absent exactly as the resolved-row
  rule refuses it with the row present, within the delivered
  domain; those two mapping-named sub-shapes are the only staleness
  classes with row-absent evidence
- **AND** with the marker's stage differing from the candidate's
  failed stage, the verdict falls through to the only-failure-left
  arm — the same arm the resolved-row rule uses on a stage
  mismatch — and, within the delivered domain, for a failed-status
  target lands on the same verdict as the resolved-row rule on the
  same state
- **AND** markers with non-cycle-grammar entity ids keep the
  historical fail-open, a foreign-cycle id still never pins, and a
  stage-less marker keeps deciding through the loop-stripped id
  token

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

