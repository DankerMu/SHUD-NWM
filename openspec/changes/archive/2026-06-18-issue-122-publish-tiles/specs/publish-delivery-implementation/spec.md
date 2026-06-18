## ADDED Requirements

### Requirement: Publish command creates delivery evidence

`nhms-pipeline publish-tiles` SHALL create or register verifiable tile delivery evidence for a forecast cycle before reporting success.

#### Scenario: Publish succeeds with artifacts

- **WHEN** `nhms-pipeline publish-tiles --cycle-id <cycle>` runs for a cycle with publishable flood or hydro products
- **THEN** it exits with code 0
- **AND** it emits JSON containing the cycle ID, `status="published"`, and at least one layer or artifact identifier
- **AND** the referenced layer or artifact can be verified in `map.tile_layer`, `map.tile_cache`, or the documented object-store publish location

#### Scenario: Publish has no products

- **WHEN** the command runs for a cycle without publishable products
- **THEN** it exits non-zero
- **AND** the JSON result includes `status="failed_publish"` with a stable error code and message
- **AND** no successful layer metadata is written for that cycle

#### Scenario: Publish is idempotent

- **WHEN** the command runs twice for the same publishable cycle
- **THEN** both successful responses reference the same logical layer or artifact identifiers
- **AND** the delivery store contains no duplicate logical layer rows or conflicting cache entries for that cycle

#### Scenario: Publish rejects unsafe discovery

- **WHEN** the requested cycle would require reading outside the configured workspace, object-store root, or object-store prefix
- **THEN** the command exits non-zero with `status="failed_publish"`
- **AND** no successful delivery metadata is written

### Requirement: Publish preserves pipeline observability

The publish stage SHALL expose success and failure state through the same pipeline job, event, and log surfaces as other M3 stages.

#### Scenario: Publish stage succeeds in orchestration

- **WHEN** the Forecast M3 chain reaches publish and the command produces delivery evidence
- **THEN** the publish pipeline job is marked successful
- **AND** the final cycle status is `complete` for full success or `parsed_partial` when upstream basin stages partially failed
- **AND** publish metadata includes published basin counts and excluded basin IDs when partial upstream stages occurred

#### Scenario: Publish stage fails in orchestration

- **WHEN** the publish command exits non-zero
- **THEN** the publish pipeline job records the error code and message
- **AND** the cycle status becomes `failed_publish`
- **AND** monitoring can distinguish publish failure from successful product publication

### Requirement: Publish uses the production Slurm command path

The real Slurm publish template SHALL invoke the same CLI implementation validated by local and mock tests.

#### Scenario: Slurm publish template executes CLI

- **WHEN** `publish_tiles.sbatch` is rendered for a real Slurm job
- **THEN** it passes the cycle ID through `NHMS_CYCLE_ID`
- **AND** it invokes `nhms-pipeline publish-tiles --cycle-id "$NHMS_CYCLE_ID"`
- **AND** non-zero command exit is not swallowed by the template

### Requirement: Publish delivery behavior is documented

The selected publish behavior SHALL be documented for operators and downstream consumers.

#### Scenario: Documentation names delivery store

- **WHEN** operators read tile publisher or pipeline documentation
- **THEN** the documentation names the implemented delivery store or object key format
- **AND** it identifies the minimum supported artifact type for this release
- **AND** it states how publish failure is surfaced
