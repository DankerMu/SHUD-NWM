# Spec Delta: pipeline-job-persistence

## MODIFIED Requirements

### Requirement: Journal existence probes SHALL enforce filesystem containment before declaring absence

Every existence probe over the file orchestration journal tree (segment slot probes, sequence-floor file probes, and latest-directory probes) SHALL resolve the probed path with the same no-follow containment discipline as the hardened readers: a symlink in any parent component, or a symlink occupying the probed slot itself, SHALL fail loud as `file_journal_unreadable` instead of being reported as "absent" (or being silently skipped) — on every public surface, read or write, that reaches the probes, with exactly the exception type and fate that a hardened-reader fault (such as a corrupt journal file) already has on that same lane: a probe-detected containment fault is never softer than a reader fault and never introduces a new exception type at any public boundary. Genuine absence — the probed entry missing under a chain of real directories, including a wholly uninitialized journal tree — SHALL still be reported as absent, and failed writes SHALL leave zero bytes written. The cycle-rows cache SHALL judge the identity of its source files under this same discipline: every stat that feeds the cache fingerprint — segment slots, event-log slots, the latest-directory listing, the by-cycle direct partition and the flat direct root — SHALL resolve through the containment-aware probe, so that a real empty directory and a symlinked parent no longer fingerprint alike. A containment fault observed while fingerprinting SHALL yield a fingerprint that can neither match a cached entry nor be stored, and, wherever the recompute itself reads the tampered path, the recompute SHALL reach the same containment fault, the same reason and the same lane fate a cold instance reports on that leg: `file_journal_unreadable` for the segment, event-log and latest-directory legs, `file_journal_unsafe_scanned_entry` for the by-cycle direct partition and the flat direct root. A warm instance therefore SHALL NOT keep serving a previously legal empty read after a parent component is swapped for a symlink, and a cold instance and a warm instance SHALL give one answer for one tree. Genuine absence SHALL still fingerprint as absence, so an untouched empty directory remains a cacheable legal empty read.

#### Scenario: Symlinked parent with missing cycle file fails loud on read

- **WHEN** `journal/<source>` (or any parent component) is a symlink whose
  target directory does not contain the requested cycle's segment files
- **THEN** the public cycle read raises `file_journal_unreadable` instead of
  returning an empty record list

#### Scenario: Sequence floor probe under a symlinked parent fails loud

- **WHEN** the next-sequence computation probes segment or latest paths whose
  parent component is a symlink
- **THEN** the enclosing public write method fails loud with
  `file_journal_unreadable` instead of silently skipping the slot and
  underestimating the sequence floor

#### Scenario: Symlink occupying a probed slot fails loud

- **WHEN** a segment slot or sequence-floor path is itself a symlink
- **THEN** the operation fails loud with `file_journal_unreadable` (the error
  may originate in the probe or the hardened reader — the reason token, not
  the origin or message, is the contract)

#### Scenario: A write that silently no-opped under a symlinked parent now fails loud

- **WHEN** a public journal write method whose empty probe result previously
  made it succeed as a silent no-op (for example marking a job permanently
  failed) runs against a symlinked journal parent
- **THEN** it fails loud with `file_journal_unreadable` instead of reporting
  success

#### Scenario: Probe faults are exactly as loud as reader faults on swallow lanes

- **WHEN** an internal lane that already absorbs hardened-reader journal
  errors (returning a partial or empty result) encounters a probe-detected
  containment fault
- **THEN** the observable result is identical to that lane's existing
  behavior for an unreadable journal file — the fault is neither swallowed
  earlier than a reader fault would be, nor does it introduce a new silent
  hole

#### Scenario: Genuine absence under real directories stays a legal empty read

- **WHEN** every path component up to the missing entry is a real directory,
  or the journal tree for the source is wholly uninitialized (cold start)
- **THEN** the cycle read returns an empty list and the next-sequence
  computation returns the base sequence, exactly as before

#### Scenario: Write surfaces fail in parity with reader faults, writing nothing

- **WHEN** an append or any other public journal write targets a path whose
  parent component is a symlink (the probes now detect the fault upstream of
  the actual write)
- **THEN** the write fails closed with `file_journal_unreadable`, carried by
  the same exception type a reader fault already surfaces on that lane, with
  zero bytes written — and pre-existing reader-raised errors on the write
  path keep their current propagation unchanged

#### Scenario: A warm cache entry does not survive a parent-symlink tamper

