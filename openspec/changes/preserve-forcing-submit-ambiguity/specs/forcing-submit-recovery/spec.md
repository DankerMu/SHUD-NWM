## ADDED Requirements

### Requirement: Forcing submission ambiguity preserves durable authority
The orchestrator SHALL durably record complete forcing submission identity before a Gateway call and SHALL retain ambiguous acceptance without automatic retry, permanent failure or proven-absence claims.

#### Scenario: Accepted request has an unverifiable response
- **WHEN** a forcing array POST crosses the Gateway boundary and returns HTTP502 SLURM_PARSE_ERROR, transport failure or invalid success identity
- **THEN** the row/event/result retain submit_result_ambiguous, empty Slurm binding, original error and audited identity, and scheduler evidence reports unknown_after_attempt with slurm_submit_proven_absent false
- **AND** no later submission is authorized merely by the missing ID or a process restart

#### Scenario: Proven pre-acceptance rejection
- **WHEN** the Gateway proves policy or validation rejection before acceptance
- **THEN** the existing rejected semantics apply rather than invented ambiguity

### Requirement: Ambiguous forcing fences intersecting cohort identities
The scheduler SHALL block new forcing execution for unresolved intersecting source/cycle/model members independently of stage-derived cohort run keys.

#### Scenario: Convert cohort becomes forcing cohort
- **WHEN** a subsequent pass derives forcing_cohort instead of convert_cohort for the same or overlapping members while prior forcing acceptance is unresolved
- **THEN** it SHALL defer without sbatch and retain the prior authority reference
- **AND** unrelated source/cycle/member work remains eligible

### Requirement: Completed forcing adoption validates identity and real outputs
An operator-only dry-run-first adoption command SHALL require explicit authorization, exact attempt/revision, uniquely provable Slurm and cohort identity, complete successful task accounting and verified actual forcing objects before any authority transition.

#### Scenario: Unsuperseded incident fixture has complete proof
- **WHEN** all expected members and task mappings, source/cycle/stage/job_type/owner/account/comment/attempt, accounting and actual package checks pass for one unsuperseded completed array
- **THEN** authorized apply SHALL atomically bind and project complete forcing evidence with an audit event preserving the original failure
- **AND** normal scheduler continuation SHALL enter forecast without submitting a new forcing array

#### Scenario: Legacy identity cannot be proven
- **WHEN** a legacy failed/null-id record lacks independently auditable identity or provenance
- **THEN** adoption SHALL refuse diagnostically with zero authority writes and zero Slurm mutations

### Requirement: Adoption is race-safe and cannot replace newer authority
Adoption SHALL compare current authority and competing attempts under existing journal locking and SHALL be idempotent for an already committed identical recovery.

#### Scenario: Successful replacement already exists
- **WHEN** a newer accepted/running/completed or published execution supersedes the target, including49174 after49309
- **THEN** adoption SHALL refuse without rewriting existing authority or outputs

#### Scenario: Same recovery repeats or races another writer
- **WHEN** an identical committed adoption repeats
- **THEN** it SHALL return the existing recovery without duplicate business events or execution
- **WHEN** a competing writer changes revision, attempt or member authority before commit
- **THEN** it SHALL refuse without partial authority projection

### Requirement: Invalid evidence is a write-free refusal
The command SHALL reject missing/conflicting/extra/duplicate task identity, incomplete accounting, invalid or changed objects, unsafe paths and exceeded evidence budgets.

#### Scenario: One member is invalid
- **WHEN** one member in an otherwise complete cohort fails identity or object integrity validation
- **THEN** the entire adoption SHALL refuse, preserve all previous journal authority and submit nothing

### Requirement: Forecast and pre-forecast boundary behavior remains unchanged
The change SHALL preserve historical forecast identity/digest validation, forecast reconciliation, strict warm-start and #2439 ordinary pre-forecast resume semantics.

#### Scenario: Existing forecast and ordinary forcing resume
- **WHEN** existing forecast reconciliation fixtures or a new ordinary pre-forecast resume with no prior ambiguous execution are evaluated
- **THEN** their existing valid behavior and strict witness protections SHALL remain unchanged
