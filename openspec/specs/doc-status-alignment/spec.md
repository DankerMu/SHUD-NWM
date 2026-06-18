# doc-status-alignment Specification

## Purpose
TBD - created by archiving change governance-3-doc-status-alignment. Update Purpose after archive.
## Requirements
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

### Requirement: Bugs are triaged as a governance ledger

`docs/bugs.md` SHALL record governed bugs with consistent status, owner area,
evidence, and retest commands so historical defects no longer read as an
undifferentiated open backlog.

#### Scenario: required historical bug is evaluated

- **WHEN** a reader evaluates BUG-20260527-003 or BUG-20260527-007 through BUG-20260527-013
- **THEN** the bug entry includes `status`, `owner_area`, `evidence`, and `retest_command`, plus `resolved_by` or `superseded_by` when its status is `resolved` or `superseded`

#### Scenario: old bug is resolved or superseded

- **WHEN** later milestones, PRs, tests, runbooks, or source contracts resolve or supersede an old bug
- **THEN** the bug entry is marked `resolved` or `superseded` with `resolved_by` or `superseded_by` evidence

#### Scenario: bug remains open

- **WHEN** a bug remains open
- **THEN** it has an owner area among `compute_control`, `display_readonly`, `slurm_gateway`, or `shared_contract`, plus a GitHub issue link when one exists or a concrete retest command when no issue exists

### Requirement: Agent and generated artifact ownership is explicit

Repository governance documentation and ignore rules SHALL distinguish reviewed
project assets from local/generated agent and evidence outputs for `.agents`,
`.codex`, `apps/frontend/artifacts`, and root `artifacts/`.

#### Scenario: contributor checks agent assets

- **WHEN** a contributor sees tracked `.agents/skills/**`
- **THEN** governance guidance and ignore rules identify reviewed project skills as project assets while unpromoted local installed or scratch skill additions remain local/generated unless explicitly promoted by PR

#### Scenario: contributor generates workflow evidence

- **WHEN** a contributor creates new `.codex/reviews/**`, `.codex/evidence/**`, or root `artifacts/**` files
- **THEN** ignore rules and guidance classify those files as local/generated evidence by default and prevent accidental staging

#### Scenario: historical evidence remains tracked

- **WHEN** existing tracked `.codex/reviews/**` fixtures or `apps/frontend/artifacts/m11-*.png` files are inspected
- **THEN** they remain tracked historical project evidence, while future generated review outputs or frontend visual artifacts are ignored unless a later issue explicitly promotes them

#### Scenario: Docker context is built

- **WHEN** Docker build context is prepared from the repository
- **THEN** non-runtime agent/evidence directories such as `.agents`, `.codex`, and frontend visual artifacts are excluded from the context

