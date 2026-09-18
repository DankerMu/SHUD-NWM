# opt-in-live-proof-lane Specification

## Purpose
TBD - created by archiving change m19-production-readiness-proof. Update Purpose after archive.
## Requirements
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

### Requirement: The gateway live-proof emitter SHALL authenticate its mutations and SHALL NOT leak the token

The M24 gateway live-proof emitter SHALL carry the shared service bearer on its mutations — job submission and
cancellation — obtained from the canonical reader, which is the only definition of the token's environment variable, minimum length, and fail-closed
whitespace/non-ASCII rules. Health checks and polling GETs SHALL remain anonymous, because the gateway's
read surface is anonymous by contract and sending a credential there would assert an access requirement that
does not exist.

The token SHALL enter the process only from that shared reader over an owner-readable environment source.
It SHALL NOT appear in the process argument vector, in the CLI's help output, in the emitted receipt's recorded
command, in receipt notes, in logs, or anywhere in the emitted evidence JSON.

When no usable token is configured, or when a mutation is refused for missing credentials, the emitter SHALL
record a blocked status with a non-empty blocker and SHALL NOT report the live proof as accepted. Reporting a
pass that the run did not observe is the failure this requirement exists to prevent.

#### Scenario: Mutations carry the bearer and reads do not

- **GIVEN** a usable service token in the environment
- **WHEN** the emitter submits a job and cancels it, and separately checks health and polls status
- **THEN** each mutation request carries an `Authorization: Bearer` header and each read request carries none

#### Scenario: A missing token blocks instead of passing

- **GIVEN** an environment in which the shared reader returns no usable token
- **WHEN** the emitter runs
- **THEN** the receipt records a blocked status with a non-empty blocker and does not mark the live proof accepted

#### Scenario: The serialized receipt never contains the token

- **GIVEN** a run configured with a known token value
- **WHEN** the emitted receipt is serialized in full
- **THEN** that value does not occur anywhere in the serialized text, including the recorded command and notes

