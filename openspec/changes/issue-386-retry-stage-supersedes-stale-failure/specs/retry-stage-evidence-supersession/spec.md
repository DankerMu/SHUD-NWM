## ADDED Requirements

### Requirement: Successful linked retry repairs stale stage failure evidence

Scheduler readiness evidence SHALL treat a successful linked manual retry for a
logical cycle stage as repairing older failed evidence for that same stage.

#### Scenario: Shared source-cycle download retry repaired the stage

- **WHEN** a `download_source_cycle` stage for source/cycle `S/T` has an older
  terminal failure
- **AND** a later manual retry linked to that failed stage has status
  `succeeded`
- **AND** the matching forecast cycle is verified ready for that stage, such as
  `raw_complete` with a manifest URI for source downloads
- **THEN** scheduler candidate readiness MUST NOT keep the older failed stage as
  the active blocker
- **AND** evidence MUST identify the original failure as repaired or superseded
  by the retry job.

#### Scenario: Unrelated success does not hide unrepaired failure

- **WHEN** a failed stage has no successful retry linked by retry provenance and
  logical stage identity
- **THEN** the failure MUST remain active blocking evidence
- **AND** the candidate failure error code/message MUST remain stable.

### Requirement: Repaired failure evidence remains auditable and bounded

The scheduler evidence contract SHALL preserve enough audit history to explain
why a stale failure no longer blocks readiness.

#### Scenario: Repaired failure audit trail

- **WHEN** an older failure is superseded by a retry repair
- **THEN** `stage_statuses` or related evidence MUST expose the original failed
  job id, the successful retry job id, the repaired stage, and the repair status
  without presenting the original failure as an active blocker.

#### Scenario: Bounded evidence reads

- **WHEN** job or event history exceeds configured evidence limits
- **THEN** evidence MUST remain bounded and indicate truncation rather than
  performing unbounded reads.

### Requirement: Existing retry compatibility is preserved

The new repaired-stage semantics SHALL NOT regress existing retry and candidate
state behavior outside the repaired logical stage.

#### Scenario: Partial array retry task supersession remains unchanged

- **WHEN** a partially failed array task is followed by a successful retry task
  for the same original task identity
- **THEN** the latest successful retry task MUST continue to supersede the older
  failed task evidence.

#### Scenario: Existing unrepaired failed candidates still block

- **WHEN** a candidate has only unrepaired failed, `submission_failed`,
  `partially_failed`, or `permanently_failed` evidence
- **THEN** scheduler readiness MUST continue to emit failed/retry evidence as it
  did before this change.
