## 1. Specification

- [x] 1.1 Create OpenSpec change `adapt-cycle-picker-retention-window`.
- [x] 1.2 Document the frontend-only retained-disk miss behavior.

## 2. Implementation

- [x] 2.1 Preserve station-series API error code information in the frontend adapter.
- [x] 2.2 Render a retention-specific empty state for `STATION_FORCING_FILE_NOT_FOUND`.
- [x] 2.3 Keep a selected issue time that is no longer listed as a disabled, labelled first option; do not remember per-session retained-disk misses (owner ruling 2026-10-11, #2881: the "known retained-out option is not selectable again" scenario is dropped).
- [x] 2.4 Keep the station-series API/direct-disk backend behavior unchanged.

## 3. Verification

- [x] 3.1 `cd apps/frontend && corepack pnpm test -- M11StationForcingPopup stationSeries`.
- [x] 3.2 `cd apps/frontend && corepack pnpm build`.
- [x] 3.3 `uv run ruff check apps/api packages tests`.
- [x] 3.4 `openspec validate adapt-cycle-picker-retention-window --strict --no-interactive`.
- [x] 3.5 `openspec validate --all --strict --no-interactive`.
- [x] 3.6 `git diff --check`.
