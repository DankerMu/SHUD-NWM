## MODIFIED Requirements

### Requirement: Cycle-scoped single-row journal lookups with fall-open on underivable keys

A single-row journal lookup SHALL read only the cycle that owns the row.
Concretely: the file journal's single-row lookup entrypoints whose argument
carries a derivable `(source_id, cycle)` — lookup by cycle id, by run id, by
idempotency key, and by job id — SHALL resolve that pair from the argument and
read only that cycle's record sources: that cycle's `latest/<source>/<cycle>` views, that
cycle's `journal/<source>/<cycle>` segments, and the direct records. That
narrowed replay SHALL NOT read any other cycle's files.

This requirement is scoped to that narrowed replay deliberately, and SHALL NOT
be read as a promise about every read reachable from these entrypoints. It does
bind every reader of the unpartitioned flat direct directory: the filename rule
stated below SHALL have exactly one definition, shared by reference, so that a
second reader of that directory cannot be corrected independently of the first
or left uncorrected. A reader that establishes a row's identity from record
**content** SHALL retain that content check; the filename rule is a prefilter
ahead of it, not a replacement for it.

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

That fallback is bounded by the journal's aggregate record budget, and on a
production-sized tree the budget is reached before the scan completes, so the
fallback SHALL be treated as a lane that can refuse rather than as one that is
merely slow. A whole-tree replay that exhausts the budget SHALL carry evidence
naming the read lane it was refused on, so that a whole-tree refusal is
distinguishable from a cycle-scoped refusal that reports the same reason token
and the same field. The refusal SHALL NOT be avoided by raising the aggregate
record budget: the budget bounds read work rather than result size, and a
larger default only moves the point at which a larger tree reaches it.

The single-row and by-cycle/by-run query entrypoints named by this requirement
— lookup by idempotency key, by job id, by cycle id, by run id and by scheduler
job id — convert such a refusal into a synthetic row, and that row SHALL NOT be
presented as an ordinary running job. (Blocked-read sentinels produced by other
surfaces — the active scheduler-job listing, the blocked candidate-state
projection and the blocked stage-status projection — are outside this
requirement and keep their own vocabulary, because their consumers adjudicate
on different allowlists.) The synthetic row SHALL remain
present (never absent, never an empty result) and SHALL remain non-terminal, so
that every caller which treats a non-terminal row as an in-flight job keeps
refusing to schedule, reserve or reuse against an unread journal; and its
status SHALL name the blocked read rather than borrow the vocabulary of a job
that is actually running. The blocked read SHALL stay identifiable by the
structured marker the readers already key on, and the identifiers the row
carries SHALL be unchanged by this requirement.

An entrypoint whose argument carries no derivable cycle SHALL keep the
whole-tree scan, with its semantics unchanged.

The by-cycle direct partition SHALL NOT be used as the sole record source for
any of these lookups, because it holds only the subset of rows that are current
accepted-submit candidate rows; every other row, including cohort master rows
and rows from non-forecast stages, is written outside it, in an unpartitioned
flat directory.

When a cycle-scoped read of that flat direct directory happens, it SHALL filter
by file name rather than read the directory in full, because the directory
retains a row per job for all retained history and reading it whole would leave
the lookup's cost growing without bound. This obligation binds every
cycle-scoped reader of that directory, not only the narrowed replay, and the
filter SHALL be a single shared definition rather than a per-reader copy. The
comparison SHALL normalise the source token before comparing, because the
callers spell the source in both the run-identifier casing and the on-disk
casing, and a raw comparison would skip every file of a source passed in the
other case. A file SHALL be skipped only when its name resolves to a
`(source_id, cycle)` other than the one being looked up. A file whose name does not resolve to a `(source_id,
cycle)` at all SHALL be read, so that the filename filter fails toward reading
too much rather than toward missing a row.

The filename rule above and the whole-tree parity guarantee stated earlier are
in tension for exactly one input: a flat direct file whose name resolves to a
`(source_id, cycle)` that contradicts the row's own content. The filename rule
governs that case — such a file SHALL be skipped — and the parity guarantee is
correspondingly read as holding for rows whose file name agrees with their
content. That agreement SHALL be enforced at the write boundary: when a row's job
identifier resolves to a `(source_id, cycle)`, a pipeline-job write whose row
carries a different source or a different cycle SHALL be rejected with
`file_journal_job_id_scope_mismatch` before any byte of that write — journal
record or direct file — reaches disk, with evidence naming the expected and the
actual pair. The comparison SHALL normalise the source on both sides and compare
the cycle in its canonical segment form. A job identifier that does not resolve
to a pair SHALL be accepted exactly as before, so the fall-open rule above is
unchanged. The read-side identity validation SHALL NOT decompose the job
identifier, so a historical row that pre-dates the gate is not turned into a
replay fault. A file introduced onto disk by any means other than these writers
remains outside the parity guarantee, with the whole-tree scan as the recovery
path.

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

#### Scenario: A malformed flat direct file of another cycle does not block this one

- **WHEN** the flat direct directory holds an unreadable or malformed file whose
  name resolves to a `(source_id, cycle)` other than the one being looked up
- **THEN** no cycle-scoped reader of that directory opens it, so the lookup for
  this cycle succeeds
- **THEN** a malformed file whose name resolves to the cycle being looked up
  still fails the lookup closed, with its existing error.

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

#### Scenario: A row whose job id contradicts its own cycle is rejected with nothing written

- **WHEN** a pipeline-job write carries a `job_id` that resolves to a cycle
  different from the row's own `cycle_time`
- **THEN** the write fails with `file_journal_job_id_scope_mismatch`, its
  evidence names the expected and actual `(source, cycle)`, and afterwards no
  journal record for that row exists in any segment and no direct file for that
  `job_id` exists in the flat directory or the by-cycle partition

#### Scenario: A row whose job id contradicts its own source is rejected the same way

- **WHEN** the `job_id` resolves to a source different from the row's own
  `source_id`
- **THEN** the write fails with the same reason and the same zero-bytes-written
  guarantee

#### Scenario: A job id that resolves to no pair is still accepted

- **WHEN** the `job_id` matches neither recognised identifier shape
- **THEN** the write is accepted and the row is readable afterwards, so the
  fall-open rule is unchanged at the write boundary


#### Scenario: A whole-tree refusal is distinguishable from a cycle-scoped refusal

- **WHEN** a whole-tree replay exhausts the aggregate record budget
- **THEN** the raised fault carries evidence naming the whole-tree read lane
- **THEN** a cycle-scoped replay that exhausts the same budget carries the
  cycle-scoped lane instead, so the two are distinguishable even though the
  reason token and field are the same

#### Scenario: A blocked read stays in-flight for every scheduling guard

- **WHEN** a single-row or by-cycle/by-run lookup refuses because the journal
  could not be read within the budget
- **THEN** the entrypoint returns a row (or a one-row list), never `None` and
  never an empty list, so the duplicate-submission guards that key on presence
  keep refusing
- **THEN** that row's status is not a terminal status, so the active-job guards
  keep treating the cycle or run as occupied
- **THEN** that row is not reusable as an auto-retry target

#### Scenario: A blocked read does not claim the job is running

- **WHEN** a caller inspects the row produced by a blocked read
- **THEN** the row's status names the blocked read rather than reporting the
  job as running
- **THEN** the structured blocked marker, the reason token and the job
  identifiers the row carries are unchanged, so readers that key on the marker
  behave exactly as before
