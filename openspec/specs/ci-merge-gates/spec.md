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
Whenever the CI workflow completes for a master push, a watcher SHALL inspect the `Unit Tests (full)` job. A `cancelled`, `failure` or `timed_out` conclusion SHALL fail the watcher with an annotation carrying the SHA and the run. The watcher SHALL emit a warning when the P95 duration of the last 20 successful runs exceeds 80% of the configured timeout.

#### Scenario: Wall-killed full run
- **WHEN** `Unit Tests (full)` is cancelled for exceeding its time limit
- **THEN** the watcher fails and its annotation names the SHA, the run and the timeout

#### Scenario: Skipped full run
- **WHEN** `Unit Tests (full)` is skipped by the path filter
- **THEN** the watcher passes

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
