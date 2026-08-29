## ADDED Requirements

### Requirement: Projection-only repaired annotations SHALL remain outside durable manual-repair marker authority

The file-journal manual-repair writer SHALL continue to derive marker target details only from durable pipeline-job rows. Projection-only `repair_status` and `active_blocker` annotations SHALL NOT be recomputed in the marker write path, added implicitly to the closed durable pipeline-row schema, or treated as durable facts. Existing marker gate-contract keys SHALL remain readable when a synthetic or legacy marker explicitly carries them, but the production writer SHALL NOT claim to produce those values.

When a target is already annotated repaired only on a projection copy at marker-write time and neither durable `repaired_stage_evidence` nor `completed_stage_evidence` names that target, the existing row-present/row-absent divergence SHALL be an accepted permanent limitation: the row-present path refuses the pin while a later row-absent record path may conservatively pin the marker attempt. This limitation SHALL remain explicit in the retry contract and paired disclosure tests. No retry decision, marker byte shape, projection winner rule, or legacy marker behavior SHALL change as part of this disposition.

#### Scenario: Durable writer does not persist projection annotations

- **WHEN** `record_manual_repair` selects a durable failed row whose repaired status exists only on a separately constructed candidate-state projection copy
- **THEN** the durable marker does not synthesize `target_repair_status` or `target_active_blocker`, the pipeline-row schema is unchanged, and no second repair authority is created

#### Scenario: Explicit gate-contract keys remain readable

- **WHEN** a synthetic or legacy marker explicitly carries a repaired `target_repair_status` or false `target_active_blocker`
- **THEN** the existing row-absent gate continues to honor that record exactly as before

#### Scenario: Projection-only repaired target keeps the disclosed conservative residue

- **WHEN** the target is repaired only in projection at marker-write time, its durable marker lacks the projection-only keys, and no surviving state-level mapping names it after the row disappears
- **THEN** the paired disclosure contract continues to record row-present refusal and row-absent pin as the accepted conservative over-pin limitation, without claiming production write-face convergence

#### Scenario: Legacy retry behavior is unchanged

- **WHEN** existing durable or legacy manual-retry markers are evaluated without any state-index repair operation
- **THEN** their marker bytes, attempt derivation, row-present and row-absent decisions, and sanitizer behavior remain unchanged
