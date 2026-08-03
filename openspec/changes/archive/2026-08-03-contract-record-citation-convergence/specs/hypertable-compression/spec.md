# hypertable-compression — delta for contract-record-citation-convergence (#1273)

## ADDED Requirements

### Requirement: Contract-module measured records cite repo-resolvable determining sources

Measured records in the lane's cross-plane contract module SHALL cite
repo-resolvable artifacts whose recorded commands determine each claim
the record states — a `Source:` that resolves to nothing in the
repository (such as a gitignored `.workplans/` path), an event cited
in place of a command, or a claim broader than what the cited command
can determine (an exec-behavior claim on a `readlink` citation, an
"always" on a single snapshot) disqualifies the record; a clause
nothing determines is deleted or narrowed to the citation's coverage,
never re-asserted; and a narrative contradicted by a committed
measured snapshot is replaced by the snapshot's fact. Constant values
themselves stay under the snapshot drift lock and are not evidence
for prose claims about live behavior.

#### Scenario: Dangling source is replaced by a committed artifact

- **WHEN** a measured record's `Source:` points at an artifact that
  resolves nowhere in the repository
- **THEN** the record cites a committed artifact instead (such as the
  external-contract snapshot fixture entry with its
  `_provenance.command` and date), and tracked production code
  contains no `.workplans/` references

#### Scenario: A claim beyond its command is narrowed or deleted

- **WHEN** a record's clause asserts something its cited command
  cannot determine — exec behavior from a path-resolution command, a
  universal from a single snapshot, a version with no citation
- **THEN** the clause is deleted or narrowed to exactly the cited
  command's coverage; design rulings that are not measurements are
  stated as the plane's decision, carrying no measurement authority

#### Scenario: A falsified narrative yields to the committed snapshot

- **WHEN** a record's narrative about live behavior is contradicted
  by a committed measured snapshot entry
- **THEN** the record states the snapshot's measured fact (citing the
  entry) and cross-references the tracked issue for any runtime
  consequence, instead of retaining the falsified narrative
