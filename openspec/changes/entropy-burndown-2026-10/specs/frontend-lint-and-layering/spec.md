# Spec Delta

## Purpose
The frontend has a lint gate, `pages/` is a leaf layer, status types come from the generated OpenAPI types, and API failures take one path.

## ADDED Requirements

### Requirement: ESLint SHALL run with exactly three rules and gate CI
`apps/frontend/eslint.config.js` (flat config) SHALL enable `@typescript-eslint/no-explicit-any` (error), `import/no-cycle` (error) and `no-restricted-imports` (error) forbidding imports of `@/pages/**` and relative paths into `src/pages/` from files under `src/stores`, `src/components`, `src/lib`, `src/api` and `src/hooks`. `package.json` SHALL define `"lint": "eslint src"`. The `frontend-build` job in `.github/workflows/ci.yml` SHALL run `pnpm lint` and `pnpm check:api-types` after the typecheck steps.

#### Scenario: Upward import fails lint
- **WHEN** a file under `src/stores` imports from `@/pages/x`
- **THEN** `pnpm lint` exits non-zero naming `no-restricted-imports`

#### Scenario: Generated types drift fails CI
- **WHEN** `openapi/nhms.v1.yaml` changes without regenerating `src/api/types.ts`
- **THEN** the `frontend-build` job fails at `check:api-types`

### Requirement: `pages/` SHALL be a leaf layer
`src/pages/hydroMet/bootstrap.ts` SHALL move to `src/stores/hydroMetBootstrap.ts` with identical exports; no file outside `src/pages` and `src/App.tsx` SHALL import from `src/pages`. `import/no-cycle` SHALL report zero cycles over `src/`.

#### Scenario: No directory cycle remains
- **WHEN** `pnpm lint` runs on `src/`
- **THEN** `import/no-cycle` reports nothing and the vitest suites pass unchanged

### Requirement: Status types SHALL be generated aliases with one failed-status set
`src/lib/constants.ts` SHALL define `PipelineStatus` and `JobStatus` as aliases of the generated `components["schemas"]` types in `src/api/types.ts` and SHALL export one `FAILED_STATUSES` constant; `components/monitoring/BasinFailures.tsx` and `JobsTable.tsx` SHALL import it instead of defining their own sets.

#### Scenario: Generated enum change propagates
- **WHEN** a status value is added to the OpenAPI schema and types are regenerated
- **THEN** `pnpm typecheck` surfaces every exhaustive switch over `PipelineStatus` that misses it

### Requirement: API failures SHALL resolve through one normalizer
Every fetch site under `src/` (openapi-fetch `client`, `apiFetch` and raw `fetch` callers) SHALL surface errors through `api/response.ts::getApiErrorMessage`; the local normalizers `safeM11ErrorMessage`, `getSafeModelAssetErrorMessage`, `apiErrorCode`, `settledError` SHALL delegate to it or be removed; `pages/m11/useNationalBasinGeo.ts` and `lib/mvtLayerMetadata.ts` SHALL record the normalized error on their store state instead of returning `null`/`[]` silently, with no change to what the UI renders when data is absent. A vitest test SHALL assert that a failing response at each of the two former silent sites yields a non-empty error string on the store.

#### Scenario: Silent site now records the error
- **WHEN** the national basin geometry request fails with HTTP 500
- **THEN** the store exposes a non-empty error message from `getApiErrorMessage` and the map renders the same absent-data state as before
