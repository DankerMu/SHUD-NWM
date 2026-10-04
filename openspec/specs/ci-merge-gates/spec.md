# ci-merge-gates Specification

## Purpose
CI merge gates that must not pass silently. Python test jobs install the locked dependency set. A watcher reports a cancelled or failed master full regression and warns as its duration nears the timeout. The production-topology hard gate runs on every change, including docs-only ones. New hard line-number references in shipped code are rejected.

## Requirements

### Requirement: CI Python jobs install the locked dependency set
The CI jobs that run Python tests SHALL install dependencies from `uv.lock` with `uv sync --locked`, not with a floating resolution. A regression test SHALL fail when importing eccodes and then using pyproj leaves more than one PROJ library mapped, or makes the process exit abnormally.

#### Scenario: Floating environment with a second PROJ
- **WHEN** an environment loads eckit's bundled PROJ through eccodes and then uses pyproj
- **THEN** the native-PROJ isolation test fails and names the mapped libraries

#### Scenario: Locked environment
- **WHEN** the CI test jobs run
- **THEN** they use the locked versions, and the isolation test passes

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

### Requirement: The production-topology hard gate runs on every change
The production-topology hard gate SHALL run on every pull request and every master push, whatever paths changed, and a hard-gate finding SHALL fail that run.

#### Scenario: Docs-only change with a topology finding
- **WHEN** a pull request changes only an openspec markdown file and introduces a production-topology finding
- **THEN** a CI job fails and points at the finding

### Requirement: No new hard line-number references in shipped code
Comments and docstrings in shipped Python code SHALL NOT gain new `file.py:N` or `` `:N `` references beyond a frozen baseline keyed by path and reference text.

#### Scenario: New reference
- **WHEN** a change adds a `foo.py:123` comment that is not in the baseline
- **THEN** the hard gate fails naming the file and the reference

#### Scenario: Non-reference colon-digit text
- **WHEN** a comment contains `EPSG:4326` or a `host:5432` URL
- **THEN** the gate reports nothing

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
