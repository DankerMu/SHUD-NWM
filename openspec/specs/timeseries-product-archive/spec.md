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
fallback; the extractor SHALL judge the retention env by a CLOSED-WORLD
line grammar — every line must be blank, a full-line `#` comment, or a
`[export ]KEY=VALUE` assignment, and any other line SHALL be refused
naming the file and the offending line — while a readable file that is
recognizably the deployed retention env (at least one other
`NODE27_TIMESERIES_RETENTION_*` assignment accepted, excluding the
archive-side pointer variable `NODE27_TIMESERIES_RETENTION_ENV`) whose
window assignment is missing or empty SHALL resolve to the shared
runner-equivalent default (the same constant the retention runner itself
defaults to), so the guard never refuses a pair the runner itself
considers healthy.

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
  assignment whose value is non-integer or non-positive, or an
  accepted-shape line carries an unsupported value (unquoted leading
  whitespace, a CRLF line), or ANY non-comment line mentions the window
  variable without being accepted as its assignment (even alongside an
  accepted plain assignment), or the file carries no recognized
  `NODE27_TIMESERIES_RETENTION_*` assignment at all
- **THEN** validation SHALL refuse with `ArchiveConfigurationError` (or
  the consumer's config error) and SHALL NOT fall back to any constant

#### Scenario: Non-grammar lines refuse closed-world

- **WHEN** the retention env file contains any non-empty line that is
  neither a full-line `#` comment nor a `[export ]KEY=VALUE` assignment
  — including `VAR+=` append, `: ${VAR:=}` default expansion, nested
  `.`/`source` lines, `printf -v`, `read`, `eval`, `readonly`/`declare`
  prefixes, or a truncated/quoted edit
- **THEN** the extractor SHALL refuse at the first such line, the
  refusal message SHALL name the file path and the offending line, and
  no runner-equivalent default SHALL be resolved from that file; a
  quoted value spanning multiple lines whose closing-quote line
  violates the grammar is likewise refused (fail-closed false
  refusal), while TWO families of divergence remain recorded as
  undetectable by a LINE-level grammar — a multi-line quoted value
  whose every line happens to conform, and a conforming line whose
  VALUE assigns the window variable through a shell expansion
  (`X=${VAR:=21}`, `X=$((VAR+=7))`) — so the deployed retention env
  MUST NOT let quoted values span lines and MUST NOT use assigning
  expansions in any value

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

