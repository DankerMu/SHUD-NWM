# Spec Delta: pipeline-job-persistence

## MODIFIED Requirements

### Requirement: Journal existence probes SHALL enforce filesystem containment before declaring absence

Every existence probe over the file orchestration journal tree (segment slot probes, sequence-floor file probes, and latest-directory probes) SHALL resolve the probed path with the same no-follow containment discipline as the hardened readers: a symlink in any parent component, or a symlink occupying the probed slot itself, SHALL fail loud as `file_journal_unreadable` instead of being reported as "absent" (or being silently skipped) — on every public surface, read or write, that reaches the probes, with exactly the exception type and fate that a hardened-reader fault (such as a corrupt journal file) already has on that same lane: a probe-detected containment fault is never softer than a reader fault and never introduces a new exception type at any public boundary. Genuine absence — the probed entry missing under a chain of real directories, including a wholly uninitialized journal tree — SHALL still be reported as absent, and failed writes SHALL leave zero bytes written. Every cache the cycle-rows recompute consults SHALL judge the identity of its source files under this same discipline — the cycle-rows cache and the direct-jobs cycle cache alike: every stat that feeds either fingerprint — segment slots, event-log slots, the latest-directory listing, the by-cycle direct partition, the flat direct root, and the direct-jobs cache's own stats of the flat root and the by-cycle partition — SHALL resolve through the containment-aware probe, so that a real empty directory and a symlinked parent no longer fingerprint alike in any cache. A containment fault observed while fingerprinting SHALL yield a fingerprint that can neither match a cached entry nor be stored, in whichever cache observed it, and, for a parent component swapped for a symlink, the recompute SHALL reach the same containment fault, the same reason and the same lane fate a cold instance reports on that leg (a transient stat fault the recompute does not reproduce yields uncached rows, not a new token). The token is the one raised by whichever hardened reader first reaches the tampered path, not a property of the leg alone: `file_journal_unreadable` for the segment and event-log legs in every lane and for the latest-directory leg of a model-scoped read; `file_journal_unsafe_scanned_entry` for the latest-directory leg of a cross-model read (`model_id` unset, which lists the directory through the scanned-entry discipline) and for the by-cycle direct partition and the flat direct root in every lane. A warm instance SHALL NOT keep serving a previously legal empty read after a parent component is swapped for a symlink — including when the swap leaves no child for the recompute to find, so that a bare absence stat would have compared equal before and after — and a cold instance, a warm instance and — for a parent component swapped for a symlink, the directory granularity its containment probe covers — the write-window owner SHALL give one answer for one tree; the leaf-swap limit stated in the cycle-write-window ownership requirement is unchanged by this. Genuine absence SHALL still fingerprint as absence, so an untouched empty directory remains a cacheable legal empty read in every cache.

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
