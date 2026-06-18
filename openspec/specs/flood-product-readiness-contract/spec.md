# flood-product-readiness-contract Specification

## Purpose
TBD - created by archiving change m8-fourth-review-remediation. Update Purpose after archive.
## Requirements
### Requirement: Flood product APIs accept product-ready terminal states

Flood alert and flood return-period map endpoints SHALL read completed flood products after publication.

#### Scenario: frequency_done run remains readable

WHEN a run has status `frequency_done` and return-period rows exist
THEN `/api/v1/flood-alerts/summary`, `/ranking`, `/segments`, `/timeline`, and `/api/v1/tiles/flood-return-period` MUST return product data.

#### Scenario: published run is readable

WHEN a run has status `published` and return-period rows exist
THEN the same flood alert and flood map endpoints MUST return product data
AND MUST NOT return `FREQUENCY_NOT_COMPUTED`.

#### Scenario: non-ready run is rejected

WHEN a run is not in a product-ready state and return-period computation is incomplete
THEN flood product endpoints MUST return a clear not-ready error envelope.

#### Scenario: non-ready run with stray rows is rejected

WHEN a run is not in a product-ready state but return-period rows exist due to partial or stray writes
THEN flood product endpoints MUST still follow the named ready-status gate
AND MUST NOT expose the product unless the state is intentionally added to the ready set.

### Requirement: Readable state set is named and tested

The backend SHALL define a named set of flood-product-ready statuses.

#### Scenario: Future terminal states are explicit

WHEN developers add or change hydro run terminal states
THEN tests MUST fail unless the flood-product-ready status set is intentionally updated or preserved.

