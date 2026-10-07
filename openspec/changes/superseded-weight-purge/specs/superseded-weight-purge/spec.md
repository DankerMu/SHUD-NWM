## ADDED Requirements

### Requirement: Weights of superseded models are purged only outside every protection

The node-27 weight purge tool SHALL delete `met.interp_weight` rows of a model only when the model is not the model of the latest displayable forecast run of any basin version and source, is not listed in the canonical scheduler manifest, has no run, forcing version or weight row created inside the idle window, and was registered before the idle window. It SHALL NOT write any other table.

#### Scenario: Current generation
- **WHEN** a model is the model of the latest displayable forecast run of at least one basin version and source
- **THEN** none of its weight rows are deleted

#### Scenario: Superseded generation still inside the window
- **WHEN** a superseded model has a run of any status inside the idle window, or was registered inside it
- **THEN** none of its weight rows are deleted and the report names the reason

#### Scenario: Legacy model
- **WHEN** a model whose row is still `active` has no run inside the idle window, is not current and is not in the manifest
- **THEN** its weight rows are purged like any other superseded model's

#### Scenario: Manifest unavailable
- **WHEN** the canonical manifest is missing, unreadable or lists no model
- **THEN** the tool refuses before opening the database

### Requirement: The purge is backed up, verified and dry-run by default

Without `--apply` the tool SHALL write nothing and report the classes and the purgeable models. With `--apply` it SHALL, per model in one transaction, take the weight writers' advisory locks, delete the rows and write exactly the deleted rows to a durable CSV backup with a single statement, and SHALL commit only after the backup is durable.

#### Scenario: Dry-run
- **WHEN** the tool runs without `--apply`
- **THEN** no file is created, no statement that writes is sent, and the per-class counts are printed

#### Scenario: Rows remain after the delete
- **WHEN** rows of the model still exist after its delete statement, or a database error such as a lock timeout occurs
- **THEN** the transaction is rolled back, the backup is removed, a failure receipt is written and no further model is touched

#### Scenario: Commit outcome unknown
- **WHEN** the commit call itself raises after the backup is durable
- **THEN** the backup is kept and the failure receipt states that the outcome is unknown

#### Scenario: Model becomes protected mid-run
- **WHEN** a model gains a run, or is added to the manifest, between classification and its own transaction
- **THEN** it is skipped with the reason recorded and its rows stay
