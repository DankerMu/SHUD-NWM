# API Contract OpenAPI And Type Sync Evidence

Generated: 2026-06-12

Scope: Governance-5 E3 issue #415 synchronization evidence for the #411 API
contract retirement candidates. This is evidence and documentation cleanup only.
It does not change API route implementation, OpenAPI paths, generated frontend
types, frontend runtime consumers, response metadata, deprecation headers, CI, or
live deployment state.

## Conclusion

No OpenAPI contraction or frontend type regeneration is allowed in #415.

Issue #412 selected no replacement endpoint. Issue #413 closed with no backend
removal-candidate migration, and issue #414 closed with no node-27 frontend
removal-candidate migration. Current consumers still use the active runtime
contracts, so #415 preserves the static OpenAPI paths and generated frontend
type entries unchanged.

Retained active contracts:

- `GET /api/v1/mvp/qhh/latest-product` remains an active compatibility route.
  It stays present in `openapi/nhms.v1.yaml` and
  `apps/frontend/src/api/types.ts`.
- `GET /api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}/forecast-series`
  remains the canonical active forecast-series route. It stays present in
  `openapi/nhms.v1.yaml` and `apps/frontend/src/api/types.ts`.

OpenAPI changed in #415: no.

Generated frontend types regenerated in #415: no.

Runtime routes or frontend consumers changed in #415: no.

## Dependency Status

The dependency chain blocks contraction:

- #412 policy: no current endpoint is deprecated or removal-ready, and no
  replacement endpoint is selected.
- #413 backend evidence: no backend code migration was needed; active-contract
  tests and production validation still cover `latest-product` and canonical
  `forecast-series`.
- #414 frontend evidence: no frontend migration was needed; display/bootstrap
  and generated-client consumers remain on the active routes.

Because both implementation slices closed as explicit deferral/no migration,
removing OpenAPI paths, removing generated type entries, marking active paths
deprecated, or adding deprecation metadata would create contract drift rather
than synchronization.

## Retained OpenAPI And Type Entries

Static OpenAPI retained entries:

- `openapi/nhms.v1.yaml`: `/api/v1/mvp/qhh/latest-product`
- `openapi/nhms.v1.yaml`:
  `/api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}/forecast-series`

Generated frontend type retained entries:

- `apps/frontend/src/api/types.ts`: `/api/v1/mvp/qhh/latest-product`
- `apps/frontend/src/api/types.ts`:
  `/api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}/forecast-series`
- `apps/frontend/src/api/types.ts`: `getQhhLatestProduct`
- `apps/frontend/src/api/types.ts`: `getRiverSegmentForecastSeries`

No `deprecated: true`, deprecation response header, response metadata, or
generated deprecation marker was introduced for either active candidate.

## Docs Synchronization

The docs-only shorthand `forecast-series` references found by #411/#412 were
handled as documentation cleanup, not runtime API deprecation:

- `docs/appendices/E_api_openapi_draft.md` is now labeled as a historical v0.2
  OpenAPI draft, and points readers to the canonical active route.
- `docs/modules/13_api_backend_design.md` lists the canonical basin-version
  route.
- `docs/modules/13_api_backend_spec.md` lists the canonical basin-version route.
- `docs/spec/06_frontend_gis_design.md` states that the generated client calls
  the canonical route with `basin_version_id`.

This cleanup does not imply that a shorthand runtime endpoint existed, was
deprecated, or was removed.

## Search Commands

`S1` active OpenAPI path and deprecation marker search:

```bash
rg -n '^  /api/v1/(mvp/qhh/latest-product|basin-versions/\{basin_version_id\}/river-segments/\{segment_id\}/forecast-series):|operationId: getQhhLatestProduct|operationId: getRiverSegmentForecastSeries|deprecated:|Deprecation|deprecation|X-Deprecated|Sunset' openapi/nhms.v1.yaml
```

`S2` generated type active path and deprecation marker search:

```bash
rg -n '"/api/v1/(mvp/qhh/latest-product|basin-versions/\{basin_version_id\}/river-segments/\{segment_id\}/forecast-series)"|getQhhLatestProduct|getRiverSegmentForecastSeries|QhhLatestProduct|deprecated|Deprecation|deprecation' apps/frontend/src/api/types.ts
```

`S3` stale docs-only shorthand cleanup search:

```bash
rg -n '/api/v1/river-segments/\{segment_id\}/forecast-series|/api/v1/river-segments/\{id\}/forecast-series|/river-segments/\{segment_id\}/forecast-series|/river-segments/\{id\}/forecast-series' docs/appendices/E_api_openapi_draft.md docs/modules/13_api_backend_design.md docs/modules/13_api_backend_spec.md docs/spec/06_frontend_gis_design.md docs/governance || true
```

`S4` frontend generated-client active usage search:

```bash
rg -n -U "client\\.GET\\(\\s*['\"]/api/v1/(mvp/qhh/latest-product|basin-versions/\\{basin_version_id\\}/river-segments/\\{segment_id\\}/forecast-series)['\"]" apps/frontend/src
```

This command proves active frontend generated-client usage of both retained
routes, including calls where `client.GET(` and the route string are split
across lines. Representative expected hits include:

- Latest product: `apps/frontend/src/pages/hydroMet/bootstrap.ts:57` and
  `apps/frontend/src/pages/hydroMet/bootstrap.ts:75`.
- Canonical forecast-series:
  `apps/frontend/src/stores/forecast.ts:376`/`:377` and
  `apps/frontend/src/lib/hydroMet/riverForecast.ts:120`/`:121`.

`S5` backend contract test active path search:

```bash
rg -n '/api/v1/mvp/qhh/latest-product|/api/v1/basin-versions/.*/forecast-series|/api/v1/basin-versions/\{basin_version_id\}/river-segments/\{segment_id\}/forecast-series' tests/test_api_contract.py tests/test_openapi_drift.py
```

## Rollback Baseline

If this documentation synchronization creates ambiguity, the rollback baseline
is to restore the active static OpenAPI and generated frontend type entries and
the canonical docs wording:

- Keep `/api/v1/mvp/qhh/latest-product` active in route code, OpenAPI, generated
  types, tests, and frontend consumers.
- Keep
  `/api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}/forecast-series`
  active in route code, OpenAPI, generated types, tests, and frontend consumers.
- Keep docs-only shorthand references either replaced by the canonical route or
  explicitly marked as historical draft examples.

## #416 Guidance

Issue #416 should treat active runtime endpoint removal as explicit deferral
unless later evidence changes the migration state. The current #413/#414/#415
evidence shows no replacement endpoint, no backend/frontend migration, active
OpenAPI and generated type entries retained, and unknown external consumers.

Under this evidence, #416 should not remove endpoints, contract OpenAPI,
regenerate types to delete paths, or mark active routes deprecated. It can close
the active runtime candidates only as explicit deferral, while recording that a
future removal decision would require a new replacement, repository and external
consumer treatment, synchronized OpenAPI/generated types, and passing contract
checks.
