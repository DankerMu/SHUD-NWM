# Spec Delta: pipeline-job-persistence

## MODIFIED Requirements

### Requirement: Journal existence probes SHALL enforce filesystem containment before declaring absence

Every existence probe over the file orchestration journal tree (segment slot probes, sequence-floor file probes, and latest-directory probes) SHALL resolve the probed path with the same no-follow containment discipline as the hardened readers: a symlink in any parent component, or a symlink occupying the probed slot itself, SHALL fail loud as `file_journal_unreadable` instead of being reported as "absent" (or being silently skipped) — on every public surface, read or write, that reaches the probes, with exactly the exception type and fate that a hardened-reader fault (such as a corrupt journal file) already has on that same lane: a probe-detected containment fault is never softer than a reader fault and never introduces a new exception type at any public boundary. Genuine absence — the probed entry missing under a chain of real directories, including a wholly uninitialized journal tree — SHALL still be reported as absent, and failed writes SHALL leave zero bytes written. Every cache the cycle-rows recompute consults SHALL judge the identity of its source files under this same discipline — the cycle-rows cache and the direct-jobs cycle cache alike: every stat that feeds either fingerprint — segment slots, event-log slots, the latest-directory listing, the by-cycle direct partition, the flat direct root, and the direct-jobs cache's own stats of the flat root and the by-cycle partition — SHALL resolve through the containment-aware probe, so that a real empty directory and a symlinked parent no longer fingerprint alike in any cache. A containment fault observed while fingerprinting SHALL yield a fingerprint that can neither match a cached entry nor be stored, in whichever cache observed it, and, for a parent component swapped for a symlink on a path the recompute reads, the recompute SHALL reach the same containment fault, the same reason and the same lane fate a cold instance reports on that leg (a transient stat fault the recompute does not reproduce yields uncached rows, not a new token). The token is the one raised by whichever hardened reader first reaches the tampered path, not a property of the leg alone: `file_journal_unreadable` for the segment and event-log legs in every lane and for the latest-directory leg of a model-scoped read; `file_journal_unsafe_scanned_entry` for the latest-directory leg of a cross-model read (`model_id` unset, which lists the directory through the scanned-entry discipline) and for the by-cycle direct partition and the flat direct root in every lane. A warm instance SHALL NOT keep serving a previously legal empty read after a parent component is swapped for a symlink — including when the swap leaves no child for the recompute to find, so that a bare absence stat would have compared equal before and after — and a cold instance, a warm instance and — for a parent component swapped for a symlink, the directory granularity its containment probe covers — the write-window owner SHALL give one answer for one tree; the directories-only limit stated in the cycle-write-window ownership requirement — any leaf-level change during the window, a symlink swap included — is unchanged by this. Genuine absence SHALL still fingerprint as absence, so an untouched empty directory remains a cacheable legal empty read in every cache.

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
  cycle, and afterwards `journal/<source>` or `pipeline-events/<source>` is
  replaced by a symlink to an empty decoy directory (the latest-directory and
  direct-partition legs have their own token scenarios below)
- **THEN** the same instance's next public read for that cycle reports
  `file_journal_unreadable` (as a blocked row on `list_stage_statuses`) instead
  of the cached empty result

#### Scenario: A cold and a warm instance agree on a tampered tree

- **WHEN** one instance cached the cycle before the tamper and another instance
  is created after it
- **THEN** both report the same fail-loud outcome — the same containment token,
  reason and lane fate — for the same public read, whichever cache the
  recompute would otherwise have been served from

#### Scenario: Direct-partition legs fail loud with their own containment token

- **WHEN** a warm instance cached a cycle and the by-cycle direct partition's
  per-source directory, or the flat direct root itself, is then replaced by a
  symlink to a decoy that the recompute reads
- **THEN** the next read reports `file_journal_unsafe_scanned_entry`, exactly
  as a cold instance does on the same tree

#### Scenario: The direct-jobs cycle cache does not serve across a by-cycle partition swap that leaves no child

- **WHEN** a warm instance cached a legal empty direct listing for a cycle and
  `pipeline-jobs/by-cycle/<source>` is then replaced by a symlink to a decoy
  directory that, like the original, holds no `<cycle>` child — so a bare
  absence stat of `<cycle>` would compare equal before and after
- **THEN** the next public read on that instance reports
  `file_journal_unsafe_scanned_entry` exactly as a cold instance does, for a
  model-scoped read, a cross-model read (`model_id` unset) and a read by the
  thread that owns the cycle write window alike, and the direct-jobs cache
  holds no entry computed under the tamper

