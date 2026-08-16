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

#### Scenario: Classification parity between this requirement and code is test-anchored

- **WHEN** the repository test suite runs
- **THEN** a test SHALL read this requirement's non-transient code list from the spec text and assert that `OUT_OF_MEMORY` appears there, is a member of the orchestrator's non-transient classification set, and is absent from every transient classification surface (`TRANSIENT_ERROR_CODES` and the scheduler-state transient retry-reason set), so that a reopened spec-code drift on this code fails the suite rather than surviving to review

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

When a candidate carries a failure signal and the forcing package its forecast stage references does not exist in the configured object-store root, the scheduler SHALL NOT emit another forecast retry from any decision branch — including the failure fallback and the permanent-failure branch — and SHALL instead emit the stable missing-forcing blocker (reason `missing_forcing_package_uri`, stable classifier, `artifact_exists` false, forecast restart stage) that the explicit single-cycle repair authorization channel accepts. Before treating a state with no forcing package reference as missing, the scheduler SHALL attempt provenance recovery through the witnessed read tiers (journal row, journal direct file, object-store forcing-version sidecar record derived from the candidate identity); a redaction placeholder standing in for a withheld URI is not a probeable package reference and SHALL take this recovery path rather than the recorded-URI probe (the public-read redaction boundary is never bypassed and the probe itself is never taught about placeholders). For the sidecar tier the existing artifact existence probe SHALL target the package manifest file key derived from the candidate-derived sidecar key (never the directory-shaped package URI, which the object-path validator rejects, and never the record's own manifest URI taken verbatim — that recorded URI serves as corroborating evidence only), so that a physically present package never produces the missing-forcing blocker, a witnessed-absent package produces exactly the unchanged `missing_forcing_package_uri` blocker, and a sidecar record pointing at a foreign manifest cannot stand in as this candidate's witness. A probe-layer store error after a successful sidecar witness SHALL be contained fail-closed (blocked, never an escaped exception aborting the scheduler pass) and SHALL classify as no-witness rather than as a determined-missing package, because an unreadable probe object is a read fault a package rebuild cannot clear. The sidecar read limit SHALL admit the provenance records the forcing producer actually writes in production, and a record exceeding that limit SHALL be reported with its own tier detail, distinct from a permission or I/O read failure. Only when no tier yields a witness SHALL the decision block with the distinct reason `forcing_version_row_absent` (error code and stable classifier `FORCING_VERSION_ROW_ABSENT`, null artifact reference, provenance source marked absent), which carries the same structural repair-eligible contract, and the single-cycle repair authorization channel — including its stable-classifier structural check — SHALL accept both blocker reason/classifier pairs. Provenance-tier failures (unreadable, malformed, oversized sidecar, unconfigured store, incomplete identity) SHALL classify as no-witness rather than as a determined-missing package, and SHALL never fail open into a retry. Failure classification produced inside the compute task SHALL survive the DB-free path to array accounting through a durable per-task outcome receipt, with generic `NODE_FAILURE` used only as the fail-safe when no receipt is readable. The effective retry attempt SHALL derive from the durable per-stage attempt record (job-identity retry suffix) on both the in-stage retry gate and the scheduler's cross-pass failure policy, so that the configured retry limit bounds retries even for misclassified failures.

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
placeholder codes (defaults fabricated when the state records no error code)
are not evidence under that clause and keep their existing behavior,
including the existing classifier-based refusals; the raw-manifest and
model-package channels consult the judgement for every permanent
classification, recorded code or not.

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
  `OUTPUT_INCOMPLETE`) or unknown and defaulted non-transient (e.g. a
  recorded `PARSE_FAILED`, `SLURM_JOB_FAILED`), or whose retry budget is
  exhausted
- **THEN** the downstream-resume channel SHALL NOT emit a resume decision;
  a recorded transient code within budget SHALL keep the existing resume
  behavior unless the state explicitly marks the failure permanent
  (top-level `permanent: true`, which forces permanence — see the top-level
  key scenario below)

#### Scenario: Synthesized placeholder codes keep existing downstream behavior

- **WHEN** a downstream failure state records no error code and the reader
  synthesizes a stage-derived placeholder for classification
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

