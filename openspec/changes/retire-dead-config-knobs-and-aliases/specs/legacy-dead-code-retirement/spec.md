## ADDED Requirements

### Requirement: Configuration names have a reader, and retired aliases are migrated before removal

A configuration key that is templated, set in a live env file or documented as live SHALL have a reader on the
surface it is templated for. Otherwise it SHALL be removed from that surface, and from the live env files after a
backup. A legacy alias SHALL be removed only after every live env file that sets it has been migrated to a key with
the same effective value. When an alias stands for more than one primary key, the migration SHALL preserve the
effective value of each. After removal, a reader that sees only the retired alias SHALL fail closed and name the
primary key.

#### Scenario: a legacy guard alias is retired without changing live behaviour

- **WHEN** a retired guard alias is the effective source of a live guard mode on a production node
- **THEN** the live env gains the primary key with the same value before the reader drops the alias, the next
  production pass resolves the same guard modes, and a configuration carrying only the alias is refused
