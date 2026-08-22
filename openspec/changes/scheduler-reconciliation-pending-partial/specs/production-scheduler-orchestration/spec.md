## ADDED Requirements

### Requirement: Reconciliation-pending candidates are partial non-success evidence

Scheduler candidate evidence SHALL treat cycle terminal `reconciling` and stage/candidate statuses `submit_result_ambiguous` and `reconcile_unverified` as incomplete reconciliation outcomes. Such evidence SHALL be partial and non-successful, but SHALL NOT manufacture a failed candidate.

#### Scenario: Reconciling cycle candidate cannot report final success

- **WHEN** a cycle-derived candidate remains active while its cycle terminal is `reconciling`
- **THEN** the candidate status SHALL be `reconciling`
- **THEN** `final_candidate_success` SHALL be false
- **THEN** the candidate SHALL contribute to producer `partial_count`
- **THEN** it SHALL NOT contribute to `failed_count`

#### Scenario: Stage reconciliation statuses share the non-success classifier

- **WHEN** candidate evidence carries `submit_result_ambiguous` or `reconcile_unverified`
- **THEN** the same non-success predicate SHALL reject final success
- **THEN** existing failed-status classification SHALL remain false for both statuses

#### Scenario: Confirmed first dispatch survives same-cycle pending projection

- **GIVEN** a scheduler candidate's initial full-array stage has a confirmed Slurm master job identity
- **WHEN** either a nested partial retry or an outer whole-array retry ends reconciliation-pending and the pass artifact is produced
- **THEN** the candidate model-run evidence SHALL retain `submitted=true` and `slurm_submit_called=true`
- **THEN** execution proof SHALL retain a positive `submitted_count` and `slurm_submit_count`
- **THEN** `slurm_submit_proven_absent` SHALL be false and no-mutation proof SHALL NOT claim `slurm_submit_called=false`
- **THEN** evidence compaction SHALL preserve those facts

#### Scenario: Multi-hop retry history preserves confirmed submission proof

- **GIVEN** a scheduler candidate's current stage confirmed a Slurm master before one or more empty-ID same-stage retry results
- **WHEN** the final retry ends reconciliation-pending without its own Slurm identity and the pass artifact is produced
- **THEN** model-run and execution proof SHALL retain the earlier confirmed submission facts
- **THEN** persisted and bounded evidence SHALL keep a positive submit count and `slurm_submit_proven_absent=false`
- **THEN** raw retry metadata and durable rows SHALL remain attributed to their original attempts

#### Scenario: Pending status without confirmed identity remains non-submitted

- **WHEN** a scheduler candidate has a reconciliation-pending status but the current stage loop has never observed a confirmed Slurm identity
- **THEN** model-run evidence SHALL keep `submitted=false`
- **THEN** no producer SHALL turn the pending token itself into a positive submission proof
