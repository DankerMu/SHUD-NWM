# Spec Delta: forecast-api

## ADDED Requirements

### Requirement: A forecast-series read bound to known runs MUST converge run identity before reading facts

A forecast-series segment read SHALL constrain its fact-table access to the runs it has already determined
it will report, whether that set came from a run identity the caller supplied or from a cycle the read
resolved per scenario before fetching rows. Such a read MUST NOT scan rows belonging to runs it has
already decided to exclude.

This requirement does not extend to reads whose purpose is to discover which runs or cycles exist; those
have no run set to converge on.

Converging run identity SHALL preserve the result rows exactly. Where a read deliberately reports across
several runs — splicing analysis runs by valid time, or reporting over every run of a run type — the
constraint it applies SHALL be one that holds for all of those runs, and MUST NOT reduce the read to a
single run.

Because the legacy and narrow fact stores are physically organised on different identity columns, the
constraint SHALL be expressed per store. The narrow store's rendered read MUST NOT reference a text
identity column of the fact table; a constraint that names one SHALL appear only in the legacy store's
rendered read.

#### Scenario: A segment read bound to a single run identity

- **WHEN** a forecast-series read is bound to one run identity and executes against a production-sized
  retention window
- **THEN** the fact-table access SHALL be constrained to that run's rows
- **AND** the returned rows SHALL be identical to those the unconstrained read returns, compared by a
  digest over the sorted fields of every row, not by row count alone
- **AND** the warm plan's shared buffer usage SHALL stay within the display curve bound, while the legacy
  fact table is still present

#### Scenario: A segment read bound to a resolved set of cycles

- **WHEN** a forecast-series read has resolved one cycle per requested scenario before fetching rows
- **THEN** the fact-table access SHALL be constrained to the runs belonging to those cycles
- **AND** a read spanning more than one scenario SHALL return every row it returned before the constraint
  was introduced, for every one of those scenarios
- **AND** the warm plan's shared buffer usage SHALL stay within the display curve bound

#### Scenario: A segment read that deliberately spans runs

- **WHEN** a forecast-series read reports over every run of a run type, or splices analysis runs by valid
  time
- **THEN** the constraint applied to the fact-table access SHALL be limited to what holds for all of those
  runs
- **AND** a read that returned rows from several runs before the change SHALL still return rows from those
  same runs after it

#### Scenario: The narrow store's read is rendered from the shared source

- **WHEN** the shared segment-rows source is rendered for the narrow store
- **THEN** the rendered statement SHALL contain no text identity column of the fact table
- **AND** any identity constraint that names such a column SHALL be absent from that statement, while
  remaining present in the legacy store's rendering
