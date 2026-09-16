# Spec Delta: readonly-db-and-e2e-evidence

## ADDED Requirements

### Requirement: Reachable-roles probe MUST execute on a real PostgreSQL

The statement the readonly-DB validation lane uses to discover the roles reachable by the tested login SHALL be
executable by a real PostgreSQL server, and the lane's regression suite SHALL prove that against a real database
rather than against a recording double. Satisfying string assertions on the statement text is not evidence that the
probe runs.

#### Scenario: Reachable-roles probe runs against a live database

- **WHEN** the readonly-DB validation lane gathers reachable roles for the tested login on a real PostgreSQL
- **THEN** the statement SHALL parse and execute, returning the reachable-role rows
- **AND** it SHALL NOT use a PostgreSQL reserved word as a relation alias
- **AND** an automated test SHALL execute that probe against a real PostgreSQL, so restoring a reserved-word alias
  fails the suite instead of surfacing only as a `BLOCKED` verdict with an unexpected-error blocker on production
