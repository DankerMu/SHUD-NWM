# frontend-lint-and-layering

## MODIFIED Requirements

### Requirement: API failures SHALL resolve through one normalizer

Every fetch site under `src/` (openapi-fetch `client`, `apiFetch` and raw `fetch` callers) SHALL
surface errors through `api/response.ts::getApiErrorMessage`; the masking wrapper
`getSafeModelAssetErrorMessage` SHALL delegate to it (it already does; its masking layer is a
redaction boundary and stays), `safeM11ErrorMessage` is an M11 label mask that consumes no error
and is not a normalizer, and `apiErrorCode` (which extracts a code, not a message) SHALL parse the
shared `ApiErrorShape` envelope from `api/response.ts` instead of its own shape. Of the two silent
sites, `useNationalBasinGeo`'s returned `NationalBasinGeoState` gains `error: string | null` (a
failed fetch is not cached for the session), and the uncalled loader in `lib/mvtLayerMetadata.ts`
that returns `[]` on failure is deleted as a dead export (zero callers). The change does not alter
what the UI renders when data is absent; the absent-data assertion is "the dependent map layer is
not rendered", in the existing component tests.

#### Scenario: Silent site now records the error

- **WHEN** the national basin geometry request fails with HTTP 500
- **THEN** the hook state exposes a non-empty `error` from `getApiErrorMessage` and the map renders the same absent-data state as before

#### Scenario: Dead loader removed

- **WHEN** `src/` is searched for the former `mvtLayerMetadata` catalog loader
- **THEN** no definition or caller remains
