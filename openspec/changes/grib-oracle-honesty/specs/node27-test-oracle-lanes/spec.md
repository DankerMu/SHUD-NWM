## ADDED Requirements

### Requirement: Grib-marked tests decode GRIB2 through ecCodes

Every test carrying `@pytest.mark.grib` SHALL read at least one GRIB2 payload through the converter's cfgrib path and SHALL assert that the netcdf4 fallback was not used. GRIB2 payloads SHALL be encoded at test time with the installed ecCodes and the tests SHALL print its version; no GRIB file is checked in. A test that decodes no GRIB2 SHALL NOT carry the marker.

#### Scenario: IFS bundle
- **WHEN** the IFS e2e tests run in the grib lane
- **THEN** each per-forecast-hour bundle holds the eight shortNames `2t 2d 10u 10v tp sp ssr str`, the converter selects each by shortName through cfgrib, and no fallback record is logged

#### Scenario: Fallback used
- **WHEN** a grib-marked test's payload cannot be decoded by cfgrib and the converter falls back to netcdf4
- **THEN** the test fails

#### Scenario: Pure CI without ecCodes
- **WHEN** pure CI collects the test modules without ecCodes installed
- **THEN** collection succeeds, because the GRIB2 helper imports ecCodes only when a grib test runs
