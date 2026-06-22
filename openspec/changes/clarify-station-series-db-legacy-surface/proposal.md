## Why

The public station-series route now reads retained SHUD forcing CSVs directly
from the object-store disk layout. The old DB-backed
`PsycopgForecastStore.station_series()` helper still exists and still has unit
coverage, but its role is ambiguous: production callers should not silently
fall back to it. Long-term historical access is governed by ADR 0001 and needs
a future explicit archive/history API change before it becomes product behavior.

## What Changes

- Declare `PsycopgForecastStore.station_series()` as a legacy/internal DB helper.
- Add a production-call-site guard so the public display route cannot drift back
  to the legacy DB helper unnoticed.
- Document the helper's retained status and the absence of a DB fallback in the
  object-store station-series runbook.

## Capabilities

### New Capabilities

- None.

### Modified Capabilities

- `met-station-series-api`: clarify the boundary between the current public
  disk-backed route and the legacy DB-backed helper.

## Impact

- `packages/common/forecast_store.py`
- `tests/test_forecast_api_met_station_series.py`
- `docs/runbooks/object-store-forcing-series-read.md`
- `openspec/specs/met-station-series-api/spec.md`
