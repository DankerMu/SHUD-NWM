## MODIFIED Requirements

### Requirement: Surviving retry-lane consumers of a read-blocked journal row SHALL refuse, never answer about the work

The file journal's query entrypoints synthesise one row for a refused read, marked by `file_journal.status == "blocked"` and carrying the fault's `reason` and `field`. That row SHALL remain PRESENT and non-terminal so the duplicate-submission and active-cycle guards keep refusing, and its shape — field set, `job_id` defaults, marker keys, reason token, status literal — SHALL NOT change.

Two retry-lane consumers SHALL identify that row by the `file_journal.status == "blocked"` marker — never by comparing `job_id`, and never by the status literal where the marker is available — and SHALL refuse with the lane's stable classified error carrying the journal's `reason` and `field`. Neither of them SHALL translate the row into a claim about the run or the job — neither "this run has nothing to retry", nor a retry/permanent-failure classification, nor an identity fault.

The two are: the manual-retry source selector (shared by the manual-repair marker lane of the compute role, `FileJournalRetryService.record_manual_repair`) and the chain stage-result retry classifier, the latter refusing with a classified `FILE_JOURNAL_READ_BLOCKED` error rather than by returning no decision. Downstream consumers reached only through those entries keep their existing indirect fail-closed behaviour and are out of scope. A consumer reading a mapping that carries no `file_journal` marker SHALL treat it as a normal row. Operator entrypoints built on the manual-retry source selector SHALL report the refusal as a decidable receipt outcome rather than an uncaught error.

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

- **WHEN** the manual-retry operator entrypoint of the compute role previews a run whose by-run read is refused
- **THEN** it SHALL emit a refused decision naming the journal read as the reason, with the journal `reason` and `field`, and SHALL NOT terminate with an uncaught error or a "no retryable failed job" verdict

#### Scenario: The blocked row keeps its load-bearing guards

- **WHEN** the unchanged blocked row reaches the duplicate-submission guard, the active-job predicate or the auto-retry reuse predicate
- **THEN** each SHALL return the same verdict it returns today — conflict, active, and not reusable — and the retry-id allocator SHALL be unreachable for a blocked read because the selector refuses first

### Requirement: DB-free scheduler retry manifests preserve runtime mode

Scheduler retry submissions for the compute role's DB-free scheduler operation SHALL preserve the
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
- **AND** the compute role's production requires direct-grid forcing
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
  configured global Slurm array bound
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
  than the configured aggregate bound, which itself MUST NOT exceed 64
- **AND** repair configuration requesting an aggregate Slurm array concurrency
  above 64 MUST be rejected before submission.

#### Scenario: downstream canonical rejection preserves the original blocker

- **WHEN** raw and warm repair preconditions pass but current canonical input is
  not ready
- **THEN** the candidate MUST retain top-level
  `missing_forcing_package_uri` / `FORCING_PACKAGE_URI_MISSING` classification
- **AND** canonical rejection details MUST be recorded only as nested repair
  evidence with zero submission.

#### Scenario: DB-free deployment supplies trusted raw authority

- **WHEN** an operator provisions the compute control host from
  `infra/env/compute.scheduler-dbfree.env.example` and the documented exact-cycle
  wrapper
- **THEN** the runtime SHALL receive the explicit trusted raw root
  `/ghdc/data/nwm/object-store`, the canonical copyback authority
  `NHMS_OBJECT_STORE_COPYBACK_ROOT=/ghdc/data/nwm/object-store`, and object
  prefix `s3://nhms`
- **AND** the root SHALL be allow-listed and use the existing canonical shared-
  NFS copyback mount rather than a duplicate mount declaration
- **AND** runtime preflight SHALL bind both the resolved trusted raw root and
  resolved copyback authority to the fixed topology identity of the compute hosts
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
