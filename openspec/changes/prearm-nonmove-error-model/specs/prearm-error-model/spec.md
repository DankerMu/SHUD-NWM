# Spec Delta: prearm-error-model

## ADDED Requirements

### Requirement: Every prearm filesystem failure MUST refuse with the operator prefix and never lose sweep forensics

The pre-arm reset SHALL surface every filesystem failure — archive
directory creation, associations directory creation, the association
destination re-probe, collision probing, and manifest writes, in
addition to the already-hardened moves — as a `pre-arm reset refused: ` exit-1
refusal rather than an unhandled traceback, catching both `OSError`
and `SafeFilesystemError` (which is not an `OSError`). When the
failure strikes after any file has moved, the refusal SHALL carry
every completed `from -> to` pair and state the partial-manifest
status (written, or explicitly NOT written), so out-of-workdir
association originals remain manually reconstructable.

#### Scenario: Terminal manifest write fails after a successful sweep

- **WHEN** the terminal manifest write raises `OSError` (e.g. ENOSPC)
  or `SafeFilesystemError` after residues and an out-of-workdir
  association were archived
- **THEN** the tool exits 1 with a `pre-arm reset refused: ` message,
  prints no traceback, and lists every completed `from -> to` pair
  including the association's original absolute path plus an explicit
  manifest-not-written statement

#### Scenario: Archive directory cannot be created before anything moved

- **WHEN** creating the archive ROOT directory fails with a
  filesystem error before any move
- **THEN** the tool exits 1 with the prefixed refusal naming the
  archive root, and the workdir is byte-identical to its pre-run
  state. (A failure of the TIMESTAMPED subdirectory inside an
  already-created root also refuses with the prefix before any move,
  but the empty archive root may remain — byte-identity is asserted
  only for the root-creation case.)

#### Scenario: A mid-sweep non-move failure still carries the sweep record

- **WHEN** a non-move operation fails after at least one move
  completed (associations directory creation, the association
  destination re-probe, or collision-candidate exhaustion)
- **THEN** the tool attempts the same best-effort partial manifest as
  the move path and the prefixed refusal carries every completed
  `from -> to` pair plus the partial-manifest status, so the
  mid-sweep MUST of the hypertable-compression requirement (refusal
  message + manifest covering what already moved) holds on these
  paths too

#### Scenario: The move path is unchanged

- **WHEN** a move itself fails
- **THEN** the round-1 behavior (partial manifest then the move's own
  prefixed refusal, with a nested partial-manifest write failure
  swallowed at `:611-613`) is preserved with zero diff inside
  `_archive_move`
