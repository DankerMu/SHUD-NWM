## ADDED Requirements

### Requirement: The retention drop phase MUST bound lock waits independently of the statement budget

The retention runner SHALL apply a `lock_timeout` inside the same session that
issues `drop_chunks`, set from configuration rather than hard-coded SQL, so a
blocked drop fails fast instead of consuming the whole `statement_timeout`
budget. The configuration parse SHALL fail closed with wire code
`RETENTION_CONFIG_INVALID`, before any database call, unless the value is an
integer strictly greater than `0` and strictly less than the drop-phase
`statement_timeout`. The enumeration and per-chunk measurement paths keep their
existing 60 s statement budget and gain no lock budget.

This bound does not eliminate deadlocks: once the server's deadlock detector
observes a cycle it aborts one side regardless of `lock_timeout`, so a blocked
tick still refuses fail-closed and still relies on the next tick's idempotent
re-entry.

#### Scenario: A blocked drop fails fast rather than exhausting the statement budget

- **WHEN** the drop-phase session is opened for a selected chunk
- **THEN** the session SHALL execute both a `lock_timeout` set to the
  configured value and the existing `statement_timeout`, both before the
  `drop_chunks` call is issued

#### Scenario: An out-of-range lock budget is refused before any database call

- **WHEN** the lock-timeout configuration value is present and non-empty but
  is non-integer, zero, negative, or greater than or equal to the drop-phase
  `statement_timeout`
- **THEN** configuration parsing SHALL raise the fail-closed configuration
  error carrying wire code `RETENTION_CONFIG_INVALID`, and no database
  connection SHALL be attempted

#### Scenario: An absent or empty lock budget takes the pinned default

- **WHEN** the lock-timeout environment variable is not present, or is present
  but empty
- **THEN** the parsed configuration SHALL carry the module's pinned default,
  which SHALL itself satisfy the same strict bound

#### Scenario: The pinned default is tuned from measurement, not from aggregates

- **WHEN** a change to the pinned default is proposed
- **THEN** the default SHALL NOT be lowered on the basis of an aggregate tick
  duration, which cannot attribute elapsed time to a lock acquisition, and
  SHALL be changed only on the basis of the per-chunk elapsed drop diagnostic,
  and the runner SHALL NOT derive it arithmetically from the drop-phase
  `statement_timeout` — the two budgets stay independently operable knobs

### Requirement: Lock-contention drop failures MUST be self-evident in refusal_reason

A drop-phase failure SHALL name lock contention explicitly in its persisted
`refusal_reason` whenever it carries a driver error code that identifies
contention — `55P03` (lock not available) or `40P01` (deadlock detected) — so an
operator can distinguish "blocked by a concurrent writer" from "the delete
itself was slow" without reading the server log. The classification SHALL be
default-deny on the driver error code alone; message-text matching SHALL NOT
be used, and any failure whose code is absent or outside that set SHALL keep
its existing reason text byte-for-byte.

The existing wire-code prefix
`RETENTION_DROP_FAILED:<hypertable_schema>.<chunk_name>:` SHALL remain
byte-unchanged so operator greps keep matching, and the runner SHALL NOT
introduce a new wire code for this classification.

#### Scenario: A lock-not-available failure is attributed to lock contention

- **WHEN** the drop of a selected chunk fails with driver error code `55P03`
- **THEN** `refusal_reason` SHALL retain the unchanged
  `RETENTION_DROP_FAILED:<hypertable_schema>.<chunk_name>:` prefix and SHALL
  additionally carry a lock-contention marker naming `55P03` ahead of the
  redacted driver text

#### Scenario: A deadlock failure is attributed to lock contention

- **WHEN** the drop of a selected chunk fails with driver error code `40P01`
- **THEN** `refusal_reason` SHALL carry a lock-contention marker naming
  `40P01` under the same unchanged prefix

#### Scenario: A non-contention failure keeps its existing text

- **WHEN** the drop fails with a statement timeout (`57014`), with any other
  driver code, or with an exception that exposes no driver code at all
- **THEN** `refusal_reason` SHALL be byte-identical to the pre-change contract
  and SHALL carry no lock-contention marker

### Requirement: The retention unit's failures MUST reach an operator without human polling

The retention systemd unit SHALL declare an `OnFailure=` handler so a
fail-closed tick becomes an outbound alert rather than a journal entry nobody
reads. The handler SHALL reuse the node-27 alert mail channel and recipient
configuration already in production rather than introducing a second channel,
and SHALL NOT be able to turn its own soft failures into an additional failed
unit.

#### Scenario: A refused retention tick raises an alert

- **WHEN** the retention unit exits non-zero
- **THEN** systemd SHALL activate the failure-alert handler for that unit, and
  the handler SHALL deliver a message identifying the failed unit together
  with recent journal context

#### Scenario: Unusable alert-channel configuration degrades quietly

- **WHEN** the failure-alert handler runs on a host where the alert recipient
  or sender is not configured, carries a header-breaking control character, or
  the configured sendmail-compatible transport is unset or not executable
- **THEN** the handler SHALL exit zero after recording the reason, so no
  second failed unit is produced

### Requirement: Each drop attempt MUST emit its own elapsed time

The retention runner SHALL emit a per-chunk diagnostic carrying the chunk's
qualified name and the elapsed time of its `drop_chunks` attempt, on both the
success and the failure path, so an operator can tell a slow delete from a
blocked one and can tune the lock budget from measurement rather than
inference. The diagnostic SHALL go to the operator stderr stream only; the
receipt's `dropped_chunks[]` entry shape SHALL NOT change.

#### Scenario: A successful drop reports its elapsed time

- **WHEN** a selected chunk is dropped successfully
- **THEN** a diagnostic naming that chunk and its elapsed time SHALL be
  emitted on stderr, and the published receipt SHALL carry the same
  `dropped_chunks[]` keys as before this change

#### Scenario: A failed drop still reports its elapsed time

- **WHEN** a selected chunk's drop raises
- **THEN** the per-chunk elapsed diagnostic SHALL still be emitted before the
  tick refuses, and it SHALL carry no error text of its own — the error keeps
  travelling only through the existing redacted `refusal_reason` channel
