## MODIFIED Requirements

### Requirement: Reconciliation-pending candidates are partial non-success evidence

Scheduler candidate evidence SHALL treat cycle terminal `reconciling` and stage/candidate statuses `submit_result_ambiguous` and `reconcile_unverified` as incomplete reconciliation outcomes. Such evidence SHALL be partial and non-successful, but SHALL NOT manufacture a failed candidate, a confirmed submission, or a proven absence of submission.

Submission confirmation and submit-call provenance SHALL remain separate. A confirmed Slurm identity makes `submitted=true` and `slurm_submit_called=true`. When the chain producer has crossed the gateway boundary and durably recorded an accepted-submit ambiguous result but has no confirmed Slurm identity, candidate evidence SHALL keep `submitted=false` while carrying `slurm_submit_called=unknown_after_attempt`; execution/no-mutation proofs and bounded evidence SHALL preserve the same uncertainty and SHALL NOT emit `slurm_submit_proven_absent=true`. A reconciliation status token without producer-owned gateway-attempt provenance SHALL NOT by itself manufacture either positive or unknown submit evidence.

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

#### Scenario: Gateway-crossed bare ambiguity preserves unknown submit-call provenance

- **GIVEN** a candidate's first submission reaches the gateway and the accepted-submit producer durably records `submission_ambiguous`
- **WHEN** the result has no confirmed Slurm identity and the pass artifact is produced
- **THEN** model-run evidence SHALL carry `submitted=false` and `slurm_submit_called=unknown_after_attempt`
- **THEN** execution proof SHALL carry zero confirmed submits, a nonzero unknown-submit count, `slurm_submit_outcome=unknown_after_attempt`, `slurm_submit_proven_absent=false`, and `mutation_outcome=unknown_after_attempt`
- **THEN** no-mutation proof, persisted evidence, and bounded compaction SHALL preserve `slurm_submit_called=unknown_after_attempt`

#### Scenario: Pending status without attempt provenance remains proven no-submit

- **WHEN** a hand-built or replayed scheduler candidate has a reconciliation-pending status but carries neither a confirmed Slurm identity nor producer-owned gateway-attempt provenance
- **THEN** model-run evidence SHALL keep `submitted=false` and `slurm_submit_called=false`
- **THEN** execution proof SHALL keep `slurm_submit_count=0` and `slurm_submit_proven_absent=true`, and no-mutation proof SHALL retain false/proven-absent submit evidence
- **THEN** persisted and bounded evidence SHALL NOT turn the pending token itself into positive or `unknown_after_attempt` submission proof

#### Scenario: True no-submit remains proven absent

- **WHEN** a candidate reaches a failed or blocked result before the gateway submission boundary and carries no confirmed Slurm identity
- **THEN** submit-call evidence SHALL remain false, execution proof SHALL retain `slurm_submit_proven_absent=true`, and bounded evidence SHALL preserve that control
