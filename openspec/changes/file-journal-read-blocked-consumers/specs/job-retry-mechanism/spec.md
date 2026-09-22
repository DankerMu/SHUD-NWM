# Job retry mechanism — file-journal read-blocked consumers and file-lane URI evidence

## ADDED Requirements

### Requirement: Retry-lane consumers of a read-blocked journal row SHALL refuse or degrade, never answer about the work

The file journal's query entrypoints synthesise one row for a refused read, marked by `file_journal.status == "blocked"` and carrying the fault's `reason` and `field`. That row SHALL remain PRESENT and non-terminal so the duplicate-submission and active-cycle guards keep refusing, and its shape — field set, `job_id` defaults, marker keys, reason token, status literal — SHALL NOT change.

Four retry-lane entry consumers SHALL identify that row by the `file_journal.status == "blocked"` marker — never by comparing `job_id`, and never by the status literal where the marker is available — and SHALL take exactly one of two treatments: refuse with the lane's stable classified error carrying the journal's `reason` and `field`, or return the lane's "no evidence" value while logging that `reason` and `field`. None of them SHALL translate the row into a claim about the run or the job — neither "this run has nothing to retry", nor a retry/permanent-failure classification, nor an identity fault.

The four are: the manual-retry source selector and the chain stage-result retry classifier, which SHALL refuse — the latter with a classified `FILE_JOURNAL_READ_BLOCKED` error rather than by returning no decision — and the two runtime-root provenance readers, which SHALL degrade to their empty result with a warning. Downstream consumers reached only through those entries keep their existing indirect fail-closed behaviour and are out of scope. A consumer reading a mapping that carries no `file_journal` marker SHALL treat it as a normal row. Operator entrypoints built on the manual-retry source selector SHALL report the refusal as a decidable receipt outcome rather than an uncaught error.

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

## MODIFIED Requirements

### Requirement: Invalid file-journal retry evidence has a structured API boundary

File-journal identity or evidence validation failures encountered while creating a manual retry SHALL fail before a retry row is written and SHALL be exposed as a stable `RetryError` family result. A refused file-journal read encountered while selecting the manual-retry source — whether it surfaces as the marked read-blocked row or as a typed journal fault raised by the selector's durable read — SHALL take the same boundary and SHALL carry the journal's `reason` and `field` in safe details. The monitoring API SHALL return HTTP 409 with code `RETRY_EVIDENCE_INVALID` and safe details rather than an unclassified HTTP 500 or a `RETRY_NOT_FOUND` answer.

#### Scenario: Invalid retry evidence is rejected before mutation

- **WHEN** private durable retry source evidence cannot satisfy file-journal normalization
- **THEN** manual retry SHALL raise `RetryError` code `RETRY_EVIDENCE_INVALID` with the affected `run_id` and stable journal field/code in safe details
- **THEN** no pending retry payload or retry event SHALL be written

#### Scenario: A refused source read takes the same boundary

- **WHEN** the manual-retry source selection reads a refused journal lane rather than an invalid row
- **THEN** the same `RETRY_EVIDENCE_INVALID` result SHALL be raised with the journal `reason` and `field`, and no retry row, marker event or gateway request SHALL be produced

#### Scenario: Monitoring API maps invalid retry evidence

- **WHEN** `POST /api/v1/runs/{run_id}/retry` encounters that retry evidence error
- **THEN** the response SHALL have status 409 and `error.code == "RETRY_EVIDENCE_INVALID"`
- **THEN** the response SHALL not expose secrets, private URIs, or a raw runtime traceback

### Requirement: Manual-retry runtime-root evidence renders one public shape on both lanes

