# timeseries-db-retention Specification

## Purpose
TBD - created by archiving change fix-retention-freed-bytes-compressed. Update Purpose after archive.
## Requirements
### Requirement: freed_bytes accounting MUST be compression-aware

The retention receipt's per-chunk `freed_bytes` SHALL report the total bytes
reclaimed by dropping the chunk, including the compressed sibling relation's
bytes when the chunk is compressed, measured BEFORE the drop (H4 ordering
preserved) via a compression-aware size source
(`chunks_detailed_size(<hypertable>)` filtered to the chunk). Per-chunk
measurement failures and empty results SHALL keep the existing best-effort
semantics: record `0` for that chunk, continue measuring the rest, and
never block the drop phase.

#### Scenario: Compressed chunk reports compression-inclusive bytes

- **WHEN** an eligible compressed chunk is measured before drop
- **THEN** the recorded `freed_bytes` SHALL equal the chunk's
  `chunks_detailed_size.total_bytes` (main + compressed sibling + indexes),
  not the main relation's bytes alone

#### Scenario: Measurement failure stays best-effort

- **WHEN** the per-chunk size query raises or returns no row for a chunk
- **THEN** that chunk's `freed_bytes` SHALL be recorded as `0`, the
  remaining chunks SHALL still be measured on fresh connections, and the
  drop phase SHALL proceed unchanged

#### Scenario: Historical receipts are immutable evidence

- **WHEN** the measurement fix lands
- **THEN** previously generated retention receipts (including the
  2026-07-25 first-enforce receipt with the known under-report) SHALL
  remain byte-unchanged, with the discrepancy documented in the receipts
  README rather than rewritten

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

### Requirement: The drop_chunks identity guard MUST have negative-direction regression coverage

The H3 identity guard in the per-chunk drop driver SHALL be pinned by unit
tests that exercise its failure directions against the real function (fake
cursor, no DB): a zero-row `drop_chunks` result (chunk vanished mid-tick),
a mismatched returned identity (server dropped a different chunk), and a
multi-row result containing the selected chunk alongside extras (server
dropped MORE than the selected chunk), each raising the guard's RuntimeError
with the selected chunk's qualified name in the message. Deleting or
weakening the guard SHALL fail the retention test suite — including
cardinality-relaxing weakenings (membership or first-row checks) that would
accept an extra-chunk drop as success.

#### Scenario: Zero-row drop result raises

- **WHEN** `_default_drop_chunk` runs against a cursor whose `fetchall()`
  returns no rows
- **THEN** it SHALL raise RuntimeError matching `expected exact selected
  chunk` and naming the selected chunk's qualified name

#### Scenario: Mismatched dropped identity raises

- **WHEN** the cursor reports a dropped chunk name different from the
  selected chunk's qualified name
- **THEN** it SHALL raise the same RuntimeError shape, preserving the
  diagnostic that names both the returned list and the expected chunk

#### Scenario: Extra chunk dropped alongside the selected chunk raises

- **WHEN** the cursor reports the selected chunk's qualified name plus at
  least one additional dropped chunk name
- **THEN** it SHALL raise the same RuntimeError shape — cardinality binds:
  a superset containing the selected chunk is NOT success

### Requirement: The §8.6 freed_bytes-0 triage procedure MUST select a unique tick bracket and its grep anchor MUST be derived

