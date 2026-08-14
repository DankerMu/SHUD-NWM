# timeseries-db-retention delta（retention-archive-gate-disabled-mode，#1369）

## ADDED Requirements

### Requirement: The archive gate MUST support an explicit auditable disabled mode while the default stays fail-closed

The retention runner SHALL accept an archive-gate mode from
`NODE27_TIMESERIES_RETENTION_ARCHIVE_GATE` (unset → `enabled`; the
stripped, lowercased value MUST be `enabled` or `disabled`; any other set
value including the empty string SHALL refuse with
`RETENTION_CONFIG_INVALID`, exit 2, no receipt) overridable by a CLI
`--archive-gate` choice argument. In `disabled` mode — authorized by ADR
0002 Revision 2026-08-11 — the runner SHALL skip loading and judging both
archive-side gates (completeness and drill), SHALL treat the two archive
receipt path variables as optional and unread (their max-age variables
keep their existing parse-time validation — fail-closed on malformed
values — but their values have no effect in `disabled` mode), with
exactly two documented semantic consequences at the
completeness object's non-gate consumers: boundary-partial chunks are no
longer bounds-deferred (they become drop candidates — without archive data
the "partially covered" notion itself is gone) and the enforced receipt's
`salvage_backed_windows` SHALL be the empty list. Every other behavior
SHALL be unchanged: lock semantics, watermark reference time, retention
window, per-tick bound, dry-run versus enforce, measure-before-drop
ordering, and fail-closed drop-failure handling. Every receipt (all three
outcome branches) SHALL carry an `archive_gate` object recording the
effective mode, with a constant `adr_reference` citing the ADR revision
required exactly when the mode is `disabled`; the receipt schema version
SHALL bump to `1.1` and historical receipts SHALL remain byte-unchanged.
No new wire code SHALL be added, and in `disabled` mode none of the
thirteen archive-family codes (five `COMPLETENESS_*` plus eight `DRILL_*`)
SHALL be reachable while the runner-own refusal codes remain reachable.

#### Scenario: Unset mode preserves today's fail-closed behavior byte-identically

- **WHEN** the runner starts with `NODE27_TIMESERIES_RETENTION_ARCHIVE_GATE`
  unset and no `--archive-gate` flag
- **THEN** both archive gates SHALL be loaded and judged exactly as before
  this change, the archive receipt path variables SHALL remain required,
  and the pre-change gate test suite SHALL pass unmodified

#### Scenario: Disabled enforce deletes without archive receipts and records the authorization

- **WHEN** the mode is `disabled`, enforce is on, both archive receipt path
  variables are absent, and eligible chunks exist beyond the retention
  window
- **THEN** the runner SHALL drop up to the per-tick bound without touching
  any archive receipt path, and the enforced receipt SHALL validate against
  schema `1.1` with `archive_gate.mode = "disabled"`, `adr_reference` equal
  to the pinned ADR 0002 Revision 2026-08-11 constant, and
  `salvage_backed_windows` equal to the empty list

#### Scenario: Boundary-partial chunks become candidates in disabled mode

- **WHEN** a chunk that enabled mode would move to `deferred_remainder`
  via the completeness-bounds partition is evaluated with the mode
  `disabled`
- **THEN** it SHALL appear in the candidate set (documented semantic
  change, stated in the code comment, runbook §8.5, and receipt-reading
  guidance consistently)

#### Scenario: Disabled dry-run skips gates but never drops

- **WHEN** the mode is `disabled` and enforce is off
- **THEN** the receipt SHALL have `outcome = "dry-run"` with candidate and
  deferred lists populated as today, `archive_gate.mode = "disabled"`, and
  no chunk SHALL be dropped

#### Scenario: Invalid mode value refuses before any action

- **WHEN** the environment variable is set to any value outside the enum
  (e.g. `disable`, `true`, `1`, or the empty string)
- **THEN** the runner SHALL exit 2 with `RETENTION_CONFIG_INVALID`
  diagnostics and SHALL write no receipt

#### Scenario: Runner-own refusals stay reachable and auditable in disabled mode

- **WHEN** a concurrent invocation holds the lock while the mode is
  `disabled`
- **THEN** the tick SHALL refuse with `RETENTION_CONCURRENT_INVOCATION` and
  the refused receipt SHALL also carry `archive_gate.mode = "disabled"`
  with the required `adr_reference`

#### Scenario: Receipt schema rejects unauditable gate records

- **WHEN** a receipt document omits `archive_gate`, or records
  `mode = "disabled"` without `adr_reference`, or records
  `mode = "enabled"` with an `adr_reference`, or carries a non-constant
  `adr_reference` string, or still declares `schema_version = "1.0"`
- **THEN** schema validation SHALL fail for each of those documents

#### Scenario: Operator documentation carries the carve-out verbatim

- **WHEN** runbook §8.4 preconditions and the env-file template are read
  after this change
- **THEN** they SHALL present `disabled` mode as an explicit, ADR-cited
  alternative to the two-receipt preconditions (anchor text
  `docs/adr/0002-node27-timeseries-hot-cold-tiering.md` plus
  `Revision 2026-08-11`), never as an undocumented bypass, and the timer
  cadence pin (`OnCalendar=*-*-* 05:15:00 UTC`) SHALL remain unchanged