The `details.runtime_root_resolution` mapping carried by the retry route's `submission_failed` 503 SHALL have one public shape regardless of which retry lane produced it. Every scalar that is an absolute local path SHALL be rendered as `[local-path]`, every URI as `[uri]` or `[object-uri]`, and the response body SHALL contain no absolute local root text. Each `resolved.*` entry SHALL remain a mapping carrying `present`, `source` and the rendered `value` (plus `same_as_workspace` on `object_store_root` when both roots resolved); the public renderer SHALL recurse into mapping values under path-shaped keys instead of replacing them, while scalar values under those keys SHALL still render as `[local-path]`. The database lane SHALL apply that rendering when it reads the evidence for the response and SHALL leave the persisted event details unchanged; the file-journal lane SHALL apply it when it persists the evidence and SHALL return the persisted mapping unchanged.

A URI-valued root SHALL render as `[uri]`/`[object-uri]` on both lanes and SHALL NOT be reduced to `null` on either. The file-journal lane SHALL therefore render its manual-retry submission events — both the failure event and the successful-submission event — at the journal's own event boundary, after the durable anti-laundering strip that turns caller-supplied `[uri]`/`[object-uri]` placeholders into `null`, and SHALL NOT pre-render the evidence before writing it. No value the rendering reaches SHALL be reduced to `null` by that strip, `rejected[].value` and `db_free_runtime.resolved.*.value` included; the db-free selector's own resolved values are local paths or fixed tokens by construction, so for them the requirement is that they survive the single render unchanged. The anti-laundering guarantee for caller-supplied placeholders on non-event records and round-tripped rows SHALL remain unchanged, and no raw root text SHALL appear in the public event. Secret redaction SHALL keep precedence over path rendering. Historical file-journal events that persisted a bare `[local-path]` string, or a `null` value, for a root entry SHALL be returned as recorded.

#### Scenario: Database lane 503 carries no absolute roots

- **WHEN** a database-lane manual retry resolves real `workspace_dir` and `object_store_root` values and the gateway submission raises
- **THEN** the 503 body contains neither root's text, `resolved.workspace_dir.value == "[local-path]"`, `resolved.object_store_root` is a mapping with `present`, `source`, `value == "[local-path]"` and `same_as_workspace`, and the persisted submission event still carries the real values

#### Scenario: Rejected URI candidates are placeholders on the wire

- **WHEN** a database-lane manual retry rejects a URL-valued candidate and the attempt ends in `submission_failed`
- **THEN** the 503 body's `rejected[].value` for that candidate is `[uri]` while the persisted event keeps the credential-stripped URL

#### Scenario: File lane keeps provenance for every resolved root

- **WHEN** a file-journal manual retry resolves `workspace_dir` and `object_store_root` and the submission is recorded
- **THEN** the persisted `runtime_root_resolution.resolved.object_store_root` is a mapping with `present is True`, a non-empty `source` and `value == "[local-path]"`, `resolved.workspace_dir` is unchanged in shape and value, and `runtime_root_contract.object_store_root` is still the scalar `[local-path]`

#### Scenario: File lane renders a URI-shaped root as a placeholder, not null

- **WHEN** a file-journal manual retry resolves a URI-shaped root such as `s3://` or `https://` and the submission fails
- **THEN** the persisted event and the 503 body carry `[object-uri]`/`[uri]` for that entry with `present is True`, never `null`; `rejected[].value` carries its placeholder rather than `null`; every `db_free_runtime.resolved.*.value` survives the render unchanged and is never `null`; and the raw URI text appears nowhere in the public event or the response

#### Scenario: Both lanes satisfy the same shape assertions

- **WHEN** the same shape assertions are applied to the database lane's and the file-journal lane's 503 `runtime_root_resolution`, including the URI-shaped root case
- **THEN** both pass with no lane-specific allowance for a withheld `null`, and the file-journal response still equals its persisted event mapping

#### Scenario: Caller-supplied placeholders are still not laundered

- **WHEN** a caller round-trips a public row carrying `[object-uri]`/`[uri]` into a durable journal write on a non-event record
- **THEN** that value SHALL still be persisted as `null`, exactly as before

#### Scenario: Historical bare-string root entries pass through

- **WHEN** a file-journal submission event recorded before this change carries `resolved.object_store_root` as the bare string `[local-path]`
- **THEN** the route returns that entry as recorded and the 503 stays intact
