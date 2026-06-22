## ADDED Requirements

### Requirement: Current production ops runbook reflects verified 27-centric topology

`docs/runbooks/current-production-ops.md` SHALL describe the current physical
production deployment as node-27-centric after live verification, not as the
historical node-22 writer topology.

#### Scenario: stale warning is removed after rewrite

- **WHEN** the current production ops runbook is inspected after this change
- **THEN** it SHALL NOT contain the top `STALE WARNING` banner
- **AND** sections for nodes/services, scheduler/ingest, Slurm Gateway,
  API/display service, and artifact locations SHALL describe the verified
  current topology.

#### Scenario: node-27 owns active DB ingest and display

- **WHEN** the runbook describes current DB, ingest, and display service
  ownership
- **THEN** it SHALL identify node-27 as hosting active PostgreSQL on `:55432`,
  cron-driven `node27_autopipe` ingest, display API on `127.0.0.1:8080`, and
  public entry `https://test.nwm.ac.cn`
- **AND** it SHALL include verification commands that sanitize or avoid secret
  values.
- **AND** those commands SHALL cover local `ss`/process evidence for
  `127.0.0.1:55432` and `127.0.0.1:8080`, cron/autopipe discovery, and public
  `https://test.nwm.ac.cn/health`.

#### Scenario: node-22 remains compute and Slurm Gateway only

- **WHEN** the runbook describes node-22 production responsibilities
- **THEN** it SHALL identify node-22 as compute/Slurm/SHUD plus Slurm Gateway
  and diagnostic API host
- **AND** it SHALL NOT instruct operators to use node-22 PostgreSQL `:55433` as
  a current NHMS production database.
- **AND** it SHALL NOT present node-22 scheduler, orchestrator, ingest, or DB
  writer commands as current operational actions.
- **AND** any retained historical node-22 writer material SHALL be explicitly
  labeled historical or do-not-connect, not an action path.

#### Scenario: current runbook frames cross-reference docs safely

- **WHEN** the runbook links adjacent deployment and role-boundary documents
- **THEN** it SHALL link `docs/governance/ROLE_BOUNDARY.md` as the current
  physical deployment source of truth
- **AND** it SHALL link `docs/runbooks/two-node-deployment-overview.md` as
  preserved role-contract/design-intent background, not as current physical
  topology.

#### Scenario: shared artifact paths use both node perspectives

- **WHEN** the runbook documents object-store, published artifacts, Basins, or
  display input paths
- **THEN** it SHALL show node-22 `/ghdc/data/nwm/...` and node-27
  `/home/ghdc/nwm/...` as the same NFS-backed production data plane
- **AND** it SHALL distinguish complete run/forcing data under `object-store`
  from display products under `published`.
- **AND** it SHALL include on-call checks for node-22 `/ghdc/data/nwm/...` and
  node-27 `/home/ghdc/nwm/...` path visibility.
