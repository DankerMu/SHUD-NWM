## REMOVED Requirements

### Requirement: node-27 live MVT facts and display config are synchronized

**Reason**: One scenario renders `infra/compose.display.yml`, which is deleted with the docker/compose lane (#3231), and another asks for compose `read_only` and a read-only mount.

**Migration**: Replaced by "node-27 live MVT facts and the display env template are synchronized", which keeps every scenario that does not need the compose file.

### Requirement: Agent and generated artifact ownership is explicit

**Reason**: The scenario "Docker context is built" is about `.dockerignore`, which is deleted with the only Dockerfile of the tree (#3231).

**Migration**: Replaced by "Agent and generated artifact ownership is explicit in guidance and git ignore rules" with the remaining scenarios unchanged.

## ADDED Requirements

### Requirement: node-27 live MVT facts and the display env template are synchronized

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

#### Scenario: display readonly safety is preserved

- **WHEN** the display env template is updated to enable live MVT
- **THEN** it preserves `NHMS_SERVICE_ROLE=display_readonly`, disabled control mutations, readonly database-role intent, and no new Slurm or control-plane capability

#### Scenario: current milestone docs are checked

- **WHEN** `CLAUDE.md` describes active work
- **THEN** it no longer presents M23 as the current active milestone after M25/M26 and governance work landed

#### Scenario: node-27 checklist references MVT status

- **WHEN** node-27 bringup checklist describes C4/MVT status
- **THEN** it no longer treats #343 as open, cites #351/live receipt for the closed live MVT root cause, keeps station-MVT/#342 separate, and routes bbox/framing popup live-click evidence to #389

### Requirement: Agent and generated artifact ownership is explicit in guidance and git ignore rules

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
