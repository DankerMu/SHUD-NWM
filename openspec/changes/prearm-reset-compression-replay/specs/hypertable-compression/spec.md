# Spec Delta: hypertable-compression

## ADDED Requirements

### Requirement: The replay arm MUST have a committed pre-arm reset that archives residue without deleting and fails closed on unsafe conditions

The controlled-replay arm SHALL be preceded by a committed pre-arm
reset script that moves the previous arm's supervisor-owned residue
into a timestamped archive directory — move-only relocation that never
discards evidence content (a cross-device move copies to the archive
before the source is removed by the standard-library fallback) —
keeping only the two files the next arm requires in place (the run
plan and the expected-stale terminal receipt), and SHALL refuse to run
— before moving any file — when the replay unit is not
inactive/failed, when the pinned expected-stale digest is missing or
malformed, when an existing terminal receipt's digest does not match
it, when the failure-intent family is unresolved, when the run plan is
present but unreadable, or when any plan label used for archive naming
is not a single safe path component (escape/traversal refusal). The
residue swept MUST include the stale finalizer state and supervisor
ledger (each would abort the next arm on its exclusive-create
refusal), and the resolved intent-family residue is swept whole, never
partially. A failure in the middle of the sweep MUST surface as the
script's own refusal message and leave a manifest covering what
already moved. When the terminal receipt is absent the sweep proceeds
but the operator MUST be warned that the arm will refuse at the
supervisor's expected-stale gate. The supervisor's own
refuse-to-overwrite trust boundary stays unchanged.

#### Scenario: Residue is archived and the next arm stays viable

- **WHEN** the pre-arm reset runs over a working directory containing
  stale checkpoint artifacts, a stale finalizer state, a stale
  supervisor ledger, and an existing plan-associated schema-dump file,
  alongside the run plan and the expected-stale terminal receipt
- **THEN** the stale artifacts, finalizer state, ledger, and
  schema-dump file are moved — content-intact — into a new timestamped
  archive directory with a manifest, while the run plan and terminal
  receipt remain in place

#### Scenario: Unsafe conditions refuse before any move

- **WHEN** the replay unit reports any state other than inactive or
  failed (including activating), or the existing terminal receipt's
  digest does not match the pinned expected-stale digest, or the
  failure-intent family shows a pending or consuming intent, or the
  run plan exists but is not valid JSON
- **THEN** the pre-arm reset exits non-zero naming the reason and the
  working directory is left byte-identical, with no archive directory
  created

#### Scenario: A mid-sweep failure still leaves a refusal and a forensic record

- **WHEN** a move fails partway through the sweep (for example the
  archive volume runs out of space)
- **THEN** the script exits non-zero with its own refusal message
  rather than a raw traceback, and the archive directory contains a
  manifest recording the pairs that had already moved and the failed
  move

#### Scenario: Re-running is safe and prior archives are preserved

- **WHEN** the pre-arm reset runs again after a previous invocation
  already produced an archive directory
- **THEN** the previous archive directory is not swept into the new
  one and remains intact, and a clean working directory yields a
  successful no-op that still prints the next arm step
