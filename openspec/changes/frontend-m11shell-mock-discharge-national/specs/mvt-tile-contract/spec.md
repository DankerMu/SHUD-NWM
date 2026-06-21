## ADDED Requirements

### Requirement: Frontend M11Shell mock fixture mirrors canonical discharge shape
The frontend unit-test mock fixture `m11MvtMetadataByLayer['discharge']` in `apps/frontend/src/pages/__tests__/M11Shell.test.tsx` SHALL reference the national-shape fixture (`dischargeNationalMvtMetadata` — `tile_url_template = "/api/v1/tiles/hydro-national/q_down/{valid_time}/{z}/{x}/{y}.pbf"`, `required_placeholders` without `{run_id}`, `source_refs` absent) and not the legacy single-run fixture (`dischargeMvtMetadata` — `tile_url_template` containing `{run_id}`, `source_refs` keyed by `run_id`). The mock fixture's `min_zoom` SHALL equal the real backend `_NATIONAL_DISCHARGE_METADATA.min_zoom` (currently `3`).

The legacy `dischargeMvtMetadata` constant MAY remain in the file as a deeplink-only test fixture (the single-run `/api/v1/tiles/hydro/{run_id}/...` deeplink route still exists) but MUST NOT be the default-discharge fixture consumed by `m11MvtMetadataByLayer`.

#### Scenario: M11Shell unit-test default-discharge fixture uses national shape
WHEN the frontend M11Shell unit tests reference `m11MvtMetadataByLayer['discharge']`
THEN the resolved metadata MUST have `tile_url_template` containing `/api/v1/tiles/hydro-national/` and NOT containing `{run_id}` placeholder
AND `required_placeholders` MUST NOT contain `'run_id'`
AND `source_refs` MUST NOT contain a `run_id` key
AND `min_zoom` MUST equal the real backend `_NATIONAL_DISCHARGE_METADATA.min_zoom` value (currently `3`)
