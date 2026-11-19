## REMOVED Requirements

### Requirement: Disposable TimescaleDB 2.10.2 probes MUST use an explicit node-27 lane

**Reason**: Replaced by "Disposable TimescaleDB 2.10.2 probes MUST run only through an explicit node-27 lane": the selector scenarios of this requirement are cut (#3219), and OpenSpec refuses a MODIFIED block that drops a scenario, so the surviving text moves to a new header.
**Migration**: See the ADDED requirement "Disposable TimescaleDB 2.10.2 probes MUST run only through an explicit node-27 lane"; apart from the selector cuts its body and scenarios are unchanged.

## ADDED Requirements

### Requirement: Disposable TimescaleDB 2.10.2 probes MUST run only through an explicit node-27 lane

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
