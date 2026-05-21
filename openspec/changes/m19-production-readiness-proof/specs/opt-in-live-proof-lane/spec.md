## ADDED Requirements

### Requirement: Opt-in Live Proof Lane
The readiness framework SHALL support live proof ingestion/execution only when explicitly configured.

#### Scenario: Default fast CI has no live side effects
WHEN readiness runs without explicit live proof flags or live receipt configuration
THEN it does not execute live IdP, alert sink, backend mutation, rollback, Slurm, object-store, weather/source, or real-national-data operations and records those live surfaces as not_executed or release_blocked evidence.

#### Scenario: Live auth proof configured
WHEN live auth configuration is provided
THEN the lane records provider metadata, role mapping, protected action checks, and redacted evidence.

#### Scenario: Live alert sink configured
WHEN an alert sink is configured
THEN the lane records delivery result or stable failure without leaking sink credentials.

#### Scenario: Live rollback drill configured
WHEN live rollback execution is explicitly requested
THEN the lane records preconditions, command, result, residual risk, and artifact references.

#### Scenario: Live proof redaction
WHEN live proof includes tokens, credentials, URLs with userinfo/query strings, provider metadata, alert sink metadata, dependency receipts, or artifact paths
THEN evidence artifacts and stdout redact sensitive values while retaining non-sensitive status, subject, blocker, and artifact-reference fields.

#### Scenario: Oversized or malformed live proof
WHEN live proof payloads are oversized, too deeply nested, cyclic, malformed, or missing required fields
THEN the lane records stable blocked or release_blocked evidence with bounded redacted payload context and does not claim live proof acceptance.
