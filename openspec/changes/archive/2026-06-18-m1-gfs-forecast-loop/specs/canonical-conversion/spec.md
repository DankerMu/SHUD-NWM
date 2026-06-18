# Capability Spec: canonical-conversion

## Context

After raw GFS GRIB2 files are acquired, the system MUST convert them into a unified canonical meteorological product format. This conversion standardizes variable names, units, and time axes so that downstream forcing production is source-agnostic. The canonical format uses NetCDF4 with 7 standard variables. Output is stored at `canonical/{source}/{cycle_time}/{variable}/` in S3-compatible object storage. Metadata is written to the `met.canonical_met_product` table (PK `canonical_product_id`, FK `source_id`) with full lineage tracing back to raw GRIB2 files. Each product row includes `grid_id`, `grid_definition_uri`, `source_version`, `native_time_resolution`, and `native_spatial_resolution` metadata. Default `quality_flag` is `'ok'`.

---

## ADDED Requirements

### Requirement: Variable standardization

The canonical converter SHALL map GFS native variable names to the 7 NHMS standard variable names. The mapping MUST be explicit, configurable, and cover all required meteorological variables.

#### Scenario: GFS native names mapped to standard names

- **WHEN** GRIB2 files containing GFS native variables are processed
- **THEN** the following mapping is applied:
  - `tmp2m` → `air_temperature_2m`
  - `apcp` → `prcp_rate_or_amount`
  - `rh2m` → `relative_humidity_2m`
  - `u10m` → `wind_u_10m`
  - `v10m` → `wind_v_10m`
  - `pressfc` → `pressure_surface`
  - `dswrf` → `shortwave_down`
- **THEN** the output NetCDF4 files use only the standard variable names

#### Scenario: Unmapped variable is rejected

- **WHEN** a GRIB2 file contains a variable not in the configured mapping
- **THEN** the variable is skipped (not included in canonical output)
- **THEN** a warning is logged identifying the unmapped variable name

#### Scenario: Missing required variable raises error

- **WHEN** a required variable (one of the 7 standard variables) is absent from the input GRIB2 files for a cycle
- **THEN** the converter raises an error identifying the missing variable
- **THEN** the canonical product for that cycle is marked with `quality_flag='fail'` (default is `'ok'`)

---

### Requirement: Unit conversion

The canonical converter SHALL apply physical unit conversions to produce outputs in standard NHMS units. Conversions MUST be numerically correct and documented.

#### Scenario: Temperature converted from Kelvin to Celsius

- **WHEN** `tmp2m` data in Kelvin is processed
- **THEN** the output `air_temperature_2m` values are in degrees Celsius (degC)
- **THEN** the conversion formula applied is `T_C = T_K - 273.15`
- **THEN** the `unit` field in `met.canonical_met_product` is set to `degC`

#### Scenario: Cumulative precipitation converted to period amount

- **WHEN** `apcp` data representing cumulative precipitation is processed
- **THEN** the output `prcp_rate_or_amount` values represent per-period precipitation amounts in mm (period amount per native time step)
- **THEN** the conversion differentiates consecutive forecast hours to compute period amounts (current step minus previous step)
- **THEN** the `unit` field is set to `mm`
- **THEN** the `native_time_resolution` metadata is set (e.g., `"3h"` for GFS) so downstream consumers know the accumulation period

#### Scenario: Relative humidity converted from percentage to fraction

- **WHEN** `rh2m` data in percentage (0-100) is processed
- **THEN** the output `relative_humidity_2m` values are in fraction (0.0-1.0)
- **THEN** the conversion formula applied is `RH_frac = RH_pct / 100.0`
- **THEN** the `unit` field is set to `0-1`

#### Scenario: Pass-through variables retain correct units

- **WHEN** `u10m`, `v10m`, `pressfc`, and `dswrf` are processed
- **THEN** `wind_u_10m` and `wind_v_10m` retain units of `m/s`
- **THEN** `pressure_surface` retains units of `Pa`
- **THEN** `shortwave_down` retains units of `W/m2`
- **THEN** each `unit` field in `met.canonical_met_product` matches the standard unit

---

### Requirement: Time axis generation

The canonical converter SHALL compute and embed a consistent time axis in every output NetCDF4 file. Each record MUST include `valid_time` (absolute forecast-valid timestamp) and `lead_time_hours` (offset from cycle issuance).

#### Scenario: valid_time computed from cycle_time and forecast_hour

- **WHEN** a GRIB2 record with `cycle_time=2026-05-07T00:00Z` and `forecast_hour=6` is processed
- **THEN** the output `valid_time` is `2026-05-07T06:00Z`
- **THEN** `lead_time_hours` is `6`

