# entropy-baseline-burndown Specification

## Purpose
TBD - created by archiving change governance-5-e1-entropy-baseline-burndown. Update Purpose after archive.
## Requirements
### Requirement: Entropy findings have normalized triage semantics

The entropy audit SHALL expose machine-readable fields that distinguish actionable findings from historical, archived, false-positive, and deferred findings.

#### Scenario: equivalent allowlist wording is normalized
- **WHEN** equivalent historical or archived findings use different human-readable allowlist wording
- **THEN** the report exposes a stable normalized allowlist identity for automation while preserving human-readable explanation text

#### Scenario: archived evidence contains a retired path token
- **WHEN** the audit scans governed archived evidence that intentionally records a retired path
- **THEN** the finding is either classified with an allowlist key or excluded from unallowlisted active-drift budgets

#### Scenario: active docs present a retired path as current
- **WHEN** the audit scans current entrypoint docs that present a retired path as an active implementation
- **THEN** the finding remains unallowlisted and budget-counted

### Requirement: Hard-gate eligibility is finding-level

The entropy audit SHALL determine future hard-gate eligibility per finding instead of failing an entire check family by default.

#### Scenario: hard-gate JSON reports finding eligibility
- **WHEN** the audit runs with `--mode hard-gate --format json`
- **THEN** each relevant finding can be counted by boolean gate eligibility and the output remains parseable even when the command exits non-zero

#### Scenario: live display e2e uses a broad API mock
- **WHEN** a live display e2e spec contains a broad `page.route('**/api/v1/**')` mock
- **THEN** that finding is gate-eligible

#### Scenario: historical mocked visual evidence uses a broad API mock
- **WHEN** a historical mocked visual regression lane contains a broad API mock and is explicitly classified as mocked evidence
- **THEN** that finding is not gate-eligible unless a later issue changes the policy

### Requirement: Entropy budget summaries are comparable

The entropy audit SHALL provide summary counts by check id, priority, role, allowlist state, and gate eligibility so cleanup PRs can compare before and after reports.

#### Scenario: before and after reports are compared
- **WHEN** maintainers provide two audit JSON reports to a comparison workflow or script
- **THEN** the comparison shows deltas for unallowlisted active drift, gate-eligible findings, and P1/P2 counts without writing `.entropy-baseline/latest.json`

#### Scenario: maintainer compares cleanup results
- **WHEN** a maintainer runs the audit before and after a cleanup PR
- **THEN** the reports expose enough summary fields to prove unallowlisted active drift decreased without increasing P1 findings

### Requirement: Retired active-tree paths cannot return silently

The repository SHALL provide an audit or test guard that checks tracked files for retired active-tree paths.

#### Scenario: retired placeholder path is reintroduced
- **WHEN** `git ls-files` contains a retired active-tree path such as `apps/web` or `workers/sbatch_templates`
- **THEN** the guard reports a governance finding or test failure

### Requirement: Governance audit remains report-only in CI

Governance-5 E1 SHALL NOT enable hard-gate mode in CI.

#### Scenario: governance workflow runs after E1
- **WHEN** `.github/workflows/governance.yml` invokes the entropy audit
- **THEN** it runs report mode only and does not pass `--mode hard-gate`

