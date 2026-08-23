# Spec Delta: pipeline-job-persistence

## ADDED Requirements

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

### Requirement: The cycle-scoped replay is memoized with a cycle-scoped invalidation signature

The cycle-scoped replay SHALL be memoized per `(source_id, cycle)` and per the
flag governing whether direct records participate, so that repeated lookups of
the same cycle within one orchestration pass do not re-read that cycle's files
once per lookup.

The memo's invalidation signature SHALL be derived exclusively from the files
that cycle's replay would itself open. Where a record source lives in a
directory shared across cycles, the signature SHALL be taken over the matched
file set rather than over the shared directory, so that a write belonging to a
different cycle does not invalidate this cycle's entry. A record source that
cannot be scoped to the cycle SHALL be recorded as a stated limitation of the
memo rather than covered by a shared directory's stat.

The memo SHALL be bounded in entry count and SHALL be safe under concurrent
orchestration threads sharing one repository instance, preserving the existing
single lock order: the signature is computed outside the cache mutex, and no
code holding the cache mutex acquires the write mutex.

#### Scenario: A write to another cycle does not evict this cycle's memo entry

- **WHEN** a cycle's replay has been memoized, and a row belonging to a
  different cycle is then written — into the same shared flat direct directory
  and the same shared per-source journal directory
- **THEN** a repeat replay of the first cycle opens no file at all
- **THEN** it returns the same rows as the first replay.

#### Scenario: A write to this cycle invalidates its memo entry

- **WHEN** a row belonging to the memoized cycle is written
- **THEN** the next replay of that cycle re-reads its files
- **THEN** it returns the newly written row rather than the stale one.

### Requirement: Journal reads are attributed to their entrypoint in pass evidence

Every read the file journal performs SHALL be counted against the entrypoint
and the reader lane that drove it, and the per-pass totals SHALL be merged into
the scheduler pass evidence artifact.

The counter SHALL be always on and SHALL ship in the repository, because the
production node from which the measurement is taken deploys by pulling the
repository and has no local-patch path, and because a probe that only runs on a
planning pass would not observe the writes that drive cache invalidation.

The counter SHALL distinguish bytes actually read from the filesystem from
reads satisfied by an in-process byte cache, so that its totals can be
reconciled against the operating system's own read accounting. It SHALL be safe
under concurrent orchestration threads sharing one repository instance, and it
SHALL NOT introduce a new lock ordering: its own mutex guards only counter
increments and is never held while any other lock is acquired.

The counters SHALL be reset at pass entry so their totals are per-pass, and the
merge into evidence SHALL be idempotent with respect to being invoked more than
once on a return path.

#### Scenario: A pass artifact carries the per-entrypoint read totals

- **WHEN** a scheduler pass completes, by any return path that writes an
  evidence artifact
- **THEN** the artifact carries a read attribution block naming, per
  entrypoint and reader lane, the number of reads and the number of bytes read
- **THEN** the totals reconcile against the sum of the per-tag rows.

#### Scenario: The counter is proven accurate, not merely self-consistent

- **WHEN** concurrent orchestration threads sharing one repository instance
  each perform a known number of reads
- **THEN** the recorded call count SHALL equal that independently known number,
  and SHALL NOT be asserted only against a total derived from the same rows it
  is being compared with — an assertion of the form
  `totals == sum(per_tag_rows)` is satisfied by construction for any counter
  content, including one that has silently lost updates under a race, and
  therefore SHALL NOT stand as the concurrency oracle for this requirement.

#### Scenario: No read escapes attribution

- **WHEN** a pass performs reads through any public journal API, including the
  cycle-status predicates and the write-path methods that read before they write
- **THEN** every counted byte SHALL carry both an entrypoint and a lane; a
  residual bucket for reads that reached no entrypoint SHALL NOT carry a
  material share of a pass's bytes, because a residual that dominates cannot
  separate baseline cost from the growth this change exists to measure.

#### Scenario: The by-cycle partition is not attributed to the flat directory

- **WHEN** a direct-record read for one cycle draws from both the unpartitioned
  flat directory and the already-partitioned by-cycle directory
- **THEN** the two SHALL be attributed to distinct lanes, so that bytes read
  from the partitioned tree are never graded against the flat directory's size.

#### Scenario: A narrowed lookup and a whole-tree lookup are told apart

- **WHEN** one lookup is answered by the cycle-scoped replay and another falls
  open to the whole-tree replay
- **THEN** the two are attributed to distinct tags, so the cost of the
  fall-open path is separable from the cost of the narrowed path.
