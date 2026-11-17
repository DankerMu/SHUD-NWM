## REMOVED Requirements

### Requirement: Manual Retry via API

**Reason**: The `POST /api/v1/runs/{run_id}/retry` route no longer submits anything (issue #3157, owner decision b): both `attempt_manual_retry` submission lanes (DB `RetryService`, file `FileJournalRetryService`) are retired, so creating a retry `pipeline_job`, submitting it via `sbatch` and appending the `trigger: "manual"` event on request are unreachable. The route now always answers 409 `CONTROL_PLANE_MANUAL_ACTION_REQUIRED` (see ops-monitoring-api).

**Migration**: Operators record a manual repair through `FileJournalRetryService.record_manual_repair`, driven by `scripts/node22_manual_retry_failed_runs.py` (runbook `node22-control-plane-manual-recovery`); the scheduler mints the retry attempt. Manual override of the automatic retry limit remains specified by "Geometry-B manual retry minting SHALL recover stage from exact marker-floor lineage" and the strict-warm-start budget requirements.

### Requirement: Concurrent Retry Protection

**Reason**: Scenario 1 specifies concurrent `POST .../retry` submissions and a `RETRY_ALREADY_IN_PROGRESS` 409 backed by a unique constraint; the route no longer submits and `RETRY_ALREADY_IN_PROGRESS` does not exist in the code. Scenario 2 specifies an `auto_retry_skipped` event with reason `manual_retry_already_active`; that string does not exist in the code either (the requirement was already unimplemented before this change).

**Migration**: Active-retry conflicts on the marker lane stay specified by the `RETRY_CONFLICT` / `RETRY_NOT_FOUND` / `RETRY_EVIDENCE_INVALID` distinguishability scenario of "Surviving retry-lane consumers of a read-blocked journal row SHALL refuse, never answer about the work". No replacement is added for the unimplemented auto-retry-skip scenario.

### Requirement: Retry Runtime-Root Safety Survives Symlink Loops

**Reason**: The runtime-root resolution (`RETRY_RUNTIME_ROOTS_UNSAFE` / `_SECRET_BEARING` / `_UNRESOLVED`) is reachable only from `RetryService._resolve_retry_runtime_roots` and the file lane `attempt_manual_retry` -> `_resolve_file_retry_runtime_roots`, both retired. Auto retry and the marker lane never call it.

**Migration**: None. The scheduler-side local artifact guard keeps its own symlink-loop requirements ("Local Artifact Allowed-Roots Normalization Survives Symlink Loops").

### Requirement: DB-free selector containment survives symlink loops at both the root and the path level

**Reason**: Governs the "db-free retry submission lane" selector adjudicators `_db_free_selector_allowed_roots` / `_db_free_selector_path_rejection`, whose only caller is `_resolve_db_free_runtime_candidate`, itself reachable only from the retired runtime-root resolution.

**Migration**: None; the scheduler pass has its own allowed-roots handling specified elsewhere.

### Requirement: File-journal retry attempts preserve durable warm-start lineage without claiming accepted-submit authority

**Reason**: Scenarios 1-5 and the manual half of scenario 8 specify the retired `attempt_manual_retry` row creation / post-submit update / concurrent-winner / private-rebind behaviour. The surviving automatic-retry scenarios are carried over verbatim under a new name.

**Migration**: See ADDED "File-journal automatic retry attempts preserve durable warm-start lineage without claiming accepted-submit authority".

### Requirement: Invalid file-journal retry evidence has a structured API boundary

**Reason**: Scenario 1 (private source evidence normalization before a pending row) and scenario 3 (route maps the error to 409 `RETRY_EVIDENCE_INVALID`) are submit-lane / route-success behaviour. The route can no longer reach the file journal. The shared selector refusal (scenario 2) is already carried by the ADDED "Surviving retry-lane consumers..." requirement (scenarios 1 and 2) and its CLI receipt scenario.

**Migration**: See ADDED "Surviving retry-lane consumers of a read-blocked journal row SHALL refuse, never answer about the work".

### Requirement: Operator recovery attestations do not cross retry-attempt boundaries

**Reason**: The body and scenarios 2-3 constrain the retired "manual-retry clone seam" (`_create_pending_manual_retry_job`): a newly created manual-retry row inheriting `operator_recovery_attested_at`. Scenarios 1 and 4 (selector exclusion of `reservation_lost`, sole attestation writer) are selector/recovery-API behaviour that survives and are carried over verbatim under a new name.

**Migration**: See ADDED "Released reservations are not a manual-retry source and recovery remains the sole attestation writer".

### Requirement: File-journal manual retry submission failures surface through the route's structured 503

**Reason**: Specifies the route 503 `submission_failed` response and the shared `attempt_manual_retry` / `submission_runtime_root_resolution` service seam; the route no longer submits, so neither exists.

**Migration**: The route answers 409 `CONTROL_PLANE_MANUAL_ACTION_REQUIRED` only (ops-monitoring-api).

### Requirement: Manual-retry runtime-root evidence renders one public shape on both lanes

**Reason**: Specifies the `details.runtime_root_resolution` mapping of the retired route 503 and the manual-retry submission events that only the retired lanes write. The anti-laundering guarantee quoted in its body is independently specified in pipeline-job-persistence ("A cohort projection write cannot launder a placeholder" and the placeholder-write rejection scenario).

**Migration**: None.

### Requirement: Public job error messages SHALL render local paths and URIs

**Reason**: The first sentence and scenario "Gateway error with absolute path in retry 503" bind the retry route `submission_failed` 503 body, which no longer exists. The clause that pipeline events keep raw text, its scenario "Persisted event keeps raw text", and the sentence that the database and file-journal lanes render the same text identically were pinned only by the deleted retry-503 tests (the retired lanes were the pinned writers of those events), so they are removed rather than carried as unpinned MUSTs. The job/basin result rendering and the raw persisted job row are carried over under a new name.

**Migration**: See ADDED "Public job error messages SHALL render local paths and URIs on pipeline job payloads".

### Requirement: Retry-lane consumers of a read-blocked journal row SHALL refuse or degrade, never answer about the work

**Reason**: Three of the five named consumers (the two runtime-root provenance readers and the runtime-root walk companion read) and scenarios "Runtime-root provenance readers degrade...", "A blocked same-run companion read...", "A blocked provenance read never falls back...", "Genuinely absent provenance keeps...", "The db-free policy still binds..." exist only in the retired runtime-root resolution. The two surviving consumers (source selector shared with `record_manual_repair`, chain stage-result classifier) keep their scenarios verbatim under a new name.

**Migration**: See ADDED "Surviving retry-lane consumers of a read-blocked journal row SHALL refuse, never answer about the work".

### Requirement: DB-free selector and object-store probe lanes survive unexpandable tildes

**Reason**: Clause (a) and the scenarios "unexpandable tilde in db-free selector values yields structured rejections" and the selector half of "expandable and tilde-free values keep their behavior" govern the retired db-free selector adjudicators. Clause (b) (object-store probe lane) is shared with the scheduler and is carried over.

**Migration**: See ADDED "Object-store probe lanes survive unexpandable tildes".

### Requirement: DB-free retry manifests preserve runtime mode

**Reason**: Scenario "manual forecast retry runs without database url" is pinned by `tests/test_retry.py::test_manual_forecast_retry_preserves_db_free_runtime_contract`, which drives `RetryService.attempt_manual_retry` (retired); a requirement cannot drop a scenario in place, so the requirement is re-added under a new name without it. All other scenarios are carried over verbatim.

**Migration**: See ADDED "DB-free scheduler retry manifests preserve runtime mode". No test pins the marker -> scheduler mint path for this scenario, so the spec does not claim it; the surviving scenarios cover the scheduler retry manifests. PR 2 re-pins "automatic forecast retry runs without database url" on the live path (`tests/test_db_free_auto_retry_manifest.py`); its evidence clause is restated to what that path records: the live path does not populate `previous_job_id` on the retry row and records no DB-free-mode field in the journal (both are carried by the retry identity and the submitted manifest), keeps `retry_count` at 0 with the attempt in `submission_attempt`, and keeps member model ids in `cohort_members`.


## MODIFIED Requirements

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

- **WHEN** an operator records a manual repair through `FileJournalRetryService.record_manual_repair`
- **THEN** a `pipeline_event` record SHALL be appended with:
  - `details_json` containing:
    - `trigger` — `"manual"`
    - `requested_by` — the requesting operator's identifier, when one was supplied
    - `previous_job_id` — the failed job's `job_id`
    - `retry_count` — the current retry attempt number

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
- **THEN** the job SHALL still be eligible for manual retry through the file-journal manual-repair marker lane (`FileJournalRetryService.record_manual_repair`, operator override)
- **THEN** the operator override SHALL reset the context but preserve the full retry history in `pipeline_event`

### Requirement: Geometry-B manual retry minting SHALL recover stage from exact marker-floor lineage

When candidate row truncation leaves `_candidate_failed_stage` unresolved but an adopted manual-retry marker targets a row represented by an authoritative `stage_retry_attempt_floor_sources` contributor, only the manual retry gate/evidence lane SHALL recover a canonical stage, and only from an exact target-identifier match between the newest adopted marker and a contributor. The ordinary candidate decision state SHALL retain its existing shared-cycle pipeline-surface strip; a separate manual-retry view built by the same authority filter MAY carry only a minimal lineage capsule (that newest marker plus the one matching canonical stage's already-narrowed floor and sources) across that strip. It SHALL NOT restore the truncated row, any top-level stage/status/retry field, unrelated floors/events, or an older matching marker when the newest adopted marker does not match. A recorded marker `failed_stage`, when present, SHALL agree with the contributor stage after both are interpreted through canonical downstream-stage identity; the floor-source mapping key itself SHALL use the canonical spelling emitted by the projection producer and an alias-spelled or non-canonical hand-shaped key SHALL NOT authorize recovery. Marker target identity consists of event `entity_id` and recorded `previous_job_id` / `failed_job_id`; contributor identity consists of its own `job_id` / `pipeline_job_id` / `entity_id`. Contributor predecessor/reference fields and marker `details.job_id` SHALL NOT authorize recovery. The system SHALL use that canonical stage's carried attempt as `previous_attempt` and mint `new_attempt = previous_attempt + 1`; the manual override SHALL remain allowed regardless of the automatic retry limit. The file-journal manual-repair marker writer SHALL derive its marker attempt as `effective_retry_attempt(target_job_id, target_retry_count) + 1`, so a suffix-only target cannot emit a stale attempt-one claim that overrides the recovered floor. Projection visibility SHALL remain unchanged: the failed row stays outside `pipeline_jobs`, top-level `failed_stage`/`stage`/`restart_stage` stay empty, and `_failed_stage` plus `_candidate_failed_stage` remain unresolved. If lineage is absent, foreign, stale, non-canonical, maps to multiple disagreeing stages, or belongs only to an older marker behind a newer unmatched adopted marker, the manual lane SHALL fail closed rather than guess.

#### Scenario: Geometry B mints the next durable attempt without changing visibility

- **WHEN** the failed `forecast` retry row carrying attempt `N` is outside the row window, terminal-success filler leaves all top-level stage keys empty, and the newest adopted marker exactly targets that floor contributor
- **THEN** the failed row remains outside the projection, `_failed_stage` and `_candidate_failed_stage` still return no stage, the ordinary shared-cycle decision state still strips the pipeline surface, and the dedicated manual-retry decision lane preserves only the exact narrowed lineage needed to report `previous_attempt == N` and `new_attempt == N+1`, preventing reuse of an already-consumed `_retry_1` identity

#### Scenario: Producer-shaped marker uses the durable suffix attempt

- **WHEN** the target failed job records attempt `N` only in its trailing `_retry_N` identity suffix while its persisted `retry_count` is zero
- **THEN** the manual-repair marker writer records attempt `N+1` through `effective_retry_attempt` and does not emit an attempt-one claim

#### Scenario: Ordinary shared-cycle consumers stay stripped

- **WHEN** the same geometry is evaluated outside the manual retry gate/evidence lane
- **THEN** the ordinary candidate decision state carries no stage-attempt floors or model-less manual marker, preserving the existing #1179 E13b behavior for failure-policy and other consumers

#### Scenario: Newest unmatched marker does not fall through

- **WHEN** the newest adopted marker has no exact authoritative contributor match while an older adopted marker does match
- **THEN** no manual lineage capsule is carried and the older marker SHALL NOT authorize stage recovery

#### Scenario: Legacy marker can use exact contributor lineage

- **WHEN** an adopted legacy marker lacks recorded `failed_stage` but its exact `entity_id` or recorded previous job id matches a unique canonical-stage floor contributor
- **THEN** the contributor's canonical stage is recovered without parsing stage text from the job id and the mint uses that stage's floor

#### Scenario: Recorded stage aliases agree by canonical identity

- **WHEN** an adopted marker records a valid downstream alias such as `run_shud_forecast`, its exact target matches the producer's canonical `forecast` floor contributor, and no other stage matches
- **THEN** the marker and contributor agree as canonical `forecast`, and manual retry mints from that floor rather than falling back to attempt one

#### Scenario: Ambiguous or foreign lineage does not infer a stage

- **WHEN** an adopted marker has no exact authoritative contributor match, names a foreign or stale contributor, meets an alias-spelled/non-canonical floor-source key, or its target identifiers map to disagreeing canonical stages
- **THEN** no stage is inferred, unrelated floors are not charged, and the pre-change stage-less fallback result is preserved

#### Scenario: Existing manual retry precedence is unchanged

- **WHEN** the candidate failed stage is already nameable or the newest adopted marker explicitly pins a retry attempt
- **THEN** the existing nameable-stage and explicit-marker precedence remains unchanged, including manual override of the automatic retry limit

## ADDED Requirements

### Requirement: Surviving retry-lane consumers of a read-blocked journal row SHALL refuse, never answer about the work

The file journal's query entrypoints synthesise one row for a refused read, marked by `file_journal.status == "blocked"` and carrying the fault's `reason` and `field`. That row SHALL remain PRESENT and non-terminal so the duplicate-submission and active-cycle guards keep refusing, and its shape — field set, `job_id` defaults, marker keys, reason token, status literal — SHALL NOT change.

Two retry-lane consumers SHALL identify that row by the `file_journal.status == "blocked"` marker — never by comparing `job_id`, and never by the status literal where the marker is available — and SHALL refuse with the lane's stable classified error carrying the journal's `reason` and `field`. Neither of them SHALL translate the row into a claim about the run or the job — neither "this run has nothing to retry", nor a retry/permanent-failure classification, nor an identity fault.

The two are: the manual-retry source selector (shared by the node-22 manual-repair marker lane `FileJournalRetryService.record_manual_repair`) and the chain stage-result retry classifier, the latter refusing with a classified `FILE_JOURNAL_READ_BLOCKED` error rather than by returning no decision. Downstream consumers reached only through those entries keep their existing indirect fail-closed behaviour and are out of scope. A consumer reading a mapping that carries no `file_journal` marker SHALL treat it as a normal row. Operator entrypoints built on the manual-retry source selector SHALL report the refusal as a decidable receipt outcome rather than an uncaught error.

#### Scenario: Manual-retry source selection on an unreadable by-run read

- **WHEN** the by-run pipeline-job read is refused by a typed journal fault while a manual repair is recorded for that run through `record_manual_repair`
- **THEN** the attempt SHALL raise the `RETRY_EVIDENCE_INVALID` family error carrying the run id and the journal `reason`/`field`, SHALL NOT raise `RETRY_NOT_FOUND`, and SHALL write no retry row and no marker event

#### Scenario: The run's durable read is refused inside the same selector

- **WHEN** the selector's durable hydro-run read raises a typed journal fault
- **THEN** the same classified `RETRY_EVIDENCE_INVALID` error SHALL surface instead of an unclassified error escaping the retry lane

#### Scenario: Genuine absence and genuine conflict stay distinguishable

- **WHEN** the same run has no retryable job at all, or has an active job, with no journal fault
- **THEN** the lane SHALL still answer `RETRY_NOT_FOUND` and `RETRY_CONFLICT` respectively, so "nothing to retry", "busy" and "unreadable" remain three distinct answers

#### Scenario: Chain stage-result retry classification on an unreadable by-id read

- **WHEN** a failed stage result's pipeline-job read is refused and the retry service is the file-journal lane
- **THEN** the classifier SHALL raise a classified `FILE_JOURNAL_READ_BLOCKED` error carrying the journal reason, SHALL NOT construct a pipeline job from the row, SHALL NOT call the failed-job handler, and the escaping error's reason SHALL be the journal's own refusal reason rather than a derived identity fault

#### Scenario: Safety does not depend on the blocked row omitting identity

- **WHEN** the same classification path receives a blocked row that carries a real `run_id` and `cycle_id`, as the by-run and by-cycle lanes mint it
- **THEN** no `permanently_failed` status, no replacement retry row and no durable change SHALL be written, and the refusal SHALL still be the classified journal-blocked error

#### Scenario: The operator CLI reports the refusal as a receipt outcome

- **WHEN** the node-22 manual-retry operator entrypoint previews a run whose by-run read is refused
- **THEN** it SHALL emit a refused decision naming the journal read as the reason, with the journal `reason` and `field`, and SHALL NOT terminate with an uncaught error or a "no retryable failed job" verdict

#### Scenario: The blocked row keeps its load-bearing guards

- **WHEN** the unchanged blocked row reaches the duplicate-submission guard, the active-job predicate or the auto-retry reuse predicate
- **THEN** each SHALL return the same verdict it returns today — conflict, active, and not reusable — and the retry-id allocator SHALL be unreachable for a blocked read because the selector refuses first


### Requirement: Object-store probe lanes survive unexpandable tildes

The object-store probe lane SHALL never let a home-directory-determination failure escape as a bare
RuntimeError: constructing a local object store with an unexpandable tilde root SHALL raise the domain
`ObjectStoreError` from within the constructor's existing error-conversion
boundary — never a bare RuntimeError and never a literal `~…` directory
created on disk — so the unified artifact probe and the sidecar provenance
tier, which already catch `ObjectStoreError`, keep producing their existing
distinguishable fail-closed attributions instead of crashing the scheduling
pass. Expandable and tilde-free roots keep their existing behavior
byte-for-byte.

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

#### Scenario: expandable and tilde-free object-store roots keep their behavior

WHEN the object-store root has no tilde or expands normally
THEN constructor behavior is byte-for-byte identical to the pre-change
behavior

### Requirement: File-journal automatic retry attempts preserve durable warm-start lineage without claiming accepted-submit authority

The file-journal retry service SHALL derive every automatic retry attempt from the private durable predecessor row. An automatic retry attempt SHALL preserve that row's bounded `init_state_identities` exactly, SHALL identify the predecessor through `previous_job_id`, and SHALL NOT carry `accepted_submit_contract_version` because a retry attempt is not the authoritative accepted-submit row. Public redaction placeholders such as `[object-uri]`, `[local-path]`, and `[redacted]` MUST NOT be persisted as retry lineage.

#### Scenario: Automatic retry inherits durable predecessor lineage

- **WHEN** `schedule_auto_retry` receives either a full row or a narrow production snapshot for a failed job whose durable row has non-empty `init_state_identities`
- **THEN** the new durable retry payload and journal record SHALL inherit the durable predecessor mapping exactly
- **THEN** a predecessor that genuinely has no mapping SHALL produce `[]` without crashing

#### Scenario: Full-row and narrow-snapshot automatic retry agree

- **WHEN** automatic retry is invoked once with a full durable-row input and once with a narrow production snapshot for equivalent failed jobs whose private durable predecessors carry the same non-empty mapping
- **THEN** both retry attempts SHALL persist the same inherited mapping in their direct payload and corresponding journal record
- **THEN** a predecessor with genuinely empty lineage SHALL still persist `[]`

#### Scenario: Permanent-failure marking retains the existing mapping

- **WHEN** an existing failed row with a non-empty mapping is marked permanently failed
- **THEN** that sibling path SHALL retain its existing mapping

### Requirement: Released reservations are not a manual-retry source and recovery remains the sole attestation writer

`reservation_lost` SHALL remain outside `MANUAL_RETRY_SOURCE_STATUSES`, and the downstream operator-recovery predicate SHALL independently require the released status and exact authority tuple. `operator_recovery_attested_at` SHALL remain authority bound to the exact released source row. The dedicated typed recovery API and the attested source row SHALL remain unchanged.

#### Scenario: Released reservation is not a generic manual-retry source

- **WHEN** manual-retry source selection observes an attested row with `status=reservation_lost` and `reconciliation_decision=identity_mismatch_released`
- **THEN** it SHALL NOT select that row as a manual-retry source, because `reservation_lost` remains outside `MANUAL_RETRY_SOURCE_STATUSES`

#### Scenario: Dedicated recovery remains the sole attestation writer

- **WHEN** `recover_released_identity_blocked_reservation` receives an eligible current-contract released master and exact attempt expectations
- **THEN** it SHALL retain its existing typed behavior and remain the sole writer of `operator_recovery_attested_at`

### Requirement: Public job error messages SHALL render local paths and URIs on pipeline job payloads

The system SHALL render public job error messages through the shared renderer. Job/basin
result payloads returned by pipeline routes (job listings, stage summaries, and the cancel response) SHALL render `error_message` through the shared public
evidence renderer: secrets become `[redacted]`, absolute local paths `[local-path]`, URIs
`[uri]`/`[object-uri]`, while surrounding words are kept. Persisted job rows keep raw text.

#### Scenario: Gateway error with absolute path in a job listing

- **WHEN** a job whose recorded error is `sbatch: error: cannot open /srv/nhms/workspace/run-42/job.sbatch for writing` is returned by a pipeline job listing
- **THEN** `error_message` equals `sbatch: error: cannot open [local-path] for writing` and the body contains no workspace or object-store root

#### Scenario: Persisted job row keeps raw text

- **WHEN** the same job is read back from the store
- **THEN** its persisted `error_message` still contains the raw path

### Requirement: DB-free scheduler retry manifests preserve runtime mode

Scheduler retry submissions for node-22 DB-free scheduler operation SHALL preserve the
same runtime repository and backend selectors as initial scheduler submissions.

#### Scenario: automatic forecast retry runs without database url

- **WHEN** a failed forecast task is automatically retried in DB-free scheduler
  mode
- **THEN** the submitted Slurm manifest MUST include
  `scheduler_db_free_required=true` and the canonical file backend selectors
  needed by the SHUD runtime
- **AND** the retry MUST be runnable without `DATABASE_URL`
- **AND** the retry's journal identity (`<prior>_retry_<N>` job id, idempotency key and
  Slurm comment) MUST identify the prior job and the retry attempt, the retry row MUST
  carry the stage, source/cycle identity, member model ids and an incremented
  `submission_attempt`, and its `submission` event MUST record the new Slurm job id;
  the DB-free runtime mode is evidenced by the submitted manifest.

#### Scenario: missing upstream forcing blocks downstream retry

- **WHEN** retry or resume targets a downstream forecast stage whose referenced
  `forcing_package_uri` no longer exists in the configured object-store root
- **THEN** the scheduler MUST block or restart from the correct upstream stage
  before submitting forecast work
- **AND** the failure MUST use a stable artifact/copyback classifier rather than
  generic `NODE_FAILURE`.

#### Scenario: operator repairs one exact cycle from verified raw input

- **GIVEN** the default missing-forcing policy remains fail-closed
- **AND** node-22 production requires direct-grid forcing
- **AND** the node-27 NFS raw manifest and all referenced raw files are present,
  ready, and match the requested source and exact cycle
- **AND** the manifest payload URI exactly equals the configured canonical
  `s3://nhms` prefix plus that exact source/cycle manifest key
- **AND** every manifest entry `local_key` is below the exact
  `raw/<normalized-source-variant>/<YYYYMMDDHH>/` prefix for that requested
  source and cycle, with the same validation applied again before staging
- **AND** the trusted NFS raw root comes from scheduler configuration rather
  than a redacted public journal field
- **AND** every selected candidate has a complete validated warm-state identity
  containing state id, URI, checksum, valid time, and lineage
- **WHEN** an operator runs the exact-cycle wrapper with
  `--repair-missing-forcing`
- **THEN** only a candidate blocked solely by `FORCING_PACKAGE_URI_MISSING`
  SHALL be reclassified to restart at `forcing`
- **AND** planning mode SHALL record an authorized/rejected policy decision
  without submitting work
- **AND** submit mode SHALL execute the existing `ForecastOrchestrator` chain
  beginning with `produce_forcing_array`, retaining the source/cycle cohort and
  global Slurm `%32` array bound
- **AND** the scheduler MUST NOT invoke login-node forcing, submit forecast
  directly, fall back to cold start, or replace the selected warm-state lineage.
- **AND** a terminal forcing job from a prior attempt MUST NOT suppress the new
  versioned forcing retry reservation
- **AND** public scheduler evidence MUST redact local raw roots and paths.

#### Scenario: repair pass preserves ordinary sibling warm admission

- **GIVEN** repair mode is enabled for one exact cycle
- **WHEN** another candidate in that pass has no classified state, an ordinary
  retry or other blocker, a different target cycle, or any classification other
  than the stable `FORCING_PACKAGE_URI_MISSING` blocker
- **AND** that candidate fails strict warm-start admission
- **THEN** the candidate SHALL retain the ordinary typed warm-start blocker
- **AND** it SHALL produce no forcing, forecast, local producer, orchestrator,
  or Slurm work
- **AND** initial classification and every post-Slurm-sync reclassification
  SHALL apply this same candidate-scoped admission decision.

#### Scenario: exact-cycle forcing repair preconditions fail closed

- **WHEN** the explicit repair flag is absent, the target cycle is missing or
  malformed, the candidate is outside that exact cycle, direct-grid is not the
  required and valid model contract, the forcing reference is unsafe, or the
  raw manifest is absent, stale, unreadable, or identity-mismatched, or warm
  state is missing, partial, cold/cutover, or identity-mismatched
- **THEN** the original missing-forcing blocker SHALL remain blocked
- **AND** an explicitly requested but rejected repair SHALL record a stable
  rejection reason and SHALL NOT submit Slurm or SHUD work.

#### Scenario: exact-cycle forcing repair preserves aggregate concurrency bound

- **WHEN** GFS and IFS repair cohorts are eligible in the same scheduler pass
- **THEN** their simultaneously active Slurm array throttles MUST sum to no more
  than 32
- **AND** repair configuration requesting an aggregate Slurm array concurrency
  above 32 MUST be rejected before submission.

#### Scenario: downstream canonical rejection preserves the original blocker

- **WHEN** raw and warm repair preconditions pass but current canonical input is
  not ready
- **THEN** the candidate MUST retain top-level
  `missing_forcing_package_uri` / `FORCING_PACKAGE_URI_MISSING` classification
- **AND** canonical rejection details MUST be recorded only as nested repair
  evidence with zero submission.

#### Scenario: DB-free deployment supplies trusted raw authority

- **WHEN** an operator provisions node-22 from
  `infra/env/compute.scheduler-dbfree.env.example` and the documented exact-cycle
  wrapper
- **THEN** the runtime SHALL receive the explicit trusted raw root
  `/ghdc/data/nwm/object-store`, the canonical copyback authority
  `NHMS_OBJECT_STORE_COPYBACK_ROOT=/ghdc/data/nwm/object-store`, and object
  prefix `s3://nhms`
- **AND** the root SHALL be allow-listed and use the existing canonical shared-
  NFS copyback mount rather than a duplicate mount declaration
- **AND** runtime preflight SHALL bind both the resolved trusted raw root and
  resolved copyback authority to the fixed node-22 topology identity
  `/ghdc/data/nwm/object-store`, as well as require them to equal each other, so
  rebinding both mutable values to an arbitrary readable allow-listed staging
  root cannot authorize repair
- **AND** repair runtime preflight SHALL bind the configured raw-manifest
  prefix to canonical `s3://nhms`; another S3 authority or a nested prefix
  such as `s3://nhms/alternate` cannot authorize repair
- **AND** explicit repair SHALL run this fixed-topology raw-authority preflight
  before lock acquisition in both plan and submit modes even when the ordinary
  DB-free scheduler flag is absent or disabled
- **AND** missing, mismatched, relative, outside-boundary, or malformed
  authority/root/prefix values SHALL fail static/runtime preflight without
  exposing either local path in public evidence.

#### Scenario: staging revalidates exact manifest object identity before mutation

- **WHEN** trusted raw readiness is staged to a compute-visible object store
- **THEN** the manifest key MUST exactly identify
  `raw/<normalized-source-variant>/<YYYYMMDDHH>/manifest.json`
- **AND** the supplied manifest path MUST resolve within the trusted source root
  to the same containment-protected file as `<source-root>/<manifest-key>`
- **AND** the payload manifest URI MUST equal the configured source prefix plus
  that exact manifest key
- **AND** wrong-source, wrong-cycle, wrong-filename, mismatched-path, or
  cross-prefix evidence MUST fail with a stable reason before target directory
  creation, destination lock acquisition, or target mutation, including when
  source and target roots are the same.
