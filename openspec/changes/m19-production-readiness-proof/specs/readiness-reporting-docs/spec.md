## ADDED Requirements

### Requirement: Readiness Reporting and Docs
The project SHALL document readiness commands, required environment variables, artifact layout, scope exclusions, and interpretation rules.

#### Scenario: Report generated
WHEN a readiness command completes
THEN docs identify the command, evidence root, summary file, status meanings, live proof flags, and how to read blockers.

#### Scenario: Progress update
WHEN readiness framework lands
THEN `progress.md` records what can be validated now, what remains release-blocked, and why CLDAS/national data are excluded.
