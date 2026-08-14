# timeseries-db-retention delta（archive-lane-retirement，#1370）

## MODIFIED Requirements

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

## REMOVED Requirements

### Requirement: The retention gate MUST refuse when the drill's recorded judgment span does not contain the retention drop window

**Reason**: the archive lane and the `enabled` archive-gate mode are
permanently retired (ADR 0002 Revision 2026-08-11, #1370); the drill
receipt and its judgment-span adjudication no longer exist.

### Requirement: The retention gate MUST refuse when the gate-time requirement set contains a salvage-backed window the drill's completeness snapshot never contained

**Reason**: retired with the `enabled` archive-gate mode (#1370); the
completeness snapshot and salvage-backed-window derivation no longer
exist at gate time.

### Requirement: The db-export coverage refusal MUST localize the shortfall window

**Reason**: retired with the `enabled` archive-gate mode (#1370); the
db-export coverage adjudication and its refusal no longer exist.
