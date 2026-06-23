## ADDED Requirements

### Requirement: Production-closure validation SHALL have lane ownership

Production-closure evidence and readiness validation SHALL be organized by
lane ownership before large aggregator files are reduced.

#### Scenario: two-node E2E evidence lanes are mapped

- **WHEN** `services/production_closure/two_node_e2e_evidence.py` validates
  Docker security, readonly DB, API/browser proof, logs, producer identity,
  source artifacts, or manual ops receipts
- **THEN** each lane SHALL have an owner module plan, input/output contract,
  blocker/finding code namespace, and focused verification command.

#### Scenario: readiness validation lanes are mapped

- **WHEN** `services/production_closure/readiness_validation.py` validates
  dependency summaries, scheduler evidence, live proof, exclusions, or final
  readiness
- **THEN** each lane SHALL have an owner module plan, input/output contract,
  blocker/finding code namespace, and focused verification command.

### Requirement: Aggregators SHALL compose structured lane results

Production-closure aggregators SHALL converge toward stable orchestration
entrypoints that compose structured lane results instead of owning all parsing
rules.

#### Scenario: lane is extracted

- **WHEN** a production-closure lane moves to a dedicated module
- **THEN** the existing public entrypoint SHALL keep its CLI/API contract
- **AND** the extracted lane SHALL return a structured result with status,
  blockers, findings, evidence summary, and redacted public error text where
  applicable.

#### Scenario: alias matrix is retained

- **WHEN** legacy alias groups are still needed for a lane
- **THEN** the alias definitions SHALL live with that lane or a named shared
  contract, not as unowned aggregator-local rules.

### Requirement: Lane extraction SHALL be evidence-preserving

Reducing production-closure file size MUST NOT weaken path safety, secret
redaction, current-run identity, or readonly boundary evidence.

#### Scenario: extracted lane validates path or secret-sensitive evidence

- **WHEN** a lane extraction touches path handling, artifact URIs, redaction, or
  producer/current-run identity
- **THEN** focused tests SHALL cover traversal/symlink rejection, credential-safe
  output, stale/current-run mismatch, and existing blocker code preservation.

#### Scenario: final aggregation runs after extraction

- **WHEN** the full production-closure command runs after lane extraction
- **THEN** final readiness status SHALL match the pre-extraction behavior for
  equivalent fixture inputs.
