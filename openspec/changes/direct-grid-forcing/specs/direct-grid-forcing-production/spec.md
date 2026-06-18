## ADDED Requirements

### Requirement: Direct-grid forcing uses exact canonical grid-cell values
For a direct-grid basin/model asset, forcing production SHALL generate station timeseries by exact lookup of each station's bound canonical grid cell.

#### Scenario: Station value equals bound grid-cell value
- **WHEN** direct-grid forcing production processes a station bound to `grid_cell_id=A`
- **THEN** each scalar forcing variable value for that station is read from canonical grid cell `A`
- **THEN** no IDW neighbor search or weighted spatial interpolation is performed.

#### Scenario: Required grid cells are subset before value extraction
- **WHEN** direct-grid forcing production starts after validation
- **THEN** the producer derives the required `grid_cell_id` set from station bindings
- **THEN** canonical product reads are limited to those required cells where the storage format supports selective access.

#### Scenario: Missing bound grid cell blocks production
- **WHEN** a station binding references a `grid_cell_id` absent from the canonical product grid
- **THEN** forcing production fails with a missing grid-cell error
- **THEN** no forcing package or forcing version is marked ready
- **THEN** IDW weights are not computed as a fallback for that run.

### Requirement: Canonical physical conversions remain mandatory
Direct-grid forcing production SHALL consume canonical products and SHALL NOT write SHUD forcing directly from raw IFS/GFS GRIB values.

#### Scenario: Raw product cannot be used as direct-grid input
- **WHEN** direct-grid forcing production is invoked before required canonical products exist
- **THEN** production fails with missing canonical product details
- **THEN** raw GRIB files are not used as a substitute for canonical values.

#### Scenario: Derived forcing variables use existing canonical semantics
- **WHEN** direct-grid forcing production writes SHUD variables
- **THEN** precipitation, temperature, relative humidity, wind, and radiation use the same units and physical conversion semantics as IDW forcing production
- **THEN** pressure, when available, remains a persisted station timeseries or lineage value and is not emitted as a SHUD station CSV column
- **THEN** only the spatial mapping method differs between `direct_grid` and `idw`.

### Requirement: Direct-grid outputs preserve SHUD forcing package contract
Direct-grid forcing production SHALL emit the same SHUD-readable package shape as existing forcing production.

#### Scenario: SHUD package uses direct-grid station contract
- **WHEN** direct-grid forcing production completes successfully
- **THEN** `.tsd.forc` `ID` column values equal the numeric `shud_forcing_index` values from direct-grid station bindings
- **THEN** `.tsd.forc` coordinates, filenames, and station count match the direct-grid station bindings
- **THEN** per-station CSV files contain the SHUD forcing columns `Precip`, `Temp`, `RH`, `Wind`, and `RN` plus the time axis required by the runtime.

#### Scenario: Direct-grid runtime staging preserves multi-station ownership
- **WHEN** SHUD runtime stages a forcing package whose lineage declares `forcing_mapping_mode="direct_grid"`
- **THEN** runtime uses the standard multi-station SHUD forcing package path
- **THEN** runtime refuses any fallback path that rewrites all `.sp.att` `FORC` values to a single forcing index
- **THEN** staged `.sp.att` `FORC` values are validated against the `.tsd.forc` `ID` column values.

#### Scenario: Timeseries persistence identifies direct-grid method
- **WHEN** direct-grid forcing values are persisted
- **THEN** `met.forcing_station_timeseries` rows are written for every station, variable, and valid time
- **THEN** associated lineage or quality metadata identifies `direct_grid` as the spatial mapping method.

#### Scenario: Direct-grid production is idempotent
- **WHEN** direct-grid forcing production is rerun for the same model/source/cycle and unchanged binding identity
- **THEN** the producer reuses or replaces outputs according to the existing idempotency policy
- **THEN** duplicate ready forcing versions are not created.