- **WHEN** a long-lived repository instance has cached a legal empty read for a
  cycle, and afterwards `journal/<source>` (or `latest/<source>`, or any other
  parent that feeds the fingerprint) is replaced by a symlink to an empty decoy
  directory
- **THEN** the same instance's next public read for that cycle reports
  `file_journal_unreadable` (as a blocked row on `list_stage_statuses`) instead
  of the cached empty result

#### Scenario: A cold and a warm instance agree on a tampered tree

- **WHEN** one instance cached the cycle before the tamper and another instance
  is created after it
- **THEN** both report the same `file_journal_unreadable` outcome for the same
  public read

#### Scenario: Direct-partition legs fail loud with their own containment token

- **WHEN** a warm instance cached a cycle and the by-cycle direct partition's
  per-source directory, or the flat direct root itself, is then replaced by a
  symlink to a decoy that the recompute reads
- **THEN** the next read reports `file_journal_unsafe_scanned_entry`, exactly
  as a cold instance does on the same tree

#### Scenario: A fingerprint that observed a containment fault is never stored

- **WHEN** fingerprinting a cycle observes a containment fault on any path it
  stats
- **THEN** no cache entry is stored for that read, so a later identical
  fingerprint cannot compare equal to it and serve rows computed under the
  tamper

#### Scenario: An untouched empty directory stays a cacheable legal empty read

- **WHEN** the cycle's directories are real and simply hold no records
- **THEN** the read returns an empty result and a second read is served from
  the cache without re-reading disk, exactly as before

### Requirement: The journal's cache fast path SHALL be granted by cycle-write-window ownership, never by the mere fact that some thread holds the write lock

The cycle-rows cache serves a hit without revalidating its source files only inside a cycle write window. That fast path is safe because two rules hold **only for the thread that owns the window, and only for the cycle that window covers**: the cycle flock excludes other writers for that cycle, and every append invalidates every reachable cache key for that source/cycle so the next read recomputes from the newly committed journal bytes. The append hook SHALL NOT be understood as updating a reachable base cache entry in place; no such base key is produced by current readers, and invalidation followed by recomputation is the governing mechanism. The fast-path predicate SHALL therefore be true when and only when the calling thread is itself inside a write window for the very cycle being read, so that a thread which merely observes another thread's write in progress — or which reads a different cycle from inside a window — falls back to full source-file revalidation, exactly as it would in a single-threaded run. The predicate SHALL NOT be satisfied by holding a write lock taken for work that is not a cycle write: a lock held for reconcile-inventory maintenance takes no cycle flock and performs no cycle-cache invalidation, so it establishes neither rule and SHALL grant no fast path. Because the ownership marker is what grants the fast path, its lifetime SHALL be bounded by the same construct that opens the window and SHALL be cleared on every exit path including exceptions — including a failure raised while the window is being established, not only one raised from the work inside it — because a marker leaked past the window would hand a fast path to whatever unrelated task next reuses that thread identity. Only one construct SHALL set the marker, so that pairing is a structural property of that construct rather than a convention repeated across call sites. The window-entry wipe SHALL be treated as a correctness precondition rather than as a performance measure: the owner bypasses fingerprint validation for every hit, so without the wipe it could serve a pre-window entry that another process has already invalidated. Reads after the wipe may cache fingerprint-free entries during the window; a subsequent append invalidates them before the next read. The owner's fast path SHALL NOT be a tamper hole either: an owner hit skips the source-file fingerprint but SHALL still probe, under the containment discipline, the directories that feed its cycle rows, so a parent component swapped for a symlink during the window turns the hit into a recompute that fails loud with `file_journal_unreadable` exactly as a cold read would. The window-exit wipe, by contrast, is a performance measure and SHALL be scoped to the window's own `(source_id, cycle)` keys — every derived key for that pair, the base key included — so that entries other cycles populated during the window survive its exit; the window-entry wipe stays global.

#### Scenario: A non-owner thread revalidates instead of trusting a hit

- **WHEN** one thread is inside a cycle write window and another thread, sharing the same repository instance, reads cycle rows for a different cohort whose cached entry is stale
- **THEN** the reading thread revalidates the source files and returns freshly recomputed rows, never the stale cached rows

#### Scenario: The owner keeps its fast path

- **WHEN** the thread that owns a cycle write window reads cycle rows inside that window
- **THEN** the cached rows are served without computing a source-file fingerprint; only the containment probe of the directories that feed the cycle runs

#### Scenario: A window for one cycle grants nothing for another

