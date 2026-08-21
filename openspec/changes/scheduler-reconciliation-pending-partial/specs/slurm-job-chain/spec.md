## ADDED Requirements

### Requirement: Reconciliation-pending nested submissions defer without manufacturing failure

The partial-array retry helper SHALL preserve a nested `submit_result_ambiguous` or `reconcile_unverified` result as a reconciliation deferral. It SHALL map either stage terminal to cycle terminal `reconciling`, while preserving the distinct duplicate-submission skip terminal.

#### Scenario: Nested ambiguous submission stops on reconciling

- **WHEN** a nested partial-array resubmission returns `submit_result_ambiguous` without an aggregation
- **THEN** the pending tasks SHALL NOT be rewritten as failed
- **THEN** the cycle SHALL terminate as `reconciling` with the raw ambiguous stage result
- **THEN** no downstream stage SHALL run and no further retry attempt SHALL be derived

#### Scenario: Nested unverified reconciliation preserves durable no-op

- **WHEN** a nested partial-array resubmission returns `reconcile_unverified` without an aggregation
- **THEN** the pending tasks SHALL NOT be rewritten as failed
- **THEN** the cycle SHALL terminate as `reconciling`
- **THEN** the existing reconciliation event or row MAY remain, but the executor SHALL NOT add a second partial or failed cycle-status write
- **THEN** no downstream stage or further retry attempt SHALL run

#### Scenario: Nested pending replacement preserves confirmed dispatch identity

- **GIVEN** the prior partial stage contains a non-empty Slurm master job identity proving that the full array was dispatched
- **WHEN** a nested resubmission returns a raw reconciliation-pending result with no Slurm identity or task outcomes
- **THEN** the returned pending stage SHALL retain the prior non-empty Slurm master job identity
- **THEN** it SHALL retain the raw pending terminal and empty task outcomes
- **THEN** it SHALL NOT reconstruct stale per-task outcomes or infer any additional submission

#### Scenario: Outer retry pending replacement preserves confirmed dispatch identity

- **GIVEN** the current cycle's prior whole-array stage contains a non-empty Slurm master job identity
- **WHEN** a same-stage outer retry returns a raw reconciliation-pending result with no Slurm identity or task outcomes
- **THEN** the replacement stage SHALL retain the prior non-empty Slurm master job identity
- **THEN** it SHALL retain the raw retry pipeline identity, pending terminal, error fields, and empty task outcomes
- **THEN** a non-empty raw retry master identity SHALL remain authoritative instead of being overwritten
- **THEN** no further retry or downstream stage SHALL be derived from the pending result

#### Scenario: Bare pending result does not manufacture dispatch identity

- **GIVEN** no prior stage contains confirmed Slurm submission identity
- **WHEN** a reconciliation-pending stage result is returned
- **THEN** the result SHALL remain without Slurm submission identity
- **THEN** the pending status alone SHALL NOT prove that Slurm submission occurred

#### Scenario: Reconciliation defer timing is neither submitted nor failed

- **WHEN** either governed nested reconciliation-pending terminal closes a stage span entered with `N` basins
- **THEN** the final span SHALL report `basin_count=N`
- **THEN** it SHALL report `submitted_count=0` and `failed_count=0`
- **THEN** ordinary failed or `submission_failed` terminals SHALL retain their existing failure attribution

#### Scenario: Unrelated nested terminals retain existing behavior

- **WHEN** a nested resubmission returns `skipped_duplicate_submission`, `submission_failed`, success, or an ordinary failed aggregation
- **THEN** duplicate skip SHALL retain its dedicated skip terminal
- **THEN** `submission_failed` and ordinary success/failure retry semantics SHALL remain unchanged
