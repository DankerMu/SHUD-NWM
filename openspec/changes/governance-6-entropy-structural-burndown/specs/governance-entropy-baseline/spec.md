## ADDED Requirements

### Requirement: Explicit entropy baseline snapshot

The repository SHALL store the current entropy baseline only when explicitly
requested by a maintainer.

#### Scenario: maintainer requests baseline write
- **WHEN** the maintainer asks to write the current project state to
  `.entropy-baseline/latest.json` through the maintainer-only
  `scripts/governance/write_entropy_baseline.py` helper
- **THEN** the baseline file is created from the current report and includes
  timestamp, branch, commit, summary metrics, module scores, high-spread
  patterns, and cleanup priorities

#### Scenario: previous baseline exists
- **WHEN** `.entropy-baseline/latest.json` already exists before the
  maintainer-only baseline helper writes a new explicit snapshot
- **THEN** the previous file is archived under `.entropy-baseline/<timestamp>.json`
  before the new latest baseline is written

### Requirement: Audit commands remain report-only

Normal entropy audit report generation SHALL NOT create or update the entropy
baseline.

#### Scenario: JSON report is generated
- **WHEN** `uv run --no-sync python scripts/governance/audit_repo_entropy.py --format json` runs
- **THEN** `metadata.baseline_written` remains false and
  `.entropy-baseline/latest.json` is not modified by the report command

#### Scenario: Markdown report is generated
- **WHEN** `uv run --no-sync python scripts/governance/audit_repo_entropy.py --format markdown` runs
- **THEN** `.entropy-baseline/latest.json` is not created, replaced, archived,
  or modified by the report command

#### Scenario: hard-gate report is generated
- **WHEN** explicit hard-gate mode is run locally
- **THEN** stdout remains parseable and the baseline file is not written or
  modified by the command

### Requirement: Baseline supports trend comparison

The baseline SHALL preserve enough machine-readable information to compare later
entropy snapshots.

#### Scenario: future audit compares snapshots
- **WHEN** a later audit reads `.entropy-baseline/latest.json`
- **THEN** it can compare total findings, budget-counted findings,
  gate-eligible findings, module scores, high-spread patterns, and cleanup
  priority status against the current snapshot

### Requirement: Governance change review-fix loop blocks closure on P0/P1

Governance-6 work SHALL continue review-fix cycles until the OpenSpec change,
sub-issues, and epic closure evidence have no remaining P0 or P1 findings.

#### Scenario: OpenSpec review reports blocking findings
- **WHEN** a review of the OpenSpec change reports any P0 or P1 finding
- **THEN** the finding is fixed or explicitly descoped in the change artifacts
  and the same review perspective runs again before Stage 5 issue creation

#### Scenario: epic closure review reports blocking findings
- **WHEN** final epic closure review reports any P0 or P1 finding against the
  implemented sub-issues, evidence, or issue closure state
- **THEN** fixes and re-review continue until the final review evidence states
  that no P0 or P1 findings remain
