## ADDED Requirements

### Requirement: Documents declare authority status

Governance documentation SHALL define authority statuses for repository documents and explain which document wins when current docs, runbooks, specs, modules, historical plans, and worklogs conflict.

#### Scenario: newcomer evaluates a document

- **WHEN** a newcomer sees a root plan, runbook, OpenSpec worklog, or module doc
- **THEN** `docs/governance/DOC_STATUS.md` tells whether it is current, current-runbook, validation, architecture/spec, historical, superseded, or archived

#### Scenario: module docs conflict with spec

- **WHEN** module decomposition docs conflict with DB/API spec or implementation
- **THEN** the documented authority hierarchy identifies spec/implementation/current runbook as higher authority

### Requirement: Current entrypoint docs link the authority model

Current entrypoint documents SHALL link the document authority model so readers
can distinguish current guidance from historical plans. Entry-point status SHALL
describe a document's navigation or onboarding role without implying that every
embedded milestone fact in the entrypoint is freshly reconciled.

#### Scenario: historical implementation plan is checked

- **WHEN** `IMPLEMENTATION_PLAN.md` remains at the repository root
- **THEN** it clearly identifies itself as historical/superseded and points to current entrypoints, or it has been moved to `docs/archived/` with a root pointer

#### Scenario: entrypoint contains deferred stale facts

- **WHEN** a current entrypoint has known stale milestone facts deferred to a later issue
- **THEN** `docs/governance/DOC_STATUS.md` qualifies the entrypoint status so readers do not infer every fact in that document is fresh
