# archive-rebuild-drill — delta for retention-drill-salvage-window-scope

## ADDED Requirements

### Requirement: db-export drill coverage is salvage-window-scoped

When the completeness receipt reports any `coverage = db-export` subject overlapping the candidate drop window, the retention gate SHALL require drill `db-export` coverage only over the salvage-backed windows — the windows of `coverage = db-export` subjects with `verdict = complete` that overlap the drop window, each intersected with the drop window — evaluating the drill's `db-export` tuple union against each such window independently. The gate SHALL NOT treat an empty derivation as satisfying the requirement. The gate SHALL NOT require `db-export` coverage over portions of the drop window backed by product archives, and the `forcing` and `runs` coverage legs SHALL retain their whole-drop-window union semantics unchanged.

#### Scenario: Drop window spanning the salvage-era boundary is admissible

- **WHEN** the drop window mixes a salvage-backed sub-window (complete `db-export` subject) with product-archive-backed time, the drill's `db-export` tuple union covers that sub-window intersected with the drop window, and the `forcing` and `runs` unions cover the whole drop window
- **THEN** the drill gate passes without `DRILL_COVERAGE_DB_EXPORT_MISSING`

#### Scenario: Coverage gap inside a salvage-backed window still refuses

- **WHEN** the drill's `db-export` tuple union leaves a gap inside any salvage-backed window (intersected with the drop window)
- **THEN** the gate refuses with `DRILL_COVERAGE_DB_EXPORT_MISSING`

#### Scenario: Salvage-backed window derivation yields nothing (defence in depth)

- **WHEN** a `coverage = db-export` subject overlaps the drop window but no salvage-backed window can be derived from it (a shape the completeness receipt schema and the completeness gate both already reject upstream)
- **THEN** the drill gate SHALL treat the db-export requirement as unsatisfied and refuse with `DRILL_COVERAGE_DB_EXPORT_MISSING`, never as satisfied
