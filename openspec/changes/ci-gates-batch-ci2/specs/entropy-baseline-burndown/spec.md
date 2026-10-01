## MODIFIED Requirements

### Requirement: Governance audit remains report-only in CI

The governance workflow's existing entropy-audit job SHALL remain report-only. The hard gate SHALL run only as a separate, dedicated job whose exit code decides that job, so that report-mode metrics and baseline handling stay untouched.

#### Scenario: governance workflow runs after E1
- **WHEN** `.github/workflows/governance.yml` runs the `Entropy Audit (report-only)` job
- **THEN** that job runs report mode only, does not pass `--mode hard-gate`, and writes no baseline

#### Scenario: governance workflow runs the hard gate
- **WHEN** `.github/workflows/governance.yml` runs for any pull request or master push
- **THEN** a separate `Production Topology Hard Gate` job runs `--mode hard-gate`, and any gated finding fails that job