#### Scenario: Time axis is monotonically increasing across forecast hours

- **WHEN** multiple forecast hours for a single cycle and variable are converted
- **THEN** the `valid_time` values in the output NetCDF4 are strictly monotonically increasing
- **THEN** `lead_time_hours` values correspond 1:1 with `valid_time` entries

#### Scenario: Time metadata written to database record

- **WHEN** a canonical product is written to the database
- **THEN** the `met.canonical_met_product` row includes `cycle_time`, `valid_time`, and `lead_time_hours`
- **THEN** these values are consistent with the NetCDF4 file contents

---

### Requirement: Lineage tracking

Every canonical product MUST include `lineage_json` that traces the transformation back to the source raw GRIB2 files. The lineage MUST be sufficient to reproduce the canonical product from raw inputs.

#### Scenario: lineage_json references source raw files

- **WHEN** a canonical product is created from raw GRIB2 files
- **THEN** the `lineage_json` field in `met.canonical_met_product` contains:
  - `source_files`: list of raw GRIB2 object URIs used as input
  - `source_cycle_id`: reference to the `met.forecast_cycle` record
  - `conversion_params`: unit conversion applied (e.g., `K_to_C`, `cumulative_to_period`, `pct_to_frac`)
  - `converter_version`: version string of the conversion code

#### Scenario: lineage_json is valid JSON and parseable

- **WHEN** `lineage_json` is read from the database
- **THEN** it parses as valid JSON without errors
- **THEN** it contains all required keys: `source_files`, `source_cycle_id`, `conversion_params`, `converter_version`

#### Scenario: Multi-step lineage for period precipitation

- **WHEN** `prcp_rate_or_amount` is computed by differencing two cumulative `apcp` forecast steps
- **THEN** `lineage_json.source_files` lists both GRIB2 files (current and previous forecast hour)
- **THEN** `lineage_json.conversion_params` documents the differencing operation

---

### Requirement: Output persistence

The canonical converter SHALL write NetCDF4 files to object storage at `canonical/{source}/{cycle_time}/{variable}/` and create corresponding records in `met.canonical_met_product`. All outputs MUST carry a `quality_flag`.

#### Scenario: NetCDF4 file stored at correct path

- **WHEN** the canonical product for `air_temperature_2m` from GFS cycle `2026050700` is created
- **THEN** the NetCDF4 file is stored at `canonical/gfs/2026050700/air_temperature_2m/{filename}.nc`
- **THEN** the file is readable by standard NetCDF4 libraries (e.g., xarray, netCDF4-python)

#### Scenario: Database record created with all required fields

- **WHEN** a canonical product is persisted
- **THEN** a `met.canonical_met_product` row is inserted with fields: `canonical_product_id` (PK), `source_id` (FK), `source_version`, `cycle_time`, `valid_time`, `lead_time_hours`, `variable`, `unit`, `grid_id`, `grid_definition_uri`, `native_time_resolution`, `native_spatial_resolution`, `object_uri`, `checksum`, `quality_flag`, `lineage_json`
- **THEN** `object_uri` matches the actual S3 path of the NetCDF4 file
- **THEN** `checksum` is computed over the stored file content (SHA-256)

#### Scenario: quality_flag set based on conversion result

- **WHEN** all unit conversions and variable mappings succeed for a product
- **THEN** `quality_flag='ok'` (the default)
- **WHEN** a non-critical anomaly is detected (e.g., out-of-range values clamped)
- **THEN** `quality_flag='warn'` and the anomaly is documented in `lineage_json`

---

### Requirement: Idempotent execution

The canonical conversion MUST be idempotent. Re-running conversion for a cycle that has already been processed SHALL NOT create duplicate products or overwrite existing valid outputs.

#### Scenario: Skip conversion when canonical product already exists

- **WHEN** the converter is invoked for `cycle_time=2026050700` and `variable=air_temperature_2m`
- **THEN** if a `met.canonical_met_product` record exists with matching `source_id`, `cycle_time`, `variable`, and `quality_flag='ok'`
- **THEN** the conversion is skipped and status `already_done` is returned

#### Scenario: Re-conversion triggered when quality_flag is fail

- **WHEN** a `met.canonical_met_product` record exists with `quality_flag='fail'`
- **THEN** the converter re-processes that variable for that cycle
- **THEN** the existing record is updated (not duplicated) with the new result

#### Scenario: No duplicate database records after repeated runs

- **WHEN** the converter runs three times for the same cycle
- **THEN** exactly one `met.canonical_met_product` row exists per variable per valid_time
- **THEN** the `checksum` and `object_uri` are consistent across runs
