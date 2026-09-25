## ADDED Requirements

### Requirement: Workload receipts SHALL name the database cluster that produced their samples

Every `nhms-pgdata-workload` receipt SHALL carry a `server` block. The block
SHALL be captured on the same database session that produced the SQL samples,
and SHALL contain `database`, `system_identifier` (a decimal string from
`pg_control_system()` when present; `null` is allowed only for
`evidence_kind=isolated`), `server_version`, `server_addr` and `server_port`. The
address and port MAY be null. The block SHALL NOT contain any credential,
connection string or role password. A `live` receipt SHALL be refused when the
cluster's `system_identifier` cannot be read. The receipt `schema_version` SHALL
be `1.1`. Receipts archived at `1.0` remain historical artifacts and are not
rewritten.

#### Scenario: a live receipt against node-27

- **WHEN** a `live` receipt is produced against the node-27 primary
- **THEN** `server.system_identifier` equals node-27's cluster identifier, and a
  receipt produced against an independently initialised cluster (for example a
  disposable test database) shows a different value. A physical copy of node-27
  keeps the same identifier, and the receipt does not claim to tell them apart.

#### Scenario: the identity is unavailable

- **WHEN** `pg_control_system()` cannot be executed or returns no identifier
  during a `live` run
- **THEN** the run is refused with `SERVER_IDENTITY_MISSING`, and no PASS receipt
  is written.
