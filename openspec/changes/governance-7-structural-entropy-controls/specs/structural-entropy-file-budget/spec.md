## ADDED Requirements

### Requirement: Source file size budget SHALL be measurable and non-mechanical

The repository SHALL classify source files by structural entropy budget without
mechanically splitting single-purpose generated, fixture, or data-table files.

#### Scenario: file exceeds hard budget

- **WHEN** a tracked source file exceeds 1000 physical lines and is not an
  explicit generated/data/fixture exemption
- **THEN** the entropy report or companion structural-budget check SHALL list
  it as requiring governance
- **AND** the finding SHALL include line count, module path, detected import
  families where applicable, and the required owner action.

#### Scenario: file enters yellow budget

- **WHEN** a tracked source file has 500 to 1000 physical lines
- **THEN** the check SHALL classify it as review-only unless it also shows
  responsibility mixing, many import families, compatibility/facade logic, or
  repeated conflict-prone ownership.

#### Scenario: exemption is retained

- **WHEN** a source file above the yellow or hard budget is generated, a static
  data table, a fixture, or a single-purpose protocol artifact
- **THEN** the exemption SHALL be explicit and machine-readable enough for the
  check to report it separately from ungoverned large files.

### Requirement: Large-file changes MUST NOT add new ownership surface silently

Existing files above 1000 lines MUST NOT gain new responsibilities without a
recorded structural disposition.

#### Scenario: bugfix touches an oversized file

- **WHEN** a bugfix edits an oversized file without adding a new import family,
  compatibility symbol, public entrypoint, or validation lane
- **THEN** the PR MAY remain scoped to the bugfix
- **AND** it SHALL NOT be required to reduce total line count in the same PR.

#### Scenario: new ownership surface is added

- **WHEN** a change adds a new import family, route/lane, compatibility symbol,
  public method, or parser/validator responsibility to an oversized file
- **THEN** the change SHALL either move that responsibility to the owning module
  or update the structural inventory with an explicit temporary retention
  reason and follow-up issue.

#### Scenario: current oversized inventory is generated

- **WHEN** the repository first adopts this budget
- **THEN** each current source file above 1000 lines SHALL have a disposition:
  immediate decomposition, compatibility-facade freeze, lane-decomposition
  plan, scoped-context coverage, or explicit exemption.

### Requirement: File-budget improvement evidence SHALL be trend-safe

Line-count budget evidence SHALL measure positive entropy movement without
rewarding destructive or cosmetic changes.

#### Scenario: entropy budget is reported

- **WHEN** the structural-budget report runs
- **THEN** it SHALL include at least total oversized source files, yellow-zone
  source files, governed exemptions, and the top oversized files by module
- **AND** it SHALL NOT treat deletion of historical docs, removal of tests, or
  broken compatibility shims as positive source-file budget improvement.
