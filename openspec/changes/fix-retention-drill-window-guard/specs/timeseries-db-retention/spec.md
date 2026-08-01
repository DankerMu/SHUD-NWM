# Spec Delta: timeseries-db-retention

## ADDED Requirements

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
