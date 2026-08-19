# retry-stage-evidence-supersession Spec Delta

## MODIFIED Requirements

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

#### Scenario: Every repaired failure keeps its annotation across multiple repairs

- **WHEN** the same candidate has two or more distinct-stage failures each
  repaired by its own later successful manual retry
- **THEN** every repaired failed job MUST carry repaired annotations
  (`repair_status`, `repaired_by_job_id`, non-blocking `active_blocker`) — a
  newer repair MUST NOT evict an older repair's annotations
- **AND** the single `repaired_stage_evidence` selection MUST still name the
  newest repair pair and its restart semantics MUST NOT regress.

## ADDED Requirements

### Requirement: Repaired evidence composes with completed-stage projection

Candidate state projection SHALL treat repaired-stage evidence and completed-stage evidence as orthogonal facts: repaired evidence that carries no restart stage MUST NOT suppress the candidate's own completed-stage projection.

#### Scenario: Source-cycle repair coexists with the candidate's completed stages

- **WHEN** a cycle-scope `download_source_cycle` failure is repaired by a
  successful manual retry and the candidate itself has a completed forecast
  stage
- **THEN** candidate state MUST expose both `repaired_stage_evidence` and
  `completed_stage_evidence` with the restart stage derived from the
  candidate's own completed stages
- **AND** downstream failed-stage and manual-retry attempt derivation MUST
  match a control candidate that has no source-cycle repair rows.

#### Scenario: Repaired evidence carrying a restart stage keeps its projection

- **WHEN** repaired-stage evidence itself carries a non-empty restart stage
- **THEN** the existing projection MUST be preserved unchanged: the repaired
  evidence supplies `completed_stage_evidence` and the restart keys, and the
  stale failure fields (`pipeline_status`, `stage`, `failed_stage`,
  `error_code`, `error_message`) are cleared, with no completed-stage scan
  overriding it.

#### Scenario: Terminal completion still suppresses the completed-stage scan

- **WHEN** repaired-stage evidence carries no restart stage (or no repaired
  evidence exists) and the candidate's cycle-wide job base already contains a
  terminal completion-stage success
- **THEN** the completed-stage scan MUST remain suppressed and no restart
  marker is re-armed by that scan.
