## ADDED Requirements

### Requirement: Operators can atomically bind a manually verified held reservation

The file-journal scheduler SHALL expose a row-scoped operator CLI, `bind-reserved-job`, that binds one held current-contract accepted-submit forecast cohort master to an operator-verified Slurm master. A held master is one that is `reserved`, has no bound or matched Slurm id, and has `submit_outcome` `submit_result_ambiguous`. The command SHALL require all of:

- explicit confirmation;
- the Slurm master id, which SHALL be a bare numeric id;
- the sacct `Submit` time, which SHALL fall within `[attempt anchor, check time]`;
- the sacct `SubmitLine` excerpt;
- the exact persisted submission attempt and attempt anchor;
- operator identity;
- a timezone-aware check time;
- a bounded, non-empty verification note.

The `SubmitLine` SHALL carry exactly one distinct `--comment=` value, and that value SHALL equal the row's own `nhms_idem:<idempotency_key>`. The Slurm id SHALL NOT be bound to or claimed by any other row.

Under the cycle lock the command SHALL write the same durable bind tuple as the automatic name-window fallback bind (`matched_bound`, `submit_outcome=accepted`, `status=submitted`, `reconciliation_source=slurm_name_window_unique`, the matched id, and the accounting submit time), and SHALL append one audit event carrying the redacted operator evidence in the same durable append. It SHALL introduce no new durable binding token.

Every refusal SHALL be named and SHALL leave the journal byte-identical. The named refusals include a legacy unversioned master, a stale attempt or anchor, a row that is not held (the held tuple is exactly `slurm_exact_comment` / `accounting_unavailable` / `comment_accounting_unproven`), a submit time outside the attempt window, a key mismatch, a claimed Slurm id (including a recycled id bound elsewhere), and a malformed Slurm id. The command SHALL behave identically through the click and argparse entrypoints. Terminal projection of the bound master SHALL remain the job of the next pass's inflight reconcile.

#### Scenario: A verified held master is bound and the lane resumes
- **WHEN** a held forecast master whose automatic reconcile reports `ambiguous_fallback_match` or a persistent `query_unavailable` is bound with a SubmitLine carrying its own key, a matching attempt and anchor, and full operator evidence
- **THEN** the master SHALL carry the automatic fallback bind tuple and one operator audit event
- **AND** after inflight reconcile projects the completed array, the members SHALL be terminal and the cycle completion verdict SHALL no longer be `gap`

#### Scenario: A key mismatch or stale expectation writes nothing
- **WHEN** the SubmitLine key differs from the row's key, is absent, or is ambiguous, or the attempt, anchor, status, binding, or outcome differs from the held expectation
- **THEN** the command SHALL exit non-zero with the named refusal and the journal SHALL be byte-identical

#### Scenario: A claimed Slurm id cannot be bound twice
- **WHEN** the supplied Slurm id is already bound to or claimed by another row
- **THEN** the command SHALL refuse with `slurm_id_claimed` and write nothing

#### Scenario: A prior attempt's master cannot be bound
- **WHEN** the supplied Slurm submit time is earlier than the row's attempt anchor or later than the check time
- **THEN** the command SHALL refuse with `submit_time_outside_attempt_window` and write nothing

#### Scenario: Legacy unversioned masters are refused by name
- **WHEN** the job id names an unversioned forecast cohort master
- **THEN** the command SHALL refuse with `legacy_unversioned_unsupported` and write nothing

### Requirement: Unresolved held reservations SHALL be enumerable from db-free pass evidence

`list-operator-actions` SHALL also list a held reservation that restart reconcile could not resolve, as decision `held_reservation_unresolved`. It SHALL read the entries from `restart_reconcile.reserved_unbound.outcomes[]` of the scanned terminal passes. It SHALL always list an outcome whose action is `ambiguous_fallback_match` or `legacy_unversioned_read_only`. It SHALL always list `multiple_matches_blocked`. It SHALL never list `bound`, `reservation_lost`, `absence_retry_permitted`, or `identity_mismatch_released`. It SHALL list every other reserved-unbound action when the outcome's `submission_attempt_started_at` (which pass evidence SHALL carry and retain under compaction) is at least six hours older than that pass's start, or is unknown. Entries SHALL be deduplicated by job id. Passes whose restart-reconcile lane was skipped, errored, or left no outcomes SHALL be reported as `restart_reconcile_unscanned_passes`; exit `0` does not vouch for held rows present only in those passes. Each entry SHALL carry the job id, source, cycle time, reconcile action, attempt anchor, first/last seen pass, and an `operator_command` from the closed set {`bind-reserved-job`, `triage`, `escalate`}, mapped per action as follows:

- `bind-reserved-job` for `ambiguous_fallback_match`;
- `triage` for `query_unavailable`, `fallback_no_match`, and `absence_unconfirmed`;
- `escalate` for `legacy_unversioned_read_only`, `multiple_matches_blocked`, `identity_mismatch_blocked`, `stale_attempt_blocked`, `journal_quarantined`, and any action outside this mapping (which SHALL still be listed).

An entry whose job id does not name a forecast cohort master stage (a forcing-lane row, or an id the stage-suffix rule cannot read) SHALL use `escalate` regardless of its action and SHALL carry `follow_up_issue` `#2675`; a legacy unversioned entry SHALL carry `follow_up_issue` `#2674`.

An entry SHALL be dropped when a newer scanned pass whose restart-reconcile lane ran no longer reports that job id, or reports it with a never-listed action. A listed entry SHALL make the command exit `1`, like every other listed action.

#### Scenario: An ambiguous held master is listed immediately
- **WHEN** the newest scanned pass carries a reserved-unbound outcome with action `ambiguous_fallback_match`
- **THEN** the command SHALL list it as `held_reservation_unresolved` with `operator_command` `bind-reserved-job` and exit `1`

#### Scenario: A resolved held master stops being listed
- **WHEN** a job was listed from an older pass and a newer reconciling pass no longer reports it (it was bound, demoted, or released)
- **THEN** the command SHALL NOT list it

#### Scenario: A held forcing row is escalated, never bound
- **WHEN** a scanned pass carries a forcing-lane reserved-unbound outcome with action `multiple_matches_blocked`, or with `query_unavailable` whose anchor is six hours old or more
- **THEN** the command SHALL list it with `operator_command` `escalate` and `follow_up_issue` `#2675`

#### Scenario: A transient held master is not noise
- **WHEN** a reserved-unbound outcome reports `query_unavailable` and its attempt anchor is less than six hours old
- **THEN** the command SHALL NOT list it
- **WHEN** the same outcome persists with an anchor six hours old or more
- **THEN** the command SHALL list it
