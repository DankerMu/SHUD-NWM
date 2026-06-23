## ADDED Requirements

### Requirement: Archived and superseded material SHALL have machine-readable status

Archived and superseded docs/OpenSpec material SHALL declare status metadata
for audits and agents to distinguish historical evidence from current
instructions.

#### Scenario: archive document is scanned

- **WHEN** a document under an archive or historical path mentions legacy routes
  or retired active-tree paths
- **THEN** the document or section SHALL expose machine-readable status such as
  `status`, `superseded_by`, and `current_authority`, or an equivalent
  standardized marker accepted by the audit.

#### Scenario: superseded active-tree document remains in place

- **WHEN** a non-archive document must preserve superseded guidance for audit
  context
- **THEN** the superseded section SHALL point to the current authority and mark
  the preserved text as non-current.

### Requirement: Archive allowlists SHALL be narrow and explainable

The entropy audit SHALL narrow archive/history allowlists using explicit status
semantics rather than globally ignoring archive paths.

#### Scenario: archive marker is complete

- **WHEN** an archive finding has complete status/current-authority metadata
- **THEN** the audit MAY classify it as allowlisted historical evidence with a
  stable allowlist key and `budget_counted=false`.

#### Scenario: archive marker is missing

- **WHEN** archived material lacks status/current-authority metadata and looks
  like current instructions
- **THEN** the audit SHALL keep the finding visible for triage rather than
  suppressing the whole path family.

#### Scenario: archive marker is incomplete

- **WHEN** archived material has a status marker but lacks required
  current-authority or supersession metadata for the preserved route/path text
- **THEN** the audit SHALL keep the finding visible for triage
- **AND** it SHALL NOT classify the finding as `budget_counted=false` solely
  because a partial marker exists.

### Requirement: Archive semantics SHALL be documented for future agents

The repository SHALL document how agents should read historical, archived, and
superseded material.

#### Scenario: agent reads archive guidance

- **WHEN** a future agent reads archive or superseded material
- **THEN** the repository guidance SHALL direct it to current authority sources
  before treating historical route/path/topology text as actionable.
