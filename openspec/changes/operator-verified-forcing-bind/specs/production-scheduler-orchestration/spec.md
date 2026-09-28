## ADDED Requirements

### Requirement: Operators can bind a manually verified held forcing master

`bind-reserved-job` SHALL also accept a held forcing master. A held forcing master is a row whose forcing submit identity is complete, that is `reserved`, has no bound or matched Slurm id, has `submit_outcome` `submit_result_ambiguous`, and has no reconciliation decision. For such a row the command SHALL require all of the following, and SHALL otherwise refuse by name with the journal byte-identical:

- the operator-supplied sacct `SubmitLine` carries exactly one distinct `--comment=` value, equal to the row's own forcing attempt comment;
- the `SubmitLine` carries exactly one `--array=` value of the form `0-<n-1>` (optionally `%<k>`), where `n` is the row's cohort member count (refusal `array_spec_mismatch`);
- when the row requires Slurm ownership, the operator-supplied Slurm user and account equal the row's expected owner (refusal `slurm_owner_mismatch`);
- the supplied Slurm submit time falls within `[attempt anchor, check time]`;
- the expected attempt and anchor equal the durable values;
- the Slurm id is canonical and unclaimed under the same bounded claimant exclusivity as the forecast bind.

Under the cycle lock the command SHALL write exactly the automatic forcing bind tuple (`matched_bound`, `submit_outcome=accepted`, `status=submitted`, `reconciliation_source=slurm_exact_comment`, the id as both bound and matched id) plus one `operator_verified_bind` audit event in the same durable append, introducing no new durable token. Forecast rows SHALL keep the existing behavior.

#### Scenario: A verified held forcing master is bound and its members are released
- **WHEN** a held forcing master is bound with a `SubmitLine` carrying its own attempt comment and `--array=0-<n-1>%<k>` for its `n` members, a window-valid submit time, and a canonical unclaimed id
- **THEN** it SHALL carry the automatic forcing bind tuple and one operator audit event
- **AND** after inflight reconcile projects the completed array, its member candidates SHALL no longer skip as `active_duplicate_pipeline`

#### Scenario: Evidence from another attempt or another array size is refused
- **WHEN** the `SubmitLine` comment names another attempt or key, or its array size differs from the member count
- **THEN** the command SHALL refuse with `submitline_key_mismatch` or `array_spec_mismatch` and write zero bytes

## MODIFIED Requirements

### Requirement: Unresolved held reservations SHALL be enumerable from db-free pass evidence

`list-operator-actions` SHALL also list a held reservation that restart reconcile could not resolve, as decision `held_reservation_unresolved`. It SHALL read the entries from `restart_reconcile.reserved_unbound.outcomes[]` of the scanned terminal passes. It SHALL always list an outcome whose action is `ambiguous_fallback_match` or `legacy_unversioned_read_only`. It SHALL always list `multiple_matches_blocked`. It SHALL never list `bound`, `reservation_lost`, `absence_retry_permitted`, or `identity_mismatch_released`. It SHALL list every other reserved-unbound action when the outcome's `submission_attempt_started_at` (which pass evidence SHALL carry and retain under compaction) is at least six hours older than that pass's start, or is unknown. Entries SHALL be deduplicated by job id. Passes whose restart-reconcile lane was skipped, errored, or left no outcomes SHALL be reported as `restart_reconcile_unscanned_passes`; exit `0` does not vouch for held rows present only in those passes. Each entry SHALL carry the job id, source, cycle time, reconcile action, attempt anchor, first/last seen pass, and an `operator_command` from the closed set {`bind-reserved-job`, `triage`, `escalate`}, mapped per action as follows:

- `bind-reserved-job` for `ambiguous_fallback_match`;
- `triage` for `query_unavailable`, `fallback_no_match`, and `absence_unconfirmed`;
- `escalate` for `legacy_unversioned_read_only`, `multiple_matches_blocked`, `identity_mismatch_blocked`, `stale_attempt_blocked`, `journal_quarantined`, and any action outside this mapping (which SHALL always be listed, regardless of its attempt anchor age).

An entry whose job id names a forcing master stage SHALL instead use `bind-reserved-job` for `multiple_matches_blocked` and `query_unavailable`, and `escalate` for every other action, and SHALL carry no `follow_up_issue`. An entry whose job id names neither a forecast cohort master nor a forcing master stage (or an id the stage-suffix rule cannot read) SHALL use `escalate` regardless of its action. A legacy unversioned entry SHALL carry `follow_up_issue` `#2674`.

An entry SHALL be dropped when a newer scanned pass whose restart-reconcile lane ran no longer reports that job id, or reports it with a never-listed action. A listed entry SHALL make the command exit `1`, like every other listed action.

#### Scenario: An ambiguous held master is listed immediately
- **WHEN** the newest scanned pass carries a reserved-unbound outcome with action `ambiguous_fallback_match`
- **THEN** the command SHALL list it as `held_reservation_unresolved` with `operator_command` `bind-reserved-job` and exit `1`

#### Scenario: A resolved held master stops being listed
- **WHEN** a job was listed from an older pass and a newer reconciling pass no longer reports it (it was bound, demoted, or released)
- **THEN** the command SHALL NOT list it

#### Scenario: A held forcing row points to the forcing bind
- **WHEN** a scanned pass carries a forcing-lane reserved-unbound outcome with action `multiple_matches_blocked`, or with `query_unavailable` whose anchor is six hours old or more
- **THEN** the command SHALL list it with `operator_command` `bind-reserved-job` and no `follow_up_issue`

#### Scenario: A held forcing row is escalated, never bound
- **WHEN** a scanned pass carries a forcing-lane reserved-unbound outcome with action `identity_mismatch_blocked` (or any other action outside the forcing bind mapping) whose anchor is six hours old or more
- **THEN** the command SHALL list it with `operator_command` `escalate` and no `follow_up_issue`

#### Scenario: A transient held master is not noise
- **WHEN** a reserved-unbound outcome reports `query_unavailable` and its attempt anchor is less than six hours old
- **THEN** the command SHALL NOT list it
- **WHEN** the same outcome persists with an anchor six hours old or more
- **THEN** the command SHALL list it
