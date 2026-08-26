## MODIFIED Requirements

### Requirement: Basins root discovery is explicit

The system SHALL discover real SHUD model assets only from an explicit Basins root configured by CLI argument or `NHMS_BASINS_ROOT`, with `data/Basins` allowed as the development default for Basins-specific commands. Unsafe-descendant detection SHALL be errno-driven rather than dependent on any Python version's `pathlib` metadata predicates swallowing an `OSError`: behavior and structured error codes SHALL be identical across supported CPython versions (3.11+).

Root metadata classification SHALL distinguish nonexistence (`ENOENT`/`ENOTDIR`) from unreadability (`EACCES`/`EPERM`). Discovery and migration-report entrypoints SHALL translate both classes into their owning structured error contracts rather than leaking raw `PermissionError`; unreadability SHALL NOT be reported as nonexistence.

#### Scenario: Discover development Basins symlink

- **WHEN** a developer runs the Basins discovery command without an explicit root and `data/Basins` points to `/volume/data/nwm/Basins`
- **THEN** the command scans that root, reports that the source is a development symlink, and records the resolved target path in the inventory

#### Scenario: Missing Basins root does not break fast tests

- **WHEN** the normal fast test command runs in an environment without `/volume/data/nwm/Basins`
- **THEN** Basins real-asset tests are skipped unless explicitly opted in, and synthetic unit tests still validate discovery behavior

#### Scenario: Explicit missing root fails discovery

- **WHEN** the Basins discovery command is run with an explicit `--basins-root` that does not exist
- **THEN** it exits non-zero with `BASINS_ROOT_NOT_FOUND`, includes the root path, and does not produce an importable inventory

#### Scenario: A Basins root denied by an ancestor is unreadable on every interpreter

- **WHEN** discovery or migration-report generation receives a Basins root whose ancestor denies traversal with `EACCES` or `EPERM`
- **THEN** it exits non-zero with the owning structured error carrying `BASINS_ROOT_UNREADABLE` and the root path
- **AND** CPython 3.11 and later interpreters produce the same code
- **AND** no raw `PermissionError` or misleading `BASINS_ROOT_NOT_FOUND` escapes

#### Scenario: Unreadable model directory fails discovery safely

- **WHEN** discovery encounters an unreadable Basins root or unreadable model subdirectory
- **THEN** it exits non-zero with `BASINS_ROOT_UNREADABLE` or `BASINS_DIRECTORY_UNREADABLE`, and does not write an importable inventory

#### Scenario: Symlink escape outside root is rejected

- **WHEN** a candidate model directory is a symlink that resolves outside the configured Basins root
- **THEN** discovery does not follow it as a valid model and reports `BASINS_SYMLINK_OUTSIDE_ROOT` as an error or warning according to the command mode

#### Scenario: Unresolvable symlink descendant blocks importability

- **WHEN** a descendant below the Basins root cannot be strictly resolved because its path contains a symlink loop or another non-permission kernel resolution defect
- **THEN** discovery records the blocking warning `BASINS_SYMLINK_UNRESOLVABLE` for that path and the affected inventory is not importable (`importable` is false, model status is not `valid`, default import is not eligible)
- **AND** this holds identically on every supported CPython version

#### Scenario: Permission denial is not a symlink verdict

- **WHEN** a descendant's strict walk reports `EACCES` or `EPERM`
- **THEN** discovery records a non-symlink unreadability warning rather than `BASINS_SYMLINK_UNRESOLVABLE`
- **AND** it does not silently treat the path as nonexistent
- **AND** containment remains fail-closed when the path cannot be established under the configured root

#### Scenario: Nonexistent descendant is not misclassified as unsafe

- **WHEN** a descendant path merely does not exist (dangling symlink or missing target, kernel `ENOENT` — including paths whose strict walk aborts at a missing component even when the lexical remainder would meet a symlink loop)
- **THEN** discovery does not emit `BASINS_SYMLINK_UNRESOLVABLE` for it and importability of an otherwise-valid inventory is unaffected — nonexistence keeps its silent-skip semantics uniformly across supported CPython versions, is never escalated to an unsafe-path verdict, and never surfaces as an unhandled exception

### Requirement: Unreadable required files degrade registration health observably

The discovery checksum walk SHALL treat a required file that was matched but cannot be read, including when strict resolution or later stat/hashing reports `EACCES` or `EPERM`, as a third-state degradation instead of silently skipping or mislabelling it as a symlink defect. The walk SHALL surface the unreadable files as their own collection (mirroring the existing invalid-required-files mechanism — the status expression consumes the collection directly, since quirks alone do not drive status), the model's status SHALL drop from `valid` to `partial` through that collection, an observable quirk marking the model as carrying an unreadable required file SHALL be recorded, and the discovery payload SHALL carry the collection under its existing key alongside the invalid-required-files key.

The missing-required-files semantics stay unchanged (a matched file is never reported as missing), actual unsafe-symlink arms keep their own semantics and are not folded into the permission arm, successful checksum entries keep their existing shape, and a `partial` model remains ineligible for default import and publication.

#### Scenario: A matched but unreadable required file yields partial status

- **WHEN** a required file matched by discovery reports `EACCES` or `EPERM` during strict resolution, stat, or hashing
- **THEN** the model's status is `partial` with an unreadable-required-file quirk
- **AND** the file is named in `unreadable_required_files` and an unreadability warning
- **AND** the file does not appear in `missing_required_files`
- **AND** no `BASINS_SYMLINK_*` warning is used for the permission failure

#### Scenario: Readable required files keep the valid status

- **WHEN** every required file is resolved, read, and hashed successfully
- **THEN** the status and checksum entries are byte-for-byte identical to the pre-change behavior