# Spec Delta

## Purpose
The frontend has a lint gate, `pages/` is a leaf layer, status types come from the generated OpenAPI types, and API failures take one path.

## ADDED Requirements

### Requirement: ESLint SHALL run with exactly three rules and gate CI
`apps/frontend/eslint.config.js` (flat config, using `typescript-eslint`, `eslint-plugin-import-x` and `eslint-import-resolver-typescript` so `@/` aliases resolve) SHALL enable `@typescript-eslint/no-explicit-any` (error), `import-x/no-cycle` (error) and `no-restricted-imports` (error) forbidding imports of `@/pages/**` and relative paths into `src/pages/` from files under `src/stores`, `src/components`, `src/lib`, `src/api` and `src/hooks`; the `no-restricted-imports` block SHALL carry `ignores: ["**/__tests__/**", "**/*.test.*"]` so test files that import page helpers keep passing. `package.json` SHALL define `"lint": "eslint src"`.

#### Scenario: Upward import fails lint
- **WHEN** a non-test file under `src/stores` imports from `@/pages/x`
- **THEN** `pnpm lint` exits non-zero naming `no-restricted-imports`

#### Scenario: Alias cycle fails lint
- **WHEN** two files under `src/lib` import each other through `@/lib/...`
- **THEN** `pnpm lint` exits non-zero naming `import-x/no-cycle`

### Requirement: CI SHALL run the lint and generated-type checks
The `frontend-build` job in `.github/workflows/ci.yml` SHALL run `pnpm check:api-types` after the typecheck steps and, once the lint gate exists, `pnpm lint`.

#### Scenario: Generated types drift fails CI
- **WHEN** `openapi/nhms.v1.yaml` changes without regenerating `src/api/types.ts`
- **THEN** the `frontend-build` job fails at `check:api-types`

### Requirement: `pages/` SHALL be a leaf layer
`src/pages/hydroMet/bootstrap.ts` SHALL move to `src/stores/hydroMetBootstrap.ts` with identical exports; its seven production importers (`stores/hydroMetProductData.ts`, `stores/stationLayerData.ts`, `components/map/M11StationForcingPopup.tsx`, `components/map/M11RiverForecastPanel.tsx`, `pages/OverviewPage.tsx`, `pages/m11/useHydroMetProduct.ts`, `pages/m11/useStationLayer.ts`) SHALL import the new path, and the seven test files that `vi.mock` or import the old path plus `pages/hydroMet/__tests__/bootstrap.test.ts` (which imports it and reads the source by path; 15 files in all) SHALL be edited **only** in their path strings. No non-test file outside `src/pages` and `src/App.tsx` SHALL import from `src/pages`. `import-x/no-cycle` SHALL report zero cycles over `src/`.

#### Scenario: No directory cycle remains
- **WHEN** `pnpm lint` runs on `src/`
- **THEN** `import-x/no-cycle` reports nothing and the vitest suites pass with no edits other than the path strings

### Requirement: Status types SHALL be generated aliases with one failed-status set
`src/lib/constants.ts` SHALL define `PipelineStatus` and `JobStatus` as aliases of the generated `components["schemas"]` types in `src/api/types.ts` and SHALL export one `FAILED_STATUSES` constant; `components/monitoring/BasinFailures.tsx` and `JobsTable.tsx` SHALL import it instead of defining their own sets. `FAILED_STATUSES` keeps its current members; whether `reservation_lost` joins it is out of scope.

#### Scenario: Generated enum change propagates
- **WHEN** a status value is added to the OpenAPI schema and types are regenerated
- **THEN** `pnpm typecheck` surfaces every exhaustive switch over `PipelineStatus` that misses it

### Requirement: API failures SHALL resolve through one normalizer
Every fetch site under `src/` (openapi-fetch `client`, `apiFetch` and raw `fetch` callers) SHALL surface errors through `api/response.ts::getApiErrorMessage`; the two masking wrappers `getSafeModelAssetErrorMessage` and `settledError` SHALL delegate to it (they already do; their masking layer is a redaction boundary and stays), `safeM11ErrorMessage` is an M11 label mask that consumes no error and is not a normalizer, and `apiErrorCode` (which extracts a code, not a message) SHALL parse the shared `ApiErrorShape` envelope from `api/response.ts` instead of its own shape. Of the two silent sites, `useNationalBasinGeo`'s returned `NationalBasinGeoState` gains `error: string | null` (a failed fetch is not cached for the session), and the uncalled loader in `lib/mvtLayerMetadata.ts` that returns `[]` on failure is deleted as a dead export (zero callers). The change does not alter what the UI renders when data is absent; the absent-data assertion is "the dependent map layer is not rendered", in the existing component tests.

#### Scenario: Silent site now records the error
- **WHEN** the national basin geometry request fails with HTTP 500
- **THEN** the hook state exposes a non-empty `error` from `getApiErrorMessage` and the map renders the same absent-data state as before

#### Scenario: Dead loader removed
- **WHEN** `src/` is searched for the former `mvtLayerMetadata` catalog loader
- **THEN** no definition or caller remains
