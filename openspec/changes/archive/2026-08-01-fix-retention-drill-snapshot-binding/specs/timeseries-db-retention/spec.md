# Spec Delta: timeseries-db-retention

## ADDED Requirements

### Requirement: The retention gate MUST refuse when the gate-time requirement set contains a salvage-backed window the drill's completeness snapshot never contained

`check_drill_gate` SHALL refuse fail-closed with wire code
`DRILL_COMPLETENESS_SNAPSHOT_UNBOUND` whenever the drill receipt's
`salvage_derivation` records `db_export_windows` (the normalized universe
of db-export/complete subject windows in the completeness receipt the
drill consumed) and any salvage-backed target window derived from the
gate-time completeness receipt is not an exact member of that recorded
set, before any coverage-union evidence is consulted for those targets;
a receipt without
the `salvage_derivation` section or without the `db_export_windows` field
SHALL keep current behavior, a recorded set that is present but unusable
in shape SHALL refuse with the same code, and requirement shrink (targets
all members of a larger recorded set) SHALL pass.

#### Scenario: Snapshot drift with a new subject refuses (issue v1/v2 replay)

- **WHEN** the drill consumed completeness v1 containing only subject A
  (db-export/complete, window `[06-01, 06-30]`) and recorded
  `db_export_windows = [[06-01, 06-30]]` with `drop_window: null`, the
  gate-time completeness v2 additionally contains new subject B
  (db-export/complete, window `[06-10, 06-20]`), the drill's coverage
  carries A's full-window db-export tuple, and retention judges drop
  window `[06-05, 06-15]`
- **THEN** `check_drill_gate` SHALL return
  `DRILL_COMPLETENESS_SNAPSHOT_UNBOUND` as the first reason (previously:
  empty reasons → PASS → false-positive release), and the same receipts
  WITHOUT the `db_export_windows` field SHALL still return empty reasons
  (pinned pre-fix oracle and recorded residual)

#### Scenario: Unchanged or shrunk requirement set is not refused, and membership is judged over drop-filtered targets only

- **WHEN** the gate-time completeness receipt yields targets whose
  `{start, end}` pairs are all members of the drill's recorded
  `db_export_windows` (identical snapshot, daily regeneration without new
  db-export subjects, or a subject removed since the drill), with
  otherwise complete evidence and drill age within `drill_max_age_days` —
  including when the gate-time receipt ALSO contains a new
  db-export/complete subject whose window does NOT overlap the drop window
- **THEN** the gate SHALL NOT emit `DRILL_COMPLETENESS_SNAPSHOT_UNBOUND`
  (no false negatives; daily completeness rewrites alone never invalidate
  a drill receipt, and a new subject outside the drop window is not a
  gate-time requirement — design D1/D2)

#### Scenario: One-sided window change and empty recorded universe refuse

- **WHEN** a gate-time target shares its `start` (or its `end`) with a
  recorded window but differs on the other endpoint (e.g. a backfill
  extended subject A from `[06-01, 06-30]` to `[06-01, 07-15]`), or the
  drill recorded `db_export_windows: []` and any target exists
- **THEN** the gate SHALL refuse with `DRILL_COMPLETENESS_SNAPSHOT_UNBOUND`
  (membership is exact on BOTH endpoints; an empty recorded universe has
  no members)

#### Scenario: Unusable recorded set refuses

- **WHEN** `salvage_derivation.db_export_windows` is present but not a
  list, or contains an entry that is not a window object with string
  `start`/`end`
- **THEN** the gate SHALL refuse with `DRILL_COMPLETENESS_SNAPSHOT_UNBOUND`

#### Scenario: Emit-to-gate round trip binds without translation

- **WHEN** a derivation-mode drill receipt built by the real emit path is
  judged by `check_drill_gate` against the very completeness receipt it
  was derived from
- **THEN** the binding check SHALL pass (no refusal from field-name or
  normalization mismatch between emit and gate), and adding a new
  db-export/complete subject overlapping the drop window to that
  completeness receipt SHALL flip the same pair to
  `DRILL_COMPLETENESS_SNAPSHOT_UNBOUND`

#### Scenario: Refusal is visible on the receipt surface and the code registers on all four surfaces

- **WHEN** `run_retention` refuses via this guard
- **THEN** the refused receipt's `refusal_reason` SHALL be
  `DRILL_COMPLETENESS_SNAPSHOT_UNBOUND`, and the code SHALL appear in
  `WIRE_CODES`, the wire-code registry tests, runbook §8.2 (table and
  priority chain), and the `tier-node27-timeseries-storage` design fixture
  #855 block in the same commit
