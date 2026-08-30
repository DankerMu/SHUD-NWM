## ADDED Requirements

### Requirement: Disposable TimescaleDB 2.10.2 probes MUST use an explicit node-27 lane

Tests marked `timescaledb_210` SHALL execute only through an explicit node-27
oracle invocation and SHALL NOT infer authorization from `/.dockerenv`, a Docker
socket, or generic Docker availability. The generic GitHub real-database job
SHALL deselect that marker while continuing to execute ordinary `integration`
items. Node-27 execution SHALL use the existing disposable-cluster isolation and
MUST NOT mutate the live `nhms-db` database, port, PGDATA, or production paths.

#### Scenario: Generic Docker CI does not run the isolated probe

- **WHEN** `SQL Migration Dry Run` runs on a Docker-capable GitHub runner with its generic TimescaleDB service and no live `nhms-db` container
- **THEN** ordinary `integration` items execute but every `timescaledb_210` item is deselected, so Docker presence alone cannot start a disposable node-27 probe

#### Scenario: Node-27 explicitly runs the engine oracle

- **WHEN** an operator on node-27 invokes pytest with the dedicated integration environment and `-m timescaledb_210` outside a production window
- **THEN** the separately named disposable PostgreSQL 15.2 / TimescaleDB 2.10.2 cluster runs, live identities are refused, and terminal evidence proves the owned container and work root absent

#### Scenario: Targeted PR selection remains honest

- **WHEN** an owning probe or 2.10.2 contract suite changes on a PR
- **THEN** the targeted unit-test selector still selects its non-gated assertions, because `timescaledb_210` remains registered-but-not-auto-skipped in `tests/conftest.py`

### Requirement: Probe setup failures MUST retain their primary error and truthful cleanup state

A probe failure before disposable-container creation SHALL report the primary
probe status and error before any test evaluates successful-run cleanup proof.
Cleanup SHALL remove or inspect a container only when the current run proved it
created that exact identity. A never-created or conflicting container SHALL NOT
be reported as successfully removed or proven absent merely to satisfy a cleanup
assertion. Successful probe reports SHALL continue to require
`created_container=true`, `container_removed=true`, `container_absent=true`,
`work_root_absent=true`, and `identity_bound=true`.

#### Scenario: Live-image inspect fails before docker run

- **WHEN** the probe cannot inspect the required live image identity before creating its disposable container
- **THEN** the test reports failed status and the original `ProbeError`, cleanup records `created_container=false` without a remove call, and no cleanup assertion masks the setup failure

#### Scenario: Name conflict is not laundered into cleanup success

- **WHEN** `docker run` rejects a same-name pre-existing container before ownership is established
- **THEN** cleanup performs no `docker rm`, reports that the container was not created by this run, and does not claim successful absence proof

#### Scenario: Successful probe still requires complete cleanup proof

- **WHEN** the node-27 disposable probe reaches a passed terminal result
- **THEN** report parsing rejects it unless the created container and work root were identity-bound and are both proven absent after cleanup
