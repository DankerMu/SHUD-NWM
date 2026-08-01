# Spec Delta: timeseries-product-archive

## MODIFIED Requirements

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
  refusal), while a multi-line quoted value whose every line happens
  to conform to the grammar remains a recorded divergence the line
  grammar cannot detect — quoted values MUST NOT span lines in the
  deployed retention env

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
