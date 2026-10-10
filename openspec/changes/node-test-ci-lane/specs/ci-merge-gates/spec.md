## ADDED Requirements

### Requirement: Script-level node tests SHALL run in a path-scoped CI lane

The CI workflow SHALL run every `*.test.mjs` file located directly in `scripts/__tests__/` in a dedicated job that is triggered only when `scripts/**/*.mjs`, `scripts/__tests__/**` or the CI workflow file itself changes. The job SHALL install no dependencies and SHALL pass the test files to `node --test` as shell-expanded file paths, so that the same command executes all cases on Node 20 and on later Node versions. The job SHALL fail when no test file matches.

#### Scenario: A change to the evidence script runs its tests

- **WHEN** a pull request changes `scripts/node27_display_v2_browser_evidence.mjs` or a file under `scripts/__tests__/`
- **THEN** the job runs, executes every case of every `*.test.mjs` file directly in `scripts/__tests__/`, and fails if any case fails

#### Scenario: Unrelated changes skip the lane

- **WHEN** a pull request changes none of the lane's paths
- **THEN** the job is skipped

#### Scenario: No matching test file fails the lane

- **WHEN** the job runs and no `*.test.mjs` file exists directly in `scripts/__tests__/`
- **THEN** the job fails instead of reporting zero tests as a pass