#### Scenario: The latest-directory leg reports the token of the reader that reaches it

- **WHEN** a warm instance cached a cycle and the `latest/<source>/<cycle>`
  directory is then replaced by a symlink to a decoy
- **THEN** a model-scoped read reports `file_journal_unreadable` and a
  cross-model read (`model_id` unset) reports
  `file_journal_unsafe_scanned_entry`, each exactly as a cold instance does for
  the same read

#### Scenario: A fingerprint that observed a containment fault is never stored

- **WHEN** fingerprinting a cycle observes a containment fault on any path it
  stats, in the cycle-rows cache or in the direct-jobs cycle cache
- **THEN** no entry is stored in that cache for that read, so a later identical
  fingerprint cannot compare equal to it and serve rows computed under the
  tamper

#### Scenario: An untouched empty directory stays a cacheable legal empty read

- **WHEN** the cycle's directories are real and simply hold no records,
  including a real by-cycle partition with no `<cycle>` child
- **THEN** the read returns an empty result and a second read is served from
  the cache — the direct-jobs cycle cache included — without re-reading disk,
  exactly as before

### Requirement: The journal's cache fast path SHALL be granted by cycle-write-window ownership, never by the mere fact that some thread holds the write lock

The cycle-rows cache serves a hit without revalidating its source files only inside a cycle write window. That fast path is safe because two rules hold **only for the thread that owns the window, and only for the cycle that window covers**: the cycle flock excludes other writers for that cycle, and every append invalidates every reachable cache key for that source/cycle so the next read recomputes from the newly committed journal bytes. The append hook SHALL NOT be understood as updating a reachable base cache entry in place; no such base key is produced by current readers, and invalidation followed by recomputation is the governing mechanism. The fast-path predicate SHALL therefore be true when and only when the calling thread is itself inside a write window for the very cycle being read, so that a thread which merely observes another thread's write in progress — or which reads a different cycle from inside a window — falls back to full source-file revalidation, exactly as it would in a single-threaded run. The predicate SHALL NOT be satisfied by holding a write lock taken for work that is not a cycle write: a lock held for reconcile-inventory maintenance takes no cycle flock and performs no cycle-cache invalidation, so it establishes neither rule and SHALL grant no fast path. Because the ownership marker is what grants the fast path, its lifetime SHALL be bounded by the same construct that opens the window and SHALL be cleared on every exit path including exceptions — including a failure raised while the window is being established, not only one raised from the work inside it — because a marker leaked past the window would hand a fast path to whatever unrelated task next reuses that thread identity. Only one construct SHALL set the marker, so that pairing is a structural property of that construct rather than a convention repeated across call sites. The window-entry wipe SHALL be treated as a correctness precondition rather than as a performance measure: the owner bypasses fingerprint validation for every hit, so without the wipe it could serve a pre-window entry that another process has already invalidated. Reads after the wipe may cache fingerprint-free entries during the window; a subsequent append invalidates them before the next read. The owner's fast path SHALL NOT be a tamper hole either: an owner hit skips the source-file fingerprint but SHALL still probe, under the containment discipline, the directories that feed its cycle rows, so a parent component swapped for a symlink during the window turns the hit into a recompute that fails loud with the containment token a cold read reports for that directory on that lane (`file_journal_unreadable` for the journal and event-log directories in every lane and for the latest directory of a model-scoped read; `file_journal_unsafe_scanned_entry` for the latest directory of a cross-model read with `model_id` unset, and for the by-cycle partition and the flat direct root in every lane). The probe detects containment faults on the probed directories only: any other change beneath them during the window — a leaf file added, replaced, removed or swapped for a symlink — is a stated limit of the fingerprint-free owner path, not a promise of this requirement; for a change that moves a probed directory's own stat identity — an entry added, removed, replaced by rename, or swapped for a symlink — the exposure is bounded to the window, because the owner's next append invalidates every reachable key for the pair and the first read after the window recomputes from disk; an in-place rewrite of an existing leaf's bytes under the flat `pipeline-jobs` root or the by-cycle partition is not detected by the direct-jobs cycle cache's directory-granular signature inside or after the window — the pre-existing granularity of that cache, not a promise of this requirement. The window-exit wipe, by contrast, is a performance measure and SHALL be scoped to the window's own `(source_id, cycle)` keys — every derived key for that pair, the base key included — so that entries other cycles populated during the window survive its exit; the window-entry wipe stays global.

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
  fails with the containment token a cold read reports for that directory

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
