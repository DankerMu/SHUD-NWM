# Spec Delta: timeseries-db-retention

## ADDED Requirements

### Requirement: Operator-facing retention error text MUST be credential-redacted

Every error string the retention runner persists SHALL be credential-redacted:
each operator-facing surface — the receipt file's `refusal_reason` and the
stderr diagnostic stream (wrapper-captured into `retention.log`) — receives
text only through the module's single redaction chokepoint. The
chokepoint SHALL scrub the configured DSN (verbatim and password in both
URL-encoded and driver-decoded forms, rendered as `***`) and the libpq
role-name echo forms `user "<dsn-username>"` AND `role "<dsn-username>"`
(rendered as `user "***"` / `role "***"`), SHALL never raise (any internal
failure — including an unavailable redaction dependency — degrades to a
credential-free placeholder that preserves the wire-code prefix and the
exception type name),
while preserving the wire-code prefix
(`RETENTION_DROP_FAILED:<hypertable_schema>.<chunk_name>:`,
`RETENTION_UNCAUGHT_ERROR:<TypeName>:`) and the exception type name for
diagnosability. The libpq host/port echo is deliberately retained
(diagnosability trade-off). The module SHALL contain no unused redaction
helper whose docstring claims a safety property nothing enforces.

#### Scenario: DSN parse failure never leaks the password

- **WHEN** a driver exception carrying the full conninfo (e.g. psycopg2
  `invalid dsn: ... "postgresql://alice:supersekret@host:5432/db" ...`)
  escapes to the uncaught fallback
- **THEN** the published receipt's bytes and the stderr diagnostic SHALL
  contain neither the password nor the bare DSN username, and
  `refusal_reason` SHALL start with
  `RETENTION_UNCAUGHT_ERROR:<ExceptionTypeName>:`

#### Scenario: libpq auth failure keeps role redacted but diagnosable

- **WHEN** a driver exception carrying
  `password authentication failed for user "<dsn-username>"` or
  `role "<dsn-username>" does not exist` reaches either the drop-phase
  failure path or the uncaught fallback
- **THEN** the persisted reason SHALL render the role echo as `user "***"`
  or `role "***"` respectively, retain the wire-code prefix (including the
  `<hypertable_schema>.<chunk_name>` component on the drop path), and leak
  no credentials on either surface

#### Scenario: Redaction failure cannot destroy the refused receipt

- **WHEN** the redaction chokepoint itself fails (e.g. psycopg2 and thus
  `packages.common.redaction` is unimportable on a driver-less host) while
  the uncaught fallback is composing `refusal_reason`
- **THEN** `main()` SHALL still publish a schema-valid refused receipt whose
  `refusal_reason` starts with `RETENTION_UNCAUGHT_ERROR:<ExceptionTypeName>:`
  followed by a credential-free placeholder, and SHALL still exit non-zero

#### Scenario: Measurement diagnostic redaction is unchanged

- **WHEN** the per-chunk measurement failure diagnostic is emitted
- **THEN** its existing redacted stderr behavior SHALL remain byte-compatible
  with the pre-change contract (same JSON keys, same redaction), with
  existing tests passing unmodified
