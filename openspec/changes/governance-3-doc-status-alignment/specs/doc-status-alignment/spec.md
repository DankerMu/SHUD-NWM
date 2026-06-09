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

### Requirement: node-27 live MVT facts and display config are synchronized

Current entrypoints, node-27 runbooks, and display readonly example config SHALL
reflect the 2026-06-08 live PostGIS MVT receipt: `NHMS_ENABLE_LIVE_POSTGIS_MVT`
is enabled for display readonly deployments, `/api/v1/layers` returns live
layers when the feature is enabled, #351 is recorded as the PR that closed #343's
live MVT root-cause investigation, #342 station-MVT remains a separate open
backend issue, and #389 routes the remaining bbox/framing popup live-click
browser evidence gap.

#### Scenario: display env example is used

- **WHEN** an operator uses `infra/env/display.example`
- **THEN** it documents `NHMS_ENABLE_LIVE_POSTGIS_MVT=true` for readonly live MVT overlays

#### Scenario: display compose config is rendered

- **WHEN** `docker compose --env-file infra/env/display.example -f infra/compose.display.yml config` is rendered
- **THEN** the `display-api` service environment passes `NHMS_ENABLE_LIVE_POSTGIS_MVT` through to the container

#### Scenario: display readonly safety is preserved

- **WHEN** display env or compose config is updated to enable live MVT
- **THEN** it preserves `NHMS_SERVICE_ROLE=display_readonly`, disabled control mutations, readonly database-role intent, compose `read_only: true`, readonly published artifact mount, and no new Slurm or control-plane capability

#### Scenario: current milestone docs are checked

- **WHEN** `CLAUDE.md` describes active work
- **THEN** it no longer presents M23 as the current active milestone after M25/M26 and governance work landed

#### Scenario: node-27 checklist references MVT status

- **WHEN** node-27 bringup checklist describes C4/MVT status
- **THEN** it no longer treats #343 as open, cites #351/live receipt for the closed live MVT root cause, keeps station-MVT/#342 separate, and routes bbox/framing popup live-click evidence to #389