- **WHEN** the thread that owns a write window for one cycle reads cycle rows for a different cycle
- **THEN** the read revalidates its source files, because the window's flock protects only its own cycle

#### Scenario: A lock held for non-cycle work grants nothing

- **WHEN** a thread holds the repository write lock for work that takes no cycle flock and runs no append hook
- **THEN** cycle-rows reads on any thread, including that one, still revalidate their source files

#### Scenario: The marker does not survive an exception in the window's body

- **WHEN** the body of a cycle write window raises
- **THEN** the ownership marker is cleared before the exception propagates, so a later task reusing the same thread identity gets no fast path

#### Scenario: The marker does not survive a failure while the window is opening

- **WHEN** establishing the window fails before its body is ever entered
- **THEN** the ownership marker is cleared just the same, because the thread identity is released back to the pool either way

#### Scenario: A non-owner read does not depend on cache-clearing granularity

- **WHEN** the cycle write window's cache clearing is disabled entirely and a thread that owns no window reads a different cycle
- **THEN** that read still returns correct values, because a non-owner read rests on revalidation rather than on eviction

#### Scenario: An owner read does depend on the window-entry wipe

- **WHEN** the same clearing is disabled and the window owner reads its own cycle, for which a pre-window entry was cached and then invalidated by another process
- **THEN** the owner would serve that stale entry — which is why the window-entry wipe is a correctness precondition and not a tunable

#### Scenario: An owner hit under a tampered parent fails loud

- **WHEN** the window owner has a cached entry for its own cycle and, between
  two of its in-window reads, a parent directory feeding that cycle is replaced
  by a symlink
- **THEN** the second read does not serve the cached rows; it recomputes and
  fails with `file_journal_unreadable`, the same outcome a cold read reports

#### Scenario: The window-exit wipe leaves other cycles' entries intact

- **WHEN** cohort X's write window is open and, during it, an entry for a
  different cycle Y is populated on the same repository instance
- **THEN** after X's window exits, Y's entry is still cached and Y's next read
  makes no disk read, while every key for X's own `(source_id, cycle)` —
  including the base key — is gone

#### Scenario: The window-entry wipe is not narrowed

- **WHEN** a write window opens
- **THEN** the whole cycle-rows cache is cleared, regardless of which cycle the
  window covers, because the owner's fast path trusts any hit it finds

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

## ADDED Requirements

### Requirement: Source-segment discovery SHALL deduplicate per-source directories by filesystem identity on every path that offers more than one spelling

Source-segment discovery SHALL collapse two spellings of a per-source directory segment into one only when some surface proves, by `(st_dev, st_ino)` under a no-follow stat, that they name one directory — on every path that offers more than one spelling for one cycle read: the primary alias list, an explicit list of segment overrides, and the merge of segments discovered across the `latest`, `journal`, `pipeline-events` and by-cycle surfaces. On a case-insensitive filesystem
this guarantees every per-source directory is enumerated once per cycle read,
so record and file budgets are not silently doubled; on a case-sensitive
filesystem no surface can prove identity, so both spellings are kept and both
directories are read, unchanged. A symlinked alias keeps its own inode under the
no-follow stat and SHALL therefore stay in the list, where the read path's
containment discipline fails it closed. Collapsing SHALL NOT weaken the existing
checks on an override list: each override is still validated against the
requested source, and an override list that dedupes to nothing still fails
closed as a missing identity.

#### Scenario: Mixed spellings discovered across surfaces read one directory once

- **WHEN** on a case-insensitive filesystem the cycle's source is discovered
  as `latest/IFS` on one surface and `journal/ifs` on another
- **THEN** a public read for that cycle opens no `(st_dev, st_ino)` under two
  spellings, so every record is read exactly once

#### Scenario: Two real directories on a case-sensitive filesystem are both read

- **WHEN** `journal/gfs` and `journal/GFS` are distinct real directories each
  holding records
- **THEN** the read enumerates both and returns rows from both

#### Scenario: An override list with both spellings collapses only when proven identical

- **WHEN** a cycle read is given segment overrides `("IFS", "ifs")`
- **THEN** on a case-insensitive filesystem one segment is read; on a
  case-sensitive filesystem both are; an override naming a different source is
  still rejected as a source mismatch; and an override list left empty after
  collapsing still fails closed as a missing identity

#### Scenario: A symlinked alias is kept and fails closed

- **WHEN** one spelling of the segment is a symlink to a directory elsewhere
- **THEN** it is not collapsed into the real directory's entry, and the read
  through it fails closed under the containment discipline
