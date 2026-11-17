## REMOVED Requirements

### Requirement: Manual retry creates executable work

**Reason**: Specifies the retry route success path (submit to Slurm or enqueue, `queued`/`submitted`/`running` response, `slurm_job_id`) and the pending-retry consumer. The route no longer submits (issue #3157); the pending-retry consumer scenario existed only for the retired manual `pending` row (auto-retry placeholders are consumed by the orchestrator, not governed here).

**Migration**: The route fails closed with 409 `CONTROL_PLANE_MANUAL_ACTION_REQUIRED` (ops-monitoring-api, ADDED "POST /api/v1/runs/{run_id}/retry is a fail-closed manual-action endpoint"). Supported manual path: `record_manual_repair` via `scripts/node22_manual_retry_failed_runs.py`.

### Requirement: Retry active guards do not deadlock

**Reason**: The stale-pending guard constrains the pending manual retry row created by `attempt_manual_retry`; that row no longer exists. `expire_stale_retries` code removal is deferred to a later dead-code PR.

**Migration**: None; no manual pending row is created, so nothing can block future retries.

### Requirement: The manual-retry durable-success set is named distinctly from the pipeline durable-success set

**Reason**: Body and scenario 2 assert the refusal on "both the DB-backed path and its file-journal twin" and a DB-lane parametrized refusal test; the DB lane sits in the retired `RetryService.attempt_manual_retry`. The file-journal arm survives through the shared source selector (`_manual_retry_source_for_run`, also called by `record_manual_repair`). Scenario 1 is carried over verbatim; scenario 2 is rewritten for the surviving lane under a new requirement name.

**Migration**: See ADDED "The manual-repair durable-success set is named distinctly from the pipeline durable-success set".

## ADDED Requirements

### Requirement: The manual-repair durable-success set is named distinctly from the pipeline durable-success set

The manual-retry refusal predicate SHALL consume a status set — on the file-journal manual-repair path (the shared manual-retry source selector used by `FileJournalRetryService.record_manual_repair`) — whose name is distinct from
`scheduler_state_types.DURABLE_HYDRO_SUCCESS_STATUSES`, because the two
sets deliberately differ in membership (`"complete"` counts as durable
success for scheduler decisions but does not block a manual retry) and a
shared name invites an accidental merge that would silently change one
predicate's behavior. The membership relationship between the two sets
SHALL be pinned by a test so that any drift on either side — or a rename
back into collision — fails loudly. This change is naming-only: neither
predicate's behavior, membership, exception shape, nor caller surface
changes.

#### Scenario: the membership divergence is explicit and locked

WHEN the manual-retry set and the scheduler durable-success set are
compared
THEN the manual-retry set equals exactly `{"succeeded", "parsed",
"published"}`, the scheduler set equals exactly `{"succeeded", "parsed",
"published", "complete"}` (pinned separately, so collapsing the scheduler
set down to three members — the one merge direction that would change
behavior — also fails), the manual-retry set equals the scheduler set
minus `"complete"`, and a regression test asserts all three relationships

#### Scenario: manual-repair behavior is unchanged

WHEN a run's durable hydro status is one of the three manual-retry
members
THEN the file-journal manual-retry source selection refuses (no retry source; `record_manual_repair` raises `RetryNotFoundError`) exactly as before the rename, pinned by the file-journal refusal-arm test; and `"complete"` — absent from the manual-retry set and not a `hydro.run_status` enum value but representable on the file-journal lane —
continues not to trigger the refusal, asserted both on the file-journal lane and at the constant level
