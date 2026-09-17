## ADDED Requirements

### Requirement: Scheduler repair recovery proof binds compute and published progression

An explicitly authorized scheduler recovery SHALL deploy only reviewed changes without violating the pinned active environment, DB-free compute boundary, journal safety or in-flight worker code identity. Recovery SHALL be proven by actual stage completion and downstream published identities, not helper tests or submission alone.

#### Scenario: Controlled pinned-runtime recovery

- **WHEN** the reviewed phase-boundary fix is deployed to a quiescent node-22 runtime
- **THEN** deployed SHA and reviewed-patch equivalence are recorded, existing interpreter/environment and safety guards are preserved, and normal scheduler execution produces real forcing, successful forecast, successor state and copied outputs
- **AND** no tracker reset, forged witness, DB connection from node-22 or forced stage marker is used

#### Scenario: Dual-source downstream advancement

- **WHEN** GFS recovery releases the prior-cycle frontier and IFS work advances normally
- **THEN** node-27 ingest/publication and public read APIs identify a later GFS cycle than 2026-09-14T00:00:00Z and a later IFS cycle than 2026-09-14T12:00:00Z with matching run/model/source identities and readable result data
- **AND** any remaining backlog or independent failure is recorded separately instead of being hidden by a successful submit

#### Scenario: Unsafe deployment or independent failure

- **WHEN** unreviewed source drift, active code readers, missing active interpreter, identity mismatch or a later-stage independent failure prevents safe progression
- **THEN** the operation does not bypass the failing guard and records exact blocker evidence; recovery is not claimed complete
