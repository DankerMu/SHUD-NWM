# Spec Delta: timeseries-product-archive

## ADDED Requirements

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
assignment accepted) whose window assignment is missing or empty SHALL
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
  assignment whose value is non-integer or non-positive, or the file
  carries the window variable only in an assignment shape the extractor
  does not support (`readonly`/`declare` prefixes, an unquoted
  leading-whitespace value, a CRLF line), or the file carries no
  recognized `NODE27_TIMESERIES_RETENTION_*` assignment at all
- **THEN** validation SHALL refuse with `ArchiveConfigurationError` (or
  the consumer's config error) and SHALL NOT fall back to any constant

#### Scenario: Missing or empty assignment mirrors the runner's default

- **WHEN** the retention env file exists, is readable, and is
  recognizably the deployed retention env (at least one other
  `NODE27_TIMESERIES_RETENTION_*` assignment is accepted) but the
  `NODE27_TIMESERIES_RETENTION_WINDOW_DAYS` assignment is absent or has
  an empty value
- **THEN** the effective window SHALL be the shared runner-equivalent
  default constant, and a minimum age satisfying that default SHALL pass
