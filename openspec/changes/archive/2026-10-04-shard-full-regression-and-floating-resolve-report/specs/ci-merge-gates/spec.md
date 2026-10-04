## ADDED Requirements

### Requirement: The master full regression runs as complete, disjoint shards

The `Unit Tests (full)` job SHALL run as a matrix of shards whose test-file sets are computed at run time by a deterministic partition of every test file pytest collects under `tests/`. The union of the shards SHALL equal that file set and the shards SHALL be pairwise disjoint; an empty shard SHALL fail. Each shard SHALL keep the marker expression `not e2e and not grib and not integration` and the locked install, SHALL NOT cancel its siblings on failure, and SHALL have a timeout no greater than the previous single-job timeout.

#### Scenario: New test file
- **WHEN** a test file absent from the duration table is added
- **THEN** it is assigned to exactly one shard with the default weight and runs in the next full regression

#### Scenario: Partition completeness
- **WHEN** the partition is computed for the repository tree
- **THEN** every collected test file appears in exactly one shard regardless of the order files are listed in

### Requirement: Floating dependency resolution is reported weekly and never gates a merge

A dedicated workflow SHALL be triggered only by a weekly schedule and by manual dispatch. It SHALL resolve dependencies without the lock, publish the version differences against `uv.lock`, run a test set that includes `tests/test_native_proj_isolation.py`, and on failure create or update a single open tracking issue identified by a fixed title. A successful run SHALL NOT open an issue. The locked installs of `ci.yml` SHALL remain unchanged.

#### Scenario: Two consecutive failures
- **WHEN** the lane fails twice in a row
- **THEN** exactly one tracking issue exists and it carries one comment per later failure

#### Scenario: Triggers
- **WHEN** the workflow file is inspected
- **THEN** its triggers are exactly `schedule` and `workflow_dispatch`

## MODIFIED Requirements

### Requirement: A failed or cancelled master full regression is reported
Whenever the CI workflow completes for a master push, a watcher SHALL inspect every shard job of `Unit Tests (full)`; the expected shard job names SHALL be derived from the matrix declared in `ci.yml`. A `cancelled`, `failure` or `timed_out` conclusion on any shard, or a missing shard, SHALL fail the watcher with an annotation carrying the SHA, the run and the shard. The watcher SHALL emit a warning when, for any shard, the P95 duration of the last 20 successful runs that carry shard jobs exceeds 80% of the configured per-shard timeout.

#### Scenario: Wall-killed full run
- **WHEN** one shard of `Unit Tests (full)` is cancelled for exceeding its time limit
- **THEN** the watcher fails and its annotation names the SHA, the run, the shard and the timeout

#### Scenario: Skipped full run
- **WHEN** `Unit Tests (full)` is skipped by the path filter, whether it appears as one unexpanded skipped job or as skipped shard jobs
- **THEN** the watcher passes

#### Scenario: Shard job missing
- **WHEN** a run that was not skipped by the path filter lacks one of the shard jobs `ci.yml` declares
- **THEN** the watcher fails naming the missing shard
