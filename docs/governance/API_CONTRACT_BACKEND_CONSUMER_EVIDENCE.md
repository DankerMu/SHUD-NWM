# API Contract Backend Consumer Evidence

Generated: 2026-06-11

Scope: Governance-5 E3 issue #413 backend/internal evidence for the #411
candidate API contracts under the #412 deprecation policy. This is evidence
only. It does not change backend runtime code, endpoint behavior, OpenAPI,
generated frontend types, frontend/node-27 files, CI, live receipts,
deprecation headers, or response metadata.

## Conclusion

No backend code migration is needed in #413.

The #412 policy marks no current endpoint deprecated or removal-ready.
Backend/internal searches found active route definitions, active store
implementations, active contract tests, and production validation evidence for
the current contracts. They did not find a backend consumer of a #411
removal-candidate endpoint, and they did not find backend runtime code calling
the docs-only shorthand `forecast-series` family as an active API contract.

Active compatibility dependencies remain and must not be removed in #413:

- `GET /api/v1/mvp/qhh/latest-product` remains an active compatibility route.
  Backend tests and production validation evidence still cover its route,
  strict identity behavior, unavailable errors, runtime OpenAPI patch, and
  read-only display route probes.
- `GET /api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}/forecast-series`
  remains the canonical active `forecast-series` route. Backend tests and
  production validation evidence still cover its route, store behavior,
  OpenAPI drift checks, E2E checks, and scale-validation endpoint identity.
- Docs-only shorthand forms remain #415/#416 documentation cleanup or
  historical-retention work. #413 found no backend runtime consumer that must
  be migrated before those docs are synchronized.

## Search Commands

`B1` latest-product backend/internal search:

```bash
rg -n 'latest-product|mvp/qhh|getQhhLatestProduct|QhhLatestProduct|latest_qhh|QHH_LATEST' apps/api packages/common services workers tests scripts docs/governance || true
```

`B2` forecast-series backend/internal search:

```bash
rg -n 'forecast-series|getForecastSeries|get_forecast_series|forecast_series' apps/api packages/common services workers tests scripts docs/governance || true
```

`B3` docs-only shorthand forecast-series backend/runtime search:

```bash
rg -n '/api/v1/river-segments/\{segment_id\}/forecast-series|/api/v1/river-segments/\{id\}/forecast-series|/river-segments/\{segment_id\}/forecast-series|/river-segments/\{id\}/forecast-series' apps/api packages/common services workers tests scripts || true
```

`B4` services/workers/scripts focused search:

```bash
rg -n 'latest-product|mvp/qhh|forecast-series' services workers scripts || true
```

## Findings

### `GET /api/v1/mvp/qhh/latest-product`

Search `B1` found active backend surfaces:

- Route and runtime schema: `apps/api/routes/forecast.py` defines
  `@router.get("/mvp/qhh/latest-product", operation_id="getQhhLatestProduct")`;
  `apps/api/main.py` patches the runtime schema for
  `/api/v1/mvp/qhh/latest-product` and `QhhLatestProduct`.
- Store implementation: `packages/common/forecast_store.py` implements
  `latest_qhh_display_product(...)`, `latest_qhh_product_identity(...)`, and
  the `QHH_LATEST_*` constants.
- Shared model discovery dependency:
  `packages/common/model_registry.py` uses
  `QHH_LATEST_READY_RUN_STATUSES` so basin discovery remains aligned with the
  latest-product candidate query.
- Active contract and store tests: `tests/test_api_contract.py`,
  `tests/test_forecast_api.py`, `tests/test_openapi_drift.py`,
  `tests/test_runtime_mode.py`, `tests/test_readonly_db_validation.py`,
  `tests/test_two_node_e2e_evidence.py`, `tests/test_model_registry_list_basins.py`,
  `tests/test_real_basin_discovery_integration.py`, and related integration
  tests assert current compatibility behavior.
- Production validation evidence: `services/production_closure/readonly_db_validation.py`
  probes `/api/v1/mvp/qhh/latest-product` as a display read route, and
  `services/production_closure/two_node_e2e_evidence.py` recognizes
  `latest-product` path aliases when validating producer evidence.

These are active-contract dependencies, not backend consumers of a
removal-candidate replacement. Under #412, there is no selected replacement for
this route and no #413 backend migration to perform.

### `GET /api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}/forecast-series`

Search `B2` found active backend surfaces:

- Route and store implementation: `apps/api/routes/forecast.py` defines the
  canonical `forecast-series` route and calls
  `packages.common.forecast_store.PsycopgForecastStore.forecast_series(...)`.
- Active contract and integration tests:
  `tests/test_forecast_api.py`, `tests/test_api_contract.py`,
  `tests/test_openapi_drift.py`, `tests/test_e2e.py`, `tests/test_e2e_ifs.py`,
  `tests/test_hindcast.py`, `tests/test_flood_alerts_api.py`,
  `tests/test_real_database_integration.py`, and
  `tests/test_production_e2e_validation.py` cover the canonical route or its
  store behavior.
- Production validation evidence:
  `services/production_closure/e2e_validation.py` builds the canonical
  `/api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}/forecast-series`
  query for deterministic API contract evidence, and
  `services/production_closure/scale_validation.py` records the same canonical
  endpoint for scale validation.

These are active canonical-contract dependencies. #412 explicitly retains this
route and assigns documentation alignment or future deferral to #415/#416, so
`#413` does not migrate these backend tests or services.

### Docs-only shorthand `forecast-series` family

Search `B3` found no backend runtime consumer of the docs-only shorthand
families:

- `GET /api/v1/river-segments/{segment_id}/forecast-series`
- `GET /api/v1/river-segments/{id}/forecast-series`
- relative `/river-segments/{segment_id}/forecast-series`
- relative `/river-segments/{id}/forecast-series`

The only backend/test hits for the templated `forecast-series` suffix were the
canonical route definition, canonical OpenAPI drift assertion, canonical scale
validation endpoint, and an entropy-audit fixture string for the canonical
route shape. Search `B4` likewise found no `workers` or `scripts` consumer of a
candidate path; services hits were production validation evidence for active
contracts.

Because #411 classified these shorthand forms as docs-only and #413 found no
backend runtime consumer, there is no backend code migration for the shorthand
family. Cleanup or historical marking remains #415/#416 work.

## Compatibility Dependencies To Preserve

`#413` intentionally preserves the following backend/test dependencies:

- Active route definitions in `apps/api/routes/forecast.py`.
- Runtime OpenAPI compatibility patching in `apps/api/main.py`.
- Store methods and constants in `packages/common/forecast_store.py`.
- Basin discovery alignment with latest-product ready statuses in
  `packages/common/model_registry.py`.
- Backend contract tests, OpenAPI drift tests, E2E tests, read-only DB
  validation tests, runtime-mode tests, production validation tests, and
  two-node evidence tests that assert current active contracts.
- Production closure validators that generate or verify active-route evidence.

Removing or changing any of these dependencies before #414/#415/#416 would
violate the #412 policy because active runtime routes remain compatible and no
current endpoint is deprecated or removal-ready.

## #413 Disposition

- Backend migration needed: no.
- Backend code changed: no.
- Backend tests added or changed: no, because no backend code migration was
  performed.
- Remaining dependency: active-contract backend tests and production validation
  evidence continue to depend on `latest-product` and canonical
  `forecast-series` compatibility.
- Follow-up owner: #415 owns docs/OpenAPI/type synchronization after migration
  evidence exists; #416 owns removal or explicit deferral, including
  external-consumer treatment.
