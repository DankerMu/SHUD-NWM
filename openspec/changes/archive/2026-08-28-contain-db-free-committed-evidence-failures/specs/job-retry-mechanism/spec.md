## ADDED Requirements

### Requirement: Db-free committed scheduler decisions survive derived evidence failures

The file-journal scheduler SHALL treat the validated cycle-journal append as the commit point for plain reservation and reclaim writes. A failure before that append SHALL fail closed without a durable reservation transition. After the append succeeds, a direct-job, reconcile-inventory, or latest projection failure SHALL NOT escape as an uncommitted write failure, reverse submit ownership, or make a later pass submit the same attempt. The scheduler SHALL return/replay the committed row, attempt remaining independent projections, and emit bounded non-secret fault evidence that identifies the failed projection without carrying exception text, class, path, arbitrary error/reason values, or secret-derived data. Failure to emit secondary fault evidence SHALL NOT reverse the committed result.

A duplicate-submission skip established by the reservation gate SHALL likewise remain `skipped_duplicate_submission` when its optional pipeline-event emission raises either `OrchestratorError` or `FileOrchestrationJournalError`; expected evidence failure SHALL NOT invoke sbatch. Other exception families and non-reservation writer contracts SHALL remain unchanged.

#### Scenario: Automatic absence reclaim survives a post-append projection fault

- **WHEN** an `absence_retry_permitted` current master is reclaimed through the real automatic forecast-cycle path and one direct-job or reconcile-inventory projection fails after the new attempt's authority append
- **THEN** the committed attempt remains replayable from a fresh repository and the winning pass does not surface `FILE_JOURNAL_WRITE_FAILED`
- **AND** at most one Slurm submission occurs for that attempt, a subsequent public cycle submits none, and neither cycle reports `PIPELINE_ALREADY_ACTIVE` because of the fault
- **AND** one bounded non-secret fault signal identifies the failed projection without exposing the raw exception.

#### Scenario: Plain reservation distinguishes pre-commit from post-commit failure

- **WHEN** a clean plain reservation's authority append succeeds and a derived projection then fails
- **THEN** the caller receives the committed reservation and fresh replay observes the same reservation
- **AND WHEN** the authority append itself fails before commit
- **THEN** the write raises its existing typed error and authority bytes remain unchanged.

#### Scenario: Duplicate skip survives either expected evidence exception

- **WHEN** the reservation gate proves another pass owns an active candidate and duplicate-skip event emission raises `OrchestratorError` or `FileOrchestrationJournalError`
- **THEN** the stage still returns `skipped_duplicate_submission`, preserves its bounded in-memory skip evidence, and performs zero Slurm submissions.

#### Scenario: Normal and sibling reservation contracts remain compatible

- **WHEN** plain reserve, automatic reclaim, operator old-ID reclaim, bind, and duplicate-skip evidence complete without an injected fault
- **THEN** their existing identities, attempt/anchor derivation, statuses, event output, and submit counts remain unchanged
- **AND** PostgreSQL repository behavior and non-reservation file-journal writer failure semantics remain outside this containment change.