Runbook §8.6 item 5 SHALL instruct the operator to select the tick bracket
by correlating the receipt's `generated_at` timestamp with the bracket's
`start`/`done` timestamps (both are UTC ISO-8601 from the wrapper's `ts()`),
SHALL state that the shipped env pins the receipt path so the path alone
cannot discriminate ticks, SHALL warn that a `start` line without a matching
`done rc=` line (tick in flight, or wrapper died mid-tick) brackets a tick
that wrote no receipt and must not be read, SHALL record the
refuse-then-retry misread window (prior refused tick's warning vs this
tick's genuine 0) with its conservative direction scoped to that window, and
the test-side §8.6 grep anchor SHALL be derived from the grep token so a
rename campaign cannot leave the fence and the runbook stale but
self-consistent.

#### Scenario: Bracket selection is unique under the shipped env

- **WHEN** an operator follows §8.6 item 5 against a cumulative
  `retention.log` produced with the shipped fixed receipt path
- **THEN** the text SHALL direct them to the bracket whose `start`/`done`
  timestamps contain the receipt's `generated_at`, SHALL state that the
  receipt path is fixed by the shipped env and cannot be used alone as the
  tick key, and SHALL warn that a `start` without a matching `done rc=` (or
  a receipt-less `rc=2` config-refused tick) is not the bracket to read

#### Scenario: Refuse-then-retry window is documented

- **WHEN** a prior tick refused with `RETENTION_DROP_FAILED` after warning
  about a chunk that the current tick genuinely measures as 0 and drops
- **THEN** §8.6 item 5 SHALL state that the in-`dropped_chunks[]` criterion
  does not exclude that stale warning and that within THIS window the
  misread direction is conservative (an extra reconciliation)

#### Scenario: Grep fence derives from the token

- **WHEN** `_MEASURE_WARNING_GREP_TOKEN` is renamed as part of a warning
  rename campaign without touching §8.6's grep command line
- **THEN** `test_measure_warning_byte_identical_with_runbook` SHALL fail,
  because `_MEASURE_WARNING_GREP_FENCE` is an f-string derivation of the
  token rather than an independent literal

### Requirement: The archive gate MUST support an explicit auditable disabled mode while the default stays fail-closed

The retention runner SHALL accept only the explicit `disabled`
archive-gate mode — the archive lane is permanently retired (ADR 0002
Revision 2026-08-11) and the `enabled` mode is retired with it:
`NODE27_TIMESERIES_RETENTION_ARCHIVE_GATE` (after strip and lowercase)
MUST equal `disabled`; when the variable is unset, set to `enabled`, or
set to any other value, the runner SHALL refuse with
`RETENTION_CONFIG_INVALID`, exit 2, no receipt, and diagnostics citing
the ADR revision and the explicit-`disabled` requirement — the unset
default never deletes data. The retired gate machinery (completeness and
drill receipt loaders, both gate adjudications, the two receipt path and
two max-age variables, and the thirteen archive-family wire codes) SHALL
be removed; `WIRE_CODES` SHALL contain no `COMPLETENESS_` or `DRILL_`
prefixed member. The disabled-mode runtime semantics are unchanged
byte-for-byte: candidates partition with `covered_eligible = eligible`
(boundary-partial chunks are drop candidates), enforced receipts record
`salvage_backed_windows` as the empty list, and every receipt carries the
`archive_gate` object with `mode = "disabled"` and the constant
`adr_reference` under receipt schema `1.1`, which this change does not
modify. Historical receipts remain byte-unchanged.

#### Scenario: Explicit disabled is the only accepted mode

- **WHEN** the runner starts with
  `NODE27_TIMESERIES_RETENTION_ARCHIVE_GATE=disabled`
- **THEN** it SHALL run with the same disabled-mode behavior as before
  this change, and the pre-change disabled-BEHAVIOR tests SHALL pass
  unmodified (tests that encode `enabled` as a reachable mode — the
  parse table's enabled rows, CLI-enabled-beats-env, enabled-requires-
  paths, and enabled-parametrized receipt tests — are rewritten to the
  retired semantics and enumerated in the change tasks)

#### Scenario: Disabled enforce deletes without archive receipts and records the authorization

- **WHEN** the mode is `disabled`, enforce is on, and eligible chunks
  exist beyond the retention window
- **THEN** the runner SHALL drop up to the per-tick bound, and the
  enforced receipt SHALL validate against schema `1.1` with
  `archive_gate.mode = "disabled"`, the pinned ADR 0002 Revision
  2026-08-11 `adr_reference`, and `salvage_backed_windows` equal to the
  empty list

#### Scenario: Boundary-partial chunks are drop candidates

- **WHEN** a chunk only partially covered by the retention cutoff window
  boundary is evaluated
- **THEN** it SHALL appear in the candidate set (the completeness-bounds
  deferral is retired with the archive lane)

#### Scenario: Disabled dry-run never drops

- **WHEN** the mode is `disabled` and enforce is off
- **THEN** the receipt SHALL have `outcome = "dry-run"` with candidate
  and deferred lists populated, and no chunk SHALL be dropped

#### Scenario: Runner-own refusals stay reachable and auditable

- **WHEN** a concurrent invocation holds the lock
- **THEN** the tick SHALL refuse with `RETENTION_CONCURRENT_INVOCATION`
  and the refused receipt SHALL carry `archive_gate.mode = "disabled"`
  with the required `adr_reference`

#### Scenario: Operator documentation carries the retirement and cadence pins verbatim

- **WHEN** runbook §8 and the env-file template are read after this
  change
- **THEN** they SHALL present `disabled` as the only mode with the ADR
  anchor text (`docs/adr/0002-node27-timeseries-hot-cold-tiering.md`
  plus `Revision 2026-08-11`) intact, the timer cadence pin
  (`OnCalendar=*-*-* 05:15:00 UTC`) SHALL remain unchanged, and the
  documented rollback SHALL be "set
  `NODE27_TIMESERIES_RETENTION_ENFORCE=0` and/or disable the timer" —
  never "drop the archive-gate env line", which after this change is a
  config-invalid state, not a rollback

#### Scenario: Unset mode refuses without deleting

- **WHEN** the runner starts with the variable unset and no
  `--archive-gate` flag
- **THEN** it SHALL exit 2 with `RETENTION_CONFIG_INVALID` diagnostics
  citing ADR 0002 Revision 2026-08-11, SHALL write no receipt, and SHALL
  drop no chunk

#### Scenario: The retired enabled mode refuses with retirement diagnostics

- **WHEN** the variable (or the CLI flag) requests `enabled`
- **THEN** the runner SHALL exit 2 with `RETENTION_CONFIG_INVALID`
  diagnostics stating the archive lane is permanently retired, SHALL
  write no receipt, and none of the archive-family gate behaviors SHALL
  be reachable

#### Scenario: Archive-family wire codes are gone

- **WHEN** the runner's wire-code set is enumerated after this change
- **THEN** it SHALL contain exactly the runner-own codes and no member
  prefixed `COMPLETENESS_` or `DRILL_`, and the receipt schema `1.1`
  SHALL be byte-unchanged by this change

### Requirement: Resource-governance receipts MUST pin archive_root absence at artifact level

The node-27 resource-governance audit receipt SHALL NOT carry a top-level
`archive_root` block (ADR 0002 revision: the audit must not claim observation
of a volume no lane uses), and this absence SHALL be pinned by a regression
test that constructs the receipt artifact itself, not merely by asserting the
absence of retired collector functions or config attributes.

#### Scenario: constructed receipt carries no archive_root key

WHEN the governance receipt is built via `build_receipt()` with its
filesystem/postgres/systemd collectors stubbed
THEN the resulting receipt dict has no top-level `archive_root` key

#### Scenario: reintroduction by another path fails the pin

WHEN any change reintroduces a top-level `archive_root` block through a
renamed or generic collector path
THEN the artifact-level pin test fails even though the retired-attribute
assertions still pass

### Requirement: Byte-identity guard tests MUST survive openspec change archival

The H4/H6 byte-identity tests that read the tiering change's design.md SHALL
resolve the document from the pending change location first and fall back to
the archived change location, failing with an explicit dual-location message
when neither exists, so that archiving the change cannot turn the guards red.

#### Scenario: pending location preferred

WHEN the design.md exists in both the pending and archived locations
THEN the tests read the pending copy

#### Scenario: archived change still resolvable

WHEN the change has been archived and only
`openspec/changes/archive/<date>-tier-node27-timeseries-storage/design.md` exists
THEN the tests resolve the latest archived copy and keep running their assertions

#### Scenario: both locations missing fails loudly

WHEN neither location exists
THEN the tests fail with a message naming both searched locations instead of a bare FileNotFoundError

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

The emission SHALL be best effort: a failed diagnostic write SHALL NOT change
the tick's outcome. A diagnostic emitted after a completed deletion must never
be able to escape into the uncaught-error path, because the refused receipt
that path publishes cannot record `dropped_chunks` — a chunk that really was
deleted would then be recorded nowhere.

#### Scenario: A successful drop reports its elapsed time

- **WHEN** a selected chunk is dropped successfully
- **THEN** a diagnostic naming that chunk and its elapsed time SHALL be
  emitted on stderr, and the published receipt SHALL carry the same
  `dropped_chunks[]` keys as before this change

#### Scenario: A failed diagnostic write cannot downgrade a completed drop

- **WHEN** the per-chunk diagnostic cannot be written (for example the log
  volume is full) after a chunk has already been dropped successfully
- **THEN** the tick SHALL continue as if the diagnostic had been written, and
  the published receipt SHALL still record that chunk in `dropped_chunks[]`
  rather than refusing with an uncaught error

#### Scenario: A failed drop still reports its elapsed time

- **WHEN** a selected chunk's drop raises
- **THEN** the per-chunk elapsed diagnostic SHALL still be emitted before the
  tick refuses, and it SHALL carry no error text of its own — the error keeps
  travelling only through the existing redacted `refusal_reason` channel

