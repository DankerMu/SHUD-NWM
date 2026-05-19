## ADDED Requirements

### Requirement: Release Blocker Summary
Readiness reporting SHALL produce a concise blocker summary for release decisions.

Required live surfaces for final readiness are live backend auth, live alert sink delivery, live rollback execution, accepted dependency proofs for Slurm/object-store/source/E2E/MVT where claimed, and real target-environment configuration receipts.

#### Scenario: Blockers present
WHEN any required live proof is missing or failed
THEN the summary lists blocker id, surface, status, residual risk, removal criteria, and linked artifact.

#### Scenario: No blockers
WHEN every required deterministic and live item passes
THEN the summary may mark the corresponding surface ready, but final readiness is claimed only if all required surfaces are ready.

#### Scenario: Exclusion present
WHEN CLDAS or national data completion is excluded by scope
THEN the summary lists it under exclusions rather than blockers.
