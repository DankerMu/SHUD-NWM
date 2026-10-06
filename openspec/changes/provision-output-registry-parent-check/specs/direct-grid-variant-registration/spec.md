## ADDED Requirements

### Requirement: The provision step refuses an unsafe output registry directory before it does anything

The direct-grid provision step SHALL check, in a dry-run and in an apply, before it reads or prepares a receipt
and before it opens the database, that the parent directory of its output registry satisfies the condition the
registry publisher enforces on the directory of a provider destination, using the same code as the
publisher. It MUST refuse otherwise, having written nothing.

#### Scenario: Output registry in a group-writable directory

- **WHEN** a provision dry-run names an output registry whose parent directory is group-writable
- **THEN** the run is refused before the database is opened
- **AND** no receipt and no registry are written

#### Scenario: Output registry parent does not exist yet

- **WHEN** the parent directory of the output registry does not exist and its nearest existing ancestor is
  writable by the effective user
- **THEN** the run proceeds as it does for a compliant parent
