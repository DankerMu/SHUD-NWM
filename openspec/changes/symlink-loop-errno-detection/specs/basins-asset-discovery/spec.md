# basins-asset-discovery (delta)

## MODIFIED Requirements

### Requirement: Basins root discovery is explicit

The system SHALL discover real SHUD model assets only from an explicit Basins root configured by CLI argument or `NHMS_BASINS_ROOT`, with `data/Basins` allowed as the development default for Basins-specific commands. Unsafe-descendant detection SHALL be errno-driven (strict resolution surfacing kernel errors such as `ELOOP`) rather than dependent on any Python version's non-strict resolution raising — behavior is identical across supported CPython versions (3.11+).

#### Scenario: Discover development Basins symlink

- **WHEN** a developer runs the Basins discovery command without an explicit root and `data/Basins` points to `/volume/data/nwm/Basins`
- **THEN** the command scans that root, reports that the source is a development symlink, and records the resolved target path in the inventory

#### Scenario: Missing Basins root does not break fast tests

- **WHEN** the normal fast test command runs in an environment without `/volume/data/nwm/Basins`
- **THEN** Basins real-asset tests are skipped unless explicitly opted in, and synthetic unit tests still validate discovery behavior

#### Scenario: Explicit missing root fails discovery

- **WHEN** the Basins discovery command is run with an explicit `--basins-root` that does not exist or cannot be read
- **THEN** it exits non-zero, emits a structured error containing the root path and error code, and does not produce an importable inventory

#### Scenario: Unreadable model directory fails discovery safely

- **WHEN** discovery encounters an unreadable Basins root or unreadable model subdirectory
- **THEN** it exits non-zero with `BASINS_ROOT_UNREADABLE` or `BASINS_DIRECTORY_UNREADABLE`, and does not write an importable inventory

#### Scenario: Symlink escape outside root is rejected

- **WHEN** a candidate model directory is a symlink that resolves outside the configured Basins root
- **THEN** discovery does not follow it as a valid model and reports `BASINS_SYMLINK_OUTSIDE_ROOT` as an error or warning according to the command mode

#### Scenario: Unresolvable symlink descendant blocks importability

- **WHEN** a descendant below the Basins root cannot be strictly resolved for a reason other than nonexistence (symlink loop / `ELOOP`, or another kernel resolution error)
- **THEN** discovery records the blocking warning `BASINS_SYMLINK_UNRESOLVABLE` for that path and the affected inventory is not importable (`importable` is false, model status is not `valid`, default import is not eligible)
- **AND** this holds identically on every supported CPython version — the detection never relies on non-strict path resolution raising.

#### Scenario: Nonexistent descendant is not misclassified as unsafe

- **WHEN** a descendant path merely does not exist (dangling symlink or missing target, kernel `ENOENT`)
- **THEN** discovery does not emit `BASINS_SYMLINK_UNRESOLVABLE` for it and importability of an otherwise-valid inventory is unaffected — nonexistence keeps its pre-existing silent-skip semantics and is never escalated to an unsafe-path verdict.
