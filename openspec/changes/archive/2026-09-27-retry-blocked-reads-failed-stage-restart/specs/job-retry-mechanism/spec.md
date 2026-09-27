## MODIFIED Requirements

### Requirement: Retry-lane consumers of a read-blocked journal row SHALL refuse or degrade, never answer about the work

The file journal's query entrypoints synthesise one row for a refused read, marked by `file_journal.status == "blocked"` and carrying the fault's `reason` and `field`. That row SHALL remain PRESENT and non-terminal so the duplicate-submission and active-cycle guards keep refusing, and its shape — field set, `job_id` defaults, marker keys, reason token, status literal — SHALL NOT change.

Five retry-lane consumers SHALL identify that row by the `file_journal.status == "blocked"` marker — never by comparing `job_id`, and never by the status literal where the marker is available — and SHALL take exactly one of two treatments: refuse with the lane's stable classified error carrying the journal's `reason` and `field`, or return the lane's "no evidence" value while logging that `reason` and `field`. None of them SHALL translate the row into a claim about the run or the job — neither "this run has nothing to retry", nor a retry/permanent-failure classification, nor an identity fault.

The five are: the manual-retry source selector and the chain stage-result retry classifier, which SHALL refuse — the latter with a classified `FILE_JOURNAL_READ_BLOCKED` error rather than by returning no decision — and the two runtime-root provenance readers and the runtime-root walk's own same-run companion read, which SHALL degrade to their empty result with a warning. Every such degrade SHALL be counted, and the runtime-root resolution evidence SHALL carry `candidate_counts.blocked_reads` whenever the count is non-zero, so a blocked read is never byte-identical to genuinely absent provenance (evidence without a blocked read is unchanged). When any runtime-root provenance read of an attempt was blocked, runtime roots SHALL be required for that attempt whatever its job type, and no root or selector value SHALL be taken from the current environment: roots and db-free selectors SHALL resolve only from recorded provenance, while the environment's db-free policy switch (`NHMS_SCHEDULER_DB_FREE_REQUIRED`), which is a mode flag and not a root value, SHALL still make the db-free selectors required. Otherwise the attempt SHALL end with the classified `RETRY_RUNTIME_ROOTS_UNRESOLVED` outcome carrying `blocked_reads`, never a submission rooted in, or reading roots from, the current environment. Downstream consumers reached only through those entries keep their existing indirect fail-closed behaviour and are out of scope. A consumer reading a mapping that carries no `file_journal` marker SHALL treat it as a normal row. Operator entrypoints built on the manual-retry source selector SHALL report the refusal as a decidable receipt outcome rather than an uncaught error.

#### Scenario: Manual-retry source selection on an unreadable by-run read

- **WHEN** the by-run pipeline-job read is refused by a typed journal fault while a manual retry is attempted for that run
- **THEN** the attempt SHALL raise the `RETRY_EVIDENCE_INVALID` family error carrying the run id and the journal `reason`/`field`, SHALL NOT raise `RETRY_NOT_FOUND`, and SHALL write no retry row, no marker event and no gateway request

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

#### Scenario: Runtime-root provenance readers degrade instead of poisoning the recorded failure

- **WHEN** the predecessor-job read or the event-scan read behind the manual-retry route is refused by a typed journal fault, after the pending retry row has been minted
- **THEN** those readers SHALL return an empty candidate batch and no predecessor respectively and SHALL log the journal `reason` and `field`, and the attempt SHALL NOT be recorded as a gateway submission failure whose code and message were derived from that refused read

#### Scenario: The operator CLI reports the refusal as a receipt outcome

- **WHEN** the node-22 manual-retry operator entrypoint previews a run whose by-run read is refused
- **THEN** it SHALL emit a refused decision naming the journal read as the reason, with the journal `reason` and `field`, and SHALL NOT terminate with an uncaught error or a "no retryable failed job" verdict

#### Scenario: The blocked row keeps its load-bearing guards

- **WHEN** the unchanged blocked row reaches the duplicate-submission guard, the active-job predicate or the auto-retry reuse predicate
- **THEN** each SHALL return the same verdict it returns today — conflict, active, and not reusable — and the retry-id allocator SHALL be unreachable for a blocked read because the selector refuses first

#### Scenario: A blocked same-run companion read is not "no companion job"

- **WHEN** the runtime-root walk's same-run by-run read returns the blocked row
- **THEN** the walk logs the journal `reason`/`field`, contributes no companion candidate, and the persisted resolution evidence carries `blocked_reads >= 1`, unlike a run that genuinely has no companion download job

#### Scenario: A blocked provenance read never falls back to the environment root

- **WHEN** a provenance read of a manual retry of any job type (download or not, db-free-required or not) is blocked, no recorded candidate resolves, and the current-environment candidate would be complete
- **THEN** no submission is made with the environment root, the attempt ends with `RETRY_RUNTIME_ROOTS_UNRESOLVED`, and its evidence carries `blocked_reads >= 1`

#### Scenario: Genuinely absent provenance keeps the environment fallback

- **WHEN** no provenance read was blocked and the job simply has no submission event
- **THEN** the environment candidate is used exactly as before and the evidence carries no `blocked_reads` key

#### Scenario: The db-free policy still binds under a blocked read

- **WHEN** a provenance read is blocked, the environment requires db-free execution, and the only recorded candidate carries complete roots but no db-free selector
- **THEN** no submission is made and the attempt ends with `RETRY_RUNTIME_ROOTS_UNRESOLVED` carrying `blocked_reads`
