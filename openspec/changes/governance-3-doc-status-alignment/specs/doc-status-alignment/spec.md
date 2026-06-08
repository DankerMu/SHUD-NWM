## ADDED Requirements

### Requirement: Documents declare authority status

Governance documentation SHALL define authority statuses for repository documents and explain which document wins when current docs, runbooks, specs, modules, historical plans, and worklogs conflict.

#### Scenario: newcomer evaluates a document
- **WHEN** a newcomer sees a root plan, runbook, OpenSpec worklog, or module doc
- **THEN** `docs/governance/DOC_STATUS.md` tells whether it is current, current-runbook, validation, architecture/spec, historical, superseded, or archived

#### Scenario: module docs conflict with spec
- **WHEN** module decomposition docs conflict with DB/API spec or implementation
- **THEN** the documented authority hierarchy identifies spec/implementation/current runbook as higher authority

### Requirement: node-27 live MVT facts are synchronized

Docs and display environment examples SHALL reflect the 2026-06-08 `display_readonly` live PostGIS MVT receipt: `NHMS_ENABLE_LIVE_POSTGIS_MVT=true`, `/api/v1/layers` returns live layers, and station-MVT remains a separate open backend issue.

#### Scenario: display env example is used
- **WHEN** an operator uses `infra/env/display.example`
- **THEN** it contains or explicitly documents `NHMS_ENABLE_LIVE_POSTGIS_MVT=true` for live readonly MVT overlays

#### Scenario: display compose config is rendered
- **WHEN** `docker compose --env-file infra/env/display.example -f infra/compose.display.yml config` is rendered
- **THEN** the `display-api` service environment passes `NHMS_ENABLE_LIVE_POSTGIS_MVT` through to the container

#### Scenario: node-27 checklist references MVT status
- **WHEN** node-27 bringup checklist describes C4/MVT status
- **THEN** it no longer treats #343 as open root cause and clearly separates remaining station-MVT/#342 or bbox/click automation gaps

### Requirement: Bugs are triaged as a ledger

`docs/bugs.md` SHALL record each governed bug with status, owner area, evidence, and retest command.

#### Scenario: old bug is resolved or superseded
- **WHEN** an old bug has been fixed or made obsolete by later milestones
- **THEN** it is marked `resolved` or `superseded` with evidence and a retest command

#### Scenario: bug remains open
- **WHEN** a bug remains open
- **THEN** it has an owner area among `compute_control`, `display_readonly`, `slurm_gateway`, or `shared_contract`

### Requirement: Current entrypoint docs do not carry stale milestone facts

Current entrypoint documents SHALL NOT retain stale current-milestone or stale issue-status wording after later milestone evidence supersedes it.

#### Scenario: current milestone docs are checked
- **WHEN** `CLAUDE.md` and `progress.md` are scanned after docs alignment
- **THEN** they do not present M23 as the current active milestone and do not present #343 as the open root cause for live PostGIS MVT after #351/live receipt evidence

#### Scenario: historical implementation plan is checked
- **WHEN** `IMPLEMENTATION_PLAN.md` remains at the repository root
- **THEN** it clearly identifies itself as historical/superseded and points to current entrypoints, or it has been moved to `docs/archived/` with a root pointer

### Requirement: tracked local/agent assets have explicit ownership

Tracked `.agents`, `.codex`, and frontend artifact paths SHALL have an explicit ownership policy consistent with `.gitignore`, `.dockerignore`, and contributor guidance.

#### Scenario: `.agents` is project asset
- **WHEN** `.agents/skills/**` remains tracked
- **THEN** docs stop saying `.agents/` must never be staged, or the guidance is narrowed to local-only subpaths

#### Scenario: `.agents` is local-only
- **WHEN** `.agents/` is declared local-only
- **THEN** it is ignored and removed from tracked project assets through a dedicated cleanup PR

#### Scenario: `.codex` review files are project assets
- **WHEN** `.codex/reviews/**` remains tracked
- **THEN** docs and ignore rules identify which `.codex` paths are project review artifacts and which paths are local cache/evidence

#### Scenario: frontend screenshots are project assets
- **WHEN** `apps/frontend/artifacts/**` remains tracked
- **THEN** docs and ignore rules identify whether those screenshots are canonical visual evidence or generated local artifacts

#### Scenario: tracked asset policy is verified
- **WHEN** the ownership policy changes
- **THEN** `git ls-files`, `.gitignore`, `.dockerignore`, and contributor guidance agree for `.agents`, `.codex`, and frontend artifact paths
