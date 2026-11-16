## ADDED Requirements

### Requirement: Old-cycle browsing after cutover renders the retention empty state

After cutover, when a user browses a pre-cutover cycle within the retention window and opens a new (M1 cell) station pin whose station-series disk file does not exist for that old cycle, the popup SHALL render the existing retention empty state (`STATION_FORCING_FILE_NOT_FOUND`, per change `adapt-cycle-picker-retention-window`) and SHALL NOT raise a generic error, draw a chart, or fall back to DB history.

#### Scenario: new pin on old cycle shows the retention empty state

- **WHEN** the cutover has committed **AND** a user selects a pre-cutover cycle within the retention window **AND** opens a new M1 cell-station pin whose station-series disk file is absent for that cycle
- **THEN** the station popup renders the retention-specific empty state for that cycle
- **AND** it does not draw a station forcing chart
- **AND** it does not raise a generic chart failure.

#### Scenario: mismatch is bounded by the retention window

- **WHEN** the popup's issue-time picker is populated and retained-out cycles are marked
- **THEN** the picker offers only catalog-provided cycles — it never synthesizes or persists a pre-cutover option of its own — and the retained-out marking is per-cycle, per-session popup state with no persistence
- **AND** these two frontend-observable properties are what the frontend suite asserts; boundedness itself follows from upstream retention rotation (once rotation stops offering pre-cutover cycles, no cutover-boundary mismatch surface remains — the degradation is time-bounded, not permanent).

#### Scenario: capability is reused, not rebuilt

- **WHEN** the popup handles the new-pin-on-old-cycle miss
- **THEN** it reuses the `adapt-cycle-picker-retention-window` retention empty-state handling
- **AND** it introduces no DB fallback, archive/history read, or synthetic station-series points.

### Requirement: Pre-cutover products and timeseries stay answerable by cycle and model key

The system SHALL keep pre-cutover flow products and station/forcing timeseries queryable for cycles produced before the cutover, keyed by `(cycle, model)`, independent of the `active_flag` flip; historical old-variant assets are immutable.

#### Scenario: pre-cutover flow product still resolves

- **WHEN** a pre-cutover cycle's flow product is requested
- **THEN** it resolves via its `(cycle, model)` key
- **AND** it is unaffected by the `active_flag` flip.

#### Scenario: pre-cutover timeseries by old model still resolves

- **WHEN** a station or forcing timeseries for a pre-cutover cycle is requested with that cycle's old `model_id`
- **THEN** it resolves from the immutable old-variant assets
- **AND** the response is unaffected by the flip, because the old assets are immutable
- **AND** this holds for an M0 legacy station that is `active_flag=false` after the flip: the single-station lookup does not filter `active_flag` (the B4 leak fix in `active-model-dynamic-resolution` desensitizes only the inactive-row disk-miss 404 details and never blocks successful reads).

#### Scenario: flip does not delete or hide historical products

- **WHEN** the cutover flip runs
- **THEN** no pre-cutover product or timeseries record is deleted or made unqueryable
- **AND** only the live station display set changes.
