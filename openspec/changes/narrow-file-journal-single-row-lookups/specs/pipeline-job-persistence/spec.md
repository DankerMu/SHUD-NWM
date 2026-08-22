# Spec Delta: pipeline-job-persistence

## ADDED Requirements

### Requirement: Cycle-scoped single-row journal lookups with fall-open on underivable keys

A single-row journal lookup SHALL read only the cycle that owns the row.
Concretely: the file journal's single-row lookup entrypoints whose argument
carries a derivable `(source_id, cycle)` — lookup by cycle id, by run id, by
idempotency key, and by job id — SHALL resolve that pair from the argument and
read only that cycle's record sources: that cycle's `latest/<source>/<cycle>` views, that
cycle's `journal/<source>/<cycle>` segments, that cycle's
`pipeline-events/<source>/<cycle>` segments, and the direct records. That
narrowed replay SHALL NOT read any other cycle's files.

This requirement is scoped to that narrowed replay deliberately, and SHALL NOT
be read as a promise about every read reachable from these entrypoints. Where
an entrypoint satisfies a lookup from a pre-existing cycle-scoped direct reader
that establishes identity from record **content** rather than from a file name,
that reader is outside this requirement's scope and SHALL retain its existing
content-authoritative behaviour. Its own read cost is governed elsewhere, not
by this requirement.

The narrowed read SHALL be a restriction of the input set only. Its result
SHALL be identical to the result of the whole-tree scan filtered by the same
key: the same rows, resolved by the same last-write-wins merge, in the same
order, and raising the same error for a blocked or unreadable row. Any flag
that governs whether direct records participate SHALL retain its meaning
unchanged in the narrowed read.

The narrowing SHALL derive the on-disk source directory by normalising the
source token parsed from the key, because run identifiers spell the source in
lower case while the on-disk directory casing is the normalised casing and
differs per source. A lookup whose key spells the source in a different case
from its directory SHALL still resolve the row.

When the `(source_id, cycle)` pair cannot be derived with certainty — an
unrecognised identifier shape, an unparseable cycle token, or an unknown source
— the entrypoint SHALL fall back to the whole-tree scan and return its answer.
It SHALL NOT return "not found" on a derivation failure. A narrowed lookup that
misses an existing row is silent and unsafe, whereas the fallback is merely as
slow as the prior behaviour.

An entrypoint whose argument carries no derivable cycle SHALL keep the
whole-tree scan, with its semantics unchanged.

The by-cycle direct partition SHALL NOT be used as the sole record source for
any of these lookups, because it holds only the subset of rows that are current
accepted-submit candidate rows; every other row, including cohort master rows
and rows from non-forecast stages, is written outside it, in an unpartitioned
flat directory.

When the narrowed replay reads that flat direct directory, it SHALL filter by
file name rather than read the directory in full, because it retains a row per
job for all retained history and reading it whole would leave the lookup's cost
growing without bound. This obligation binds the narrowed replay only; it is
NOT a general instruction to prefilter that directory by file name, and in
particular it SHALL NOT be applied to a reader whose identity check is
content-authoritative, where a filename prefilter would change behaviour for a
name that contradicts its own content. A file SHALL be
skipped only when its name resolves to a `(source_id, cycle)` other than the
one being looked up. A file whose name does not resolve to a `(source_id,
cycle)` at all SHALL be read, so that the filename filter fails toward reading
too much rather than toward missing a row.

The filename rule above and the whole-tree parity guarantee stated earlier are
in tension for exactly one input: a flat direct file whose name resolves to a
`(source_id, cycle)` that contradicts the row's own content. The filename rule
governs that case — such a file SHALL be skipped — and the parity guarantee is
correspondingly read as holding for rows whose file name agrees with their
content. This residual is declared rather than closed: no write path produces a
contradicting row, because every job identifier is derived from a run identifier
that is itself pinned to the row's own source and cycle, and the source token is
drawn from a closed allowlist containing no separator character. Nothing at the
write boundary *enforces* that agreement, so a file introduced onto disk by any
means other than these writers is outside the parity guarantee, with the
whole-tree scan as the recovery path.

#### Scenario: A lookup by cycle id reads only that cycle's files

- **WHEN** a single-row lookup is issued with a key from which
  `(source_id, cycle)` is derivable, against a journal holding records for many
  cycles and both sources
- **THEN** the lookup opens no file belonging to any other cycle
- **THEN** the rows it returns are identical — in content, merge resolution, and
  order — to those the whole-tree scan returns when filtered by that key.

#### Scenario: A cohort master row is still found after narrowing

- **WHEN** the row that answers the lookup is a cohort master row or a row from
  a non-forecast stage, which is not written into the by-cycle direct partition
- **THEN** the narrowed lookup still returns it, because it reads that cycle's
  view and journal record sources and not the direct partition alone.

#### Scenario: A lookup by job id reads only that cycle's files

- **WHEN** a lookup is issued by a job id whose shape encodes a source and a
  cycle, and the direct record for it is absent so the lookup must fall through
  to a record replay
- **THEN** the replay reads only that cycle's record sources
- **THEN** it returns the same row the whole-tree replay would have returned
- **THEN** whether direct records participate in that replay is governed by the
  same flag, with the same meaning, as before this change.

#### Scenario: An unrecognised flat direct file name is read, not skipped

- **WHEN** the flat direct directory holds a file whose name does not resolve to
  any `(source_id, cycle)`
- **THEN** the lookup reads that file rather than skipping it
- **THEN** a file whose name resolves to a different `(source_id, cycle)` than
  the one being looked up is skipped.

#### Scenario: A source spelled in the other case still resolves

- **WHEN** the key spells the source in lower case while the journal's directory
  for that source is normalised to upper case
- **THEN** the lookup resolves the correct directory and returns the row.

#### Scenario: An underivable key falls open to the whole-tree scan

- **WHEN** the key does not match any recognised identifier shape, or its cycle
  token is not a valid cycle time, or its source is unknown
- **THEN** the entrypoint performs the whole-tree scan and returns that answer
- **THEN** it does not report the row as absent on account of the derivation
  having failed.

#### Scenario: A lookup whose argument carries no cycle is unchanged

- **WHEN** a single-row lookup is issued by an identifier that carries no
  derivable cycle
- **THEN** the entrypoint behaves exactly as it did before this change,
  returning the same row for the same journal state.
