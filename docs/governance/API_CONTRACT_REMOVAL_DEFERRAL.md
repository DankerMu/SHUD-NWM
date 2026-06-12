# API Contract Removal Deferral

Generated: 2026-06-12

Scope: Governance-5 E3 issue #416 final endpoint removal or explicit deferral
decision for the #411 API contract retirement candidates. This is governance
evidence only. It does not change API runtime code, OpenAPI static paths,
generated frontend types, frontend runtime consumers, route tests, CI workflows,
database migrations, response metadata, or deprecation headers.

## Decision Summary

The #416 removal gate is not satisfied. No runtime endpoint is removed in #416.

Current gate status:

- #412 selected no replacement endpoint and marked no current endpoint
  deprecated or removal-ready.
- #413 closed with no backend removal-candidate migration; active backend tests
  and production validation evidence still cover the retained contracts.
- #414 closed with no frontend migration; display/bootstrap and generated-client
  consumers still call the retained contracts.
- #415 preserved static OpenAPI paths and generated frontend type entries.
- Repository consumers remain, and external consumers are unknown.

Because the required removal evidence is absent, both active runtime candidates
are closed in #416 as explicit deferral and remain active compatibility
contracts.

## Candidate Decisions

| Candidate | #416 decision | Runtime/OpenAPI/type action | Reason |
|---|---|---|---|
| `GET /api/v1/mvp/qhh/latest-product` | Explicit deferral; keep active. | No endpoint removal, OpenAPI contraction, generated type removal, deprecation header, response metadata, or frontend/runtime migration. | The route remains implemented, present in static OpenAPI and generated types, covered by backend contract tests, and called by frontend display/bootstrap code. #412 selected no replacement and external consumers remain unknown. |
| `GET /api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}/forecast-series` | Explicit deferral; keep active. | No endpoint removal, OpenAPI contraction, generated type removal, deprecation header, response metadata, or frontend/runtime migration. | This remains the canonical active forecast-series contract with route, OpenAPI, generated type, backend test, and frontend generated-client consumers. #412 selected no alternate replacement and external consumers remain unknown. |
| Docs-only shorthand forecast-series family: `GET /api/v1/river-segments/{segment_id}/forecast-series`, `GET /api/v1/river-segments/{id}/forecast-series`, and relative `/river-segments/{segment_id}/forecast-series` | Closed as documentation cleanup / historical retention. | No runtime endpoint existed or was removed. No OpenAPI path or generated type entry existed to contract. | #411 found these forms only in documentation. #415 canonicalized active docs or marked the old shorthand as a historical draft example. |

## Search Register Results

The #416 search register from
`openspec/changes/governance-5-e3-api-contract-retirement/tasks.md` was run
before this artifact was written, and is rerun during final verification.
Representative results:

- Route implementations remain in `apps/api/routes/forecast.py`: canonical
  `forecast-series` at line 31 and `latest-product` at line 115.
- Static OpenAPI entries remain in `openapi/nhms.v1.yaml`: canonical
  `forecast-series` path and `operationId: getRiverSegmentForecastSeries` at
  lines 504/506, and `latest-product` path and
  `operationId: getQhhLatestProduct` at lines 696/698.
- Generated frontend type entries remain in `apps/frontend/src/api/types.ts`:
  canonical `forecast-series` path/operation at lines 228/236/2339 and
  `latest-product` path/operation at lines 279/287/2448.
- Frontend generated-client usage remains in `apps/frontend/src`: canonical
  `forecast-series` calls in `stores/forecast.ts` and
  `lib/hydroMet/riverForecast.ts`, plus `latest-product` calls in
  `pages/hydroMet/bootstrap.ts`.
- Backend contract and OpenAPI drift coverage remains in
  `tests/test_api_contract.py` and `tests/test_openapi_drift.py`.
- Deprecation marker search found no `deprecated: true`, `X-Deprecated`, or
  `Sunset` markers in `openapi/nhms.v1.yaml` or
  `apps/frontend/src/api/types.ts`. Governance-doc hits are policy rationale,
  not runtime metadata.
- `git status --short --untracked-files=all` is expected to remain limited to
  #416 docs/OpenSpec/deferral evidence and any pre-existing E3 fixture/task
  edits; no API route implementation, OpenAPI path, generated frontend type,
  frontend runtime, or CI workflow file is changed by #416.

## Future Evidence Required To Reopen Removal

Removal of either active runtime contract requires a new issue or explicit
policy update with all of the following evidence:

- #412 policy update or successor policy that selects a replacement, retirement
  strategy, and external-consumer treatment.
- Repository no-current-consumer proof across route definitions, backend tests,
  services/workers/scripts, static OpenAPI, generated frontend types, frontend
  generated-client usage, frontend tests, E2E/mocked routes, docs, and runbooks.
- External-consumer notice or compatibility treatment; repository-only search
  is not enough to prove a public HTTP contract can be removed.
- OpenAPI contraction and generated frontend type regeneration in the same
  implementation slice as the removal, with active contract checks passing.
- Rollback evidence showing the old route, OpenAPI path, generated type entry,
  tests, and frontend consumers can be restored if the replacement fails.

Until that evidence exists, the rollback baseline is the current compatible
state:

- Keep `/api/v1/mvp/qhh/latest-product` active in route code, OpenAPI,
  generated types, tests, and frontend bootstrap/display consumers.
- Keep
  `/api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}/forecast-series`
  active in route code, OpenAPI, generated types, tests, and frontend
  generated-client consumers.
- Keep docs-only shorthand `forecast-series` forms either removed from active
  contract docs or explicitly marked as historical examples.
