# Fixture Review: Issue #180 / M18 Model Asset Operations

Fixture review verdict: approve

Blocking fixture gaps:
- None

Recommended clarifications:
- Name the exact lifecycle API route paths or explicitly state that route naming is implementation-defined while the stable contract is the M17 action ids (`models.activate`, `models.deactivate`, `models.switch_version`, `models.rollback_version`, `models.supersede`) plus the service boundary.
- Clarify whether `deprecate` needs a canonical M17 action id before implementation. M18 includes `deprecate` in the transition policy and task list, while M17 lists model action ids through `models.supersede` but not `models.deprecate`.
- Specify the explicit override policy for deactivation that would leave a required operational basin without an active model: allowed role/action id, required reason field, audit decision shape, and whether this override is permitted in deterministic validation fixtures.
- Tighten the audit-write-failure expectation in the invariant matrix. It is listed as a failure path, but the fixture should say whether audit failure blocks/rolls back the lifecycle mutation or records a release-blocked/no-mutation result.

Review notes:
- Fixture level `expanded` and repair intensity `high` are justified by the mutating public API/service surface, RBAC dependency, atomic model state transitions, audit evidence, frontend controls, and production-like validation drill.
- Relevant risk packs are selected and the non-selected packs are explicitly justified. The non-selected geospatial, temporal, numerical, and solver packs are reasonable because M18 validates registry lineage metadata rather than changing CRS parsing, forcing series, hydrologic numerics, or runtime behavior.
- The invariant matrix is concrete enough for implementation/review: it names the governing identity tuple, producers, validators, storage/query surfaces, public entrypoints, downstream consumers, failure paths, evidence outputs, and regression rows for activation, denial, unsafe lineage, deactivation blocking, rollback, idempotency, concurrency, UI authorization, stale frontend state, and redaction.
- Evidence mapping covers the selected risk packs, failure paths, frontend behavior, audit/redaction, deterministic validation, legacy compatibility, and documentation/readiness updates.
- Non-goals are explicit enough to constrain scope around package upload, production deletes/object-store mutation, live enterprise proof, hydrologic skill/calibration changes, and solver behavior.
