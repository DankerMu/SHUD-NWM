## REMOVED Requirements

### Requirement: The frontend CI filter entries SHALL be pinned by a meta-test

**Reason**: Its only enforcement was a test in the retired selector suite and its subject is the workflow's own text (#3219, D6); it is removed rather than kept as unenforced prose.
**Migration**: None; the workflow is validated by actionlint and by running.

### Requirement: Workflow files SHALL be linted on pull requests that change them

**Reason**: Replaced by "Workflow files SHALL be linted by actionlint on pull requests that change them": the selector scenarios of this requirement are cut (#3219), and OpenSpec refuses a MODIFIED block that drops a scenario, so the surviving text moves to a new header.
**Migration**: See the ADDED requirement "Workflow files SHALL be linted by actionlint on pull requests that change them"; apart from the selector cuts its body and scenarios are unchanged.

## RENAMED Requirements

- FROM: `### Requirement: The master full regression runs as complete, disjoint shards`
- TO: `### Requirement: The full regression runs on pull requests and master pushes as complete, disjoint shards`

## MODIFIED Requirements

### Requirement: The full regression runs on pull requests and master pushes as complete, disjoint shards

The `Unit Tests (full)` job SHALL run on every manual `workflow_dispatch`, and on every pull request and every push to master whose diff has a path outside `apps/frontend/`. The `Unit Tests (full)` job SHALL run as a matrix of shards whose test-file sets are computed at run time by a deterministic partition of every test file pytest collects under `tests/`. The union of the shards SHALL equal that file set and the shards SHALL be pairwise disjoint; an empty shard SHALL fail. Each shard SHALL keep the marker expression `not e2e and not grib and not integration` and the locked install, SHALL NOT cancel its siblings on failure, and SHALL have a timeout no greater than the previous single-job timeout.

#### Scenario: New test file
- **WHEN** a test file absent from the duration table is added
- **THEN** it is assigned to exactly one shard with the default weight and runs in the next full regression

#### Scenario: Partition completeness
- **WHEN** the partition is computed for the repository tree
- **THEN** every collected test file appears in exactly one shard regardless of the order files are listed in

#### Scenario: Only a frontend-only diff skips the regression
- **WHEN** a pull request or master push changes only paths under `apps/frontend/`
- **THEN** the `Unit Tests (full)` job is skipped, while a diff that changes only `docs/` or `openspec/` runs every shard

## ADDED Requirements

### Requirement: Workflow files SHALL be linted by actionlint on pull requests that change them

The CI workflow SHALL run a pinned actionlint over every file under `.github/workflows/` in a job gated by a `workflows` path filter that matches `.github/workflows/**`. The job SHALL NOT run on pull requests that leave the workflow files untouched and SHALL NOT be a dependency of any other job.

#### Scenario: A pull request edits a workflow

- **WHEN** a pull request changes any file under `.github/workflows/`
- **THEN** the Workflow Lint job runs and fails the pull request if actionlint reports a finding
