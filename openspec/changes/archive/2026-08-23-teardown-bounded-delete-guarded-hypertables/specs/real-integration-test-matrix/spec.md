# real-integration-test-matrix Specification Delta

## ADDED Requirements

### Requirement: Integration teardown MUST NOT issue unbounded DELETEs against guarded hypertables

Integration test teardown SHALL bound every `DELETE` it issues against a
hypertable registered in the write guard's guarded set with a `valid_time` lower
and upper bound, and SHALL issue no `DELETE` at all when no rows match the
identity predicate. The bound SHALL be derived from the rows actually present
under that identity predicate, not from constants known to the seeding fixture,
so that extending the fixture cannot silently narrow what teardown removes.

This holds regardless of which identity column the statement uses, so that an
identity-column migration cannot drop the bound.

#### Scenario: Rows present under the identity predicate

- **WHEN** integration teardown cleans a guarded hypertable and rows exist under
  its identity predicate
- **THEN** it first reads the minimum and maximum `valid_time` present under that
  same identity predicate, and issues a single `DELETE` carrying both a
  `valid_time >=` lower bound and a `valid_time <=` upper bound covering that
  range

#### Scenario: No rows present under the identity predicate

- **WHEN** integration teardown cleans a guarded hypertable and no row exists
  under its identity predicate
- **THEN** no `DELETE` statement is issued against that hypertable, so that a
  compressed chunk elsewhere in the table cannot fail a delete that would have
  matched nothing

#### Scenario: Seeding fixture is extended with a new timestamp

- **WHEN** the seeding fixture writes a row under an existing identity at a
  `valid_time` outside the range it previously used
- **THEN** teardown still removes that row, because the bound is probed from the
  table rather than taken from the fixture's own constants
