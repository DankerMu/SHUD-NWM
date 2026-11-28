## RENAMED Requirements

- FROM: `### Requirement: Node-22 operator documentation describes execution and recovery`
- TO: `### Requirement: Operator documentation of the compute role describes execution and recovery`

## MODIFIED Requirements

### Requirement: Operator documentation of the compute role describes execution and recovery

The repository MUST document the backfill command of the compute role, required environment variables, dry-run/apply distinction, rerun behavior, and rollback boundaries.

#### Scenario: Operator follows documented command

- **WHEN** an operator reads the documentation
- **THEN** they can identify the dry-run command, the explicit `--apply` command, required env vars, that DB rows are not mutated, and how to rerun or manually roll back packages reported as copied.
