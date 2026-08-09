# Spec Delta: timeseries-db-retention

## ADDED Requirements

### Requirement: The db-export coverage refusal MUST localize the shortfall window

The retention gate's db-export leg SHALL, when refusing for per-window
coverage shortfall, emit `refusal_reason` in the form
`DRILL_COVERAGE_DB_EXPORT_MISSING:<clipped_start>/<clipped_end>` — the
first uncovered salvage-backed target in ascending window order, clipped
to the drop window, serialized with the module's canonical UTC `Z`
ISO-8601 rendering — and SHALL, when refusing because an overlapping
db-export
subject derives no salvage-backed window at all, emit
`DRILL_COVERAGE_DB_EXPORT_MISSING:no-derivable-window`; the bare
registered code SHALL remain a strict prefix of every emitted form, the
`WIRE_CODES` registry and receipt schema SHALL be unchanged, and gate
judgment semantics (which inputs refuse) SHALL be byte-identical to the
pre-change behavior.

#### Scenario: Shortfall names the first uncovered clipped window

- **WHEN** multiple salvage-backed windows are derived and the k-th (not
  first, not last) lacks db-export tuple-union coverage after clipping
- **THEN** `refusal_reason` SHALL be exactly the bare code plus `:` plus
  that k-th clipped window as `<start>/<end>`, and the tick SHALL refuse
  on that single shortfall (early return preserved)

#### Scenario: Clipping is rendered, not the raw subject

- **WHEN** the uncovered subject window overruns the drop window on both
  sides
- **THEN** the suffix SHALL carry the clipped intersection bounds, not
  the raw subject bounds

#### Scenario: Empty derivation carries the dedicated suffix

- **WHEN** the completeness receipt has a db-export subject overlapping
  the drop window but no salvage-backed window is derivable (D2
  fail-closed, #1162)
- **THEN** `refusal_reason` SHALL be exactly
  `DRILL_COVERAGE_DB_EXPORT_MISSING:no-derivable-window`

#### Scenario: Inverted clip renders its inverted interval

- **WHEN** a salvage-backed window's intersection with the drop window is
  inverted (corrupt subject, #1162 guard)
- **THEN** the refusal SHALL use the per-window suffix form rendering the
  inverted interval verbatim, and the refusal SHALL remain fail-closed

#### Scenario: Registry and token-walk surfaces are unchanged

- **WHEN** the suffixed forms are emitted
- **THEN** `WIRE_CODES` SHALL still contain exactly the bare code, the
  H6 forward/reverse token-walk suites SHALL pass unmodified, and the
  runbook §8.2 / §7.5 / §8.7 and the pending
  `tier-node27-timeseries-storage` design wire-format entries SHALL
  describe both payload forms (including that the suffix carries the
  CLIPPED window and does not string-match the receipt's unclipped
  entries) in the same commit
