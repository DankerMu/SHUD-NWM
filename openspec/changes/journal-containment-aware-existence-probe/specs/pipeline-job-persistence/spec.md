# pipeline-job-persistence (delta)

## ADDED Requirements

### Requirement: Journal existence probes SHALL enforce filesystem containment before declaring absence

Every existence probe over the file orchestration journal tree (segment slot probes, sequence-floor file probes, and latest-directory probes) SHALL resolve the probed path with the same no-follow containment discipline as the hardened readers: a symlink in any parent component, or a symlink occupying the probed slot itself, SHALL fail loud as `file_journal_unreadable` instead of being reported as "absent" (or being silently skipped) — on every public surface, read or write, with exactly the exception type and fate that a hardened-reader fault (such as a corrupt journal file) already has on that same lane: a probe-detected containment fault is never softer than a reader fault and never introduces a new exception type at any public boundary. Genuine absence — the probed entry missing under a chain of real directories, including a wholly uninitialized journal tree — SHALL still be reported as absent, and failed writes SHALL leave zero bytes written.

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
