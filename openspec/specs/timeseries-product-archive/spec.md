# timeseries-product-archive Specification

## Purpose
TBD - created by archiving change audit-producer-window-containment. Update Purpose after archive.
## Requirements
### Requirement: Producer provenance window verification SHALL use containment semantics

When the inventory audit binds an archived product's producer provenance to its DB inventory subject, identity fields (kind, stable subject identifier, producer-manifest path, model and basin-version identity) SHALL be compared by equality, while the producer window SHALL be verified by containment — the audit SHALL accept the subject when the producer window contains the DB subject window (producer start at or before the subject start AND producer end at or after the subject end) and SHALL block with a stable sanitized reason when the producer window does not contain the DB window, when a window value is missing or unparseable, or when any identity field differs.

#### Scenario: Superset producer window passes verification

- **WHEN** an archived product's producer window strictly contains the DB
  subject window (for example, the archive covers three additional hours
  beyond a DB row truncated by an ingest interruption)
- **THEN** producer provenance verification passes and the subject remains
  eligible for `product-archive` coverage classification

#### Scenario: Subset producer window blocks fail-closed

- **WHEN** an archived product's producer window ends before the DB
  subject window ends (the archive is missing coverage the DB holds)
- **THEN** the audit blocks the subject with a stable sanitized message
  identifying the subject, and the receipt outcome remains fail-closed

#### Scenario: Identity field mismatch blocks fail-closed

- **WHEN** any producer identity field (kind, subject identifier,
  manifest path, model, or basin-version identity) differs from the DB
  subject
- **THEN** the audit blocks the subject exactly as before this change

### Requirement: Archive min-age validation MUST compare against the live DB retention window

Configuration validation SHALL, for both the product-archive mover and the
storage inventory audit, compare `NHMS_ARCHIVE_MIN_AGE_DAYS` against the
LIVE DB retention window read from the deployed retention env file (the
`NODE27_TIMESERIES_RETENTION_WINDOW_DAYS` assignment in the file named by
the required `NODE27_TIMESERIES_RETENTION_ENV` variable), never against a
compile-time constant; a minimum age below the live window SHALL be
rejected before any discovery or mutation; an unreadable window SOURCE
(unset/empty/relative path variable, missing file, or a present
non-integer or non-positive value, an assignment shape the extractor does
not support, or a readable file with no recognized retention-family
assignment at all) SHALL be rejected fail-closed with no constant
fallback, while a readable file that is recognizably the deployed
retention env (at least one other `NODE27_TIMESERIES_RETENTION_*`
assignment accepted, excluding the archive-side pointer variable
`NODE27_TIMESERIES_RETENTION_ENV`) whose window assignment is missing
or empty SHALL
resolve to the shared runner-equivalent default (the same constant the
retention runner itself defaults to), so the guard never refuses a pair
the runner itself considers healthy.

#### Scenario: Live drifted pair is rejected

- **WHEN** the deployed retention env carries
  `NODE27_TIMESERIES_RETENTION_WINDOW_DAYS=21` and the mover or audit is
  configured with `NHMS_ARCHIVE_MIN_AGE_DAYS=14`
- **THEN** configuration validation SHALL refuse before any discovery or
  mutation, and the refusal message SHALL name both live numbers

#### Scenario: Equal or larger min age passes

- **WHEN** the minimum age equals or exceeds the live window value
- **THEN** configuration validation SHALL accept the pair (no refusal from
  this guard)

#### Scenario: Unreadable window source fails closed

- **WHEN** `NODE27_TIMESERIES_RETENTION_ENV` is unset, empty, or a
  relative path, or names a missing file, or the file carries a present
  assignment whose value is non-integer or non-positive, or ANY
  non-comment line carries the window variable in a DETECTABLE
  unsupported assignment shape (a line containing the literal
  `NODE27_TIMESERIES_RETENTION_WINDOW_DAYS=` substring that is not
  accepted as its assignment — `readonly`/`declare` prefixes,
  truncated edits — or an accepted-shape line with an unsupported
  value: unquoted leading whitespace, a CRLF line; even alongside an
  accepted plain assignment), or the file carries no recognized
  `NODE27_TIMESERIES_RETENTION_*` assignment at all (shell forms that
  set the variable without that detectable substring are a recorded
  residual tracked by issue #1230 — see design D5(d))
- **THEN** validation SHALL refuse with `ArchiveConfigurationError` (or
  the consumer's config error) and SHALL NOT fall back to any constant

#### Scenario: Missing or empty assignment mirrors the runner's default

- **WHEN** the retention env file exists, is readable, and is
  recognizably the deployed retention env (at least one other
  `NODE27_TIMESERIES_RETENTION_*` assignment is accepted, where the
  archive-side pointer variable `NODE27_TIMESERIES_RETENTION_ENV`
  itself never counts as such evidence) but the
  `NODE27_TIMESERIES_RETENTION_WINDOW_DAYS` assignment is absent or has
  an empty value
- **THEN** the effective window SHALL be the shared runner-equivalent
  default constant, and a minimum age satisfying that default SHALL pass

