## REMOVED Requirements

### Requirement: Current production ops runbook reflects verified 27-centric topology

**Reason**: Slice 3.6 of `sweep-compute-role-narration` (#3414) takes the banner off
`docs/runbooks/current-production-ops.md`: the page and its `production-ops/` pages now name the
compute role and its hosts themselves, and none of them holds a command addressed to the retired
host. The scenario "node-22 remains compute and Slurm Gateway only" used the codename for the
compute role in its title, and a MODIFIED block cannot retitle a scenario, so the requirement is
removed and added back under a new title.

**Migration**: None for the three scenarios carried over word for word ("stale warning is removed
after rewrite", "node-27 owns active DB ingest and display", "current runbook frames
cross-reference docs safely"). The rule that a `production-ops/` page with a literal ssh command to
`210.77.77.22` carries a pointer to the banner has no page left to apply to. The rule that replaces
it (no command of these pages is a literal `ssh` to the retired host) is a requirement of the change
`sweep-compute-role-narration` and reaches the main spec of `production-topology-contract` when that
change is archived (#3431); until then the static test
`test_production_ops_pages_hold_no_command_to_the_retired_host` of this change holds it.

## ADDED Requirements

### Requirement: Current production ops runbook reflects the verified node-27-centric topology and names the compute hosts

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

#### Scenario: the compute role remains compute and Slurm Gateway only

- **WHEN** the runbook describes the production responsibilities of the
  compute role
- **THEN** it SHALL identify the compute role as compute/Slurm/SHUD plus the
  Slurm Gateway, and SHALL NOT present an API other than the gateway as running
  on a compute host (the diagnostic API unit is retired, #3394)
- **AND** it SHALL NOT instruct operators to use the PostgreSQL `:55433` of the
  retired node-22 host as a current NHMS production database.
- **AND** it SHALL NOT present the scheduler, orchestrator, ingest, or DB
  writer commands of the historical node-22 writer topology as current
  operational actions.
- **AND** any retained historical node-22 writer material SHALL be explicitly
  labeled historical or do-not-connect, not an action path.

#### Scenario: the runbook says which hosts carry the compute role

- **WHEN** the runbook is inspected after the compute role moved to `w3` and
  `w2`
- **THEN** its body, outside any banner or blockquote, SHALL name `w3` (the
  compute control host) and `w2` as the hosts of the compute role since
  2026-11-23
- **AND** the body SHALL state that the host `210.77.77.22` is retired from
  NHMS compute and is a rollback archive only, and that no command of the
  runbook or its `production-ops/` pages is run on it
- **AND** the body SHALL link `docs/runbooks/w2-w3-compute-topology.md`
- **AND** the runbook SHALL NOT carry a banner that tells the reader what
  "node-22" means in it or in its pages: the runbook and its pages name the
  compute role, the compute control host and the compute hosts themselves

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
- **THEN** it SHALL show `/ghdc/data/nwm/...` on the compute hosts and
  `/home/ghdc/nwm/...` on node-27 as the same NFS-backed production data plane
- **AND** it SHALL distinguish complete run/forcing data under `object-store`
  from display products under `published`.
- **AND** it SHALL include on-call checks for the visibility of
  `/ghdc/data/nwm/...` on the compute hosts and of `/home/ghdc/nwm/...` on
  node-27.
