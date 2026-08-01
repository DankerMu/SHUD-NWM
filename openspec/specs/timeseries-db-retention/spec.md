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

### Requirement: The retention gate MUST refuse when the drill's recorded judgment span does not contain the retention drop window

`check_drill_gate` SHALL read the drill receipt's
`salvage_derivation.drop_window` and SHALL refuse fail-closed with wire code
`DRILL_DERIVATION_WINDOW_TOO_NARROW` whenever that recorded window exists
but does not contain (closed-interval; equality passes) the retention drop
window, before any coverage-union evidence is consulted; a receipt without
the `salvage_derivation` section SHALL keep current behavior, a recorded
`drop_window` of null SHALL pass the guard, and a present-but-unusable
`salvage_derivation` shape (not a Mapping, missing `drop_window` key,
unparseable, or inverted window) SHALL refuse with the same code.

#### Scenario: Narrow drill cannot vouch for a wider drop (issue A/B replay)

- **WHEN** completeness subjects A `[06-14, 06-28]` and B `[06-20, 06-27]`
  are both db-export/complete, the drill receipt records
  `salvage_derivation.drop_window = [06-18, 06-19]` and carries only A's
  full-window coverage tuple, and retention judges drop window
  `[06-18, 06-25]`
- **THEN** `check_drill_gate` SHALL return
  `DRILL_DERIVATION_WINDOW_TOO_NARROW` as the first reason (previously:
  empty reasons → PASS → false-positive release)

#### Scenario: No-derivation-section receipt keeps current behavior

- **WHEN** the drill receipt has no `salvage_derivation` section (pre-#1206
  receipts or explicit-manifest drills, which never write the section)
- **THEN** the gate SHALL behave exactly as before this change (the
  cross-subject substitution residual for section-less receipts is pinned
  by a test and recorded in runbook §7.5)

#### Scenario: Un-narrowed, containing, and window-equal drills are not refused

- **WHEN** the recorded `drop_window` is null, contains the retention drop
  window, or is exactly equal to it, with otherwise complete evidence
- **THEN** the gate SHALL NOT emit `DRILL_DERIVATION_WINDOW_TOO_NARROW`
  (a live drill records an equal-or-wider window than the runner's own
  drop window — equality is the tight end of that range; no false
  negatives introduced)

#### Scenario: Unusable derivation shape refuses

- **WHEN** the `salvage_derivation` section is present but unusable — not
  a Mapping, `drop_window` key missing, window unparseable, or inverted
  (`end` before `start`)
- **THEN** the gate SHALL refuse with `DRILL_DERIVATION_WINDOW_TOO_NARROW`

#### Scenario: Wire code syncs across all four registry surfaces

- **WHEN** the new code is added
- **THEN** it SHALL appear in `WIRE_CODES`, the wire-code registry tests,
  runbook §8.2 (table and priority chain), and the
  `tier-node27-timeseries-storage` design fixture #855 block in the same
  commit

#### Scenario: Refusal is visible on the receipt surface

- **WHEN** `run_retention` refuses via this guard
- **THEN** the refused receipt's `refusal_reason` SHALL be
  `DRILL_DERIVATION_WINDOW_TOO_NARROW` and the code SHALL be a member of
  `WIRE_CODES`

