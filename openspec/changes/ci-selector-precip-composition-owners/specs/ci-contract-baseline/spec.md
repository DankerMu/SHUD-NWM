## ADDED Requirements

### Requirement: the precipitation application-composition owners MUST select the precipitation surface oracles

`apps/api/route_registry.py` imports `precip_router` and lists it in `_BUSINESS_ROUTERS`, which `register_role_aware_routes` walks to register every business router; dropping that entry turns both published precipitation endpoints — `/api/v1/precip/{source}/{cycle}/index` and `/api/v1/precip/{source}/{cycle}/{valid_time}.png` — into 404. `apps/api/main.py` calls `_patch_precip_openapi(schema)` inside `_patch_openapi_schema`; dropping that call makes the runtime schema drift from the committed `openapi/nhms.v1.yaml`. Before this change neither owner selected `tests/test_precip_overlay.py` or `tests/test_openapi_drift.py`: the registry selected only the two connection-attribution suites plus the three broad `apps/api/**` suites, and `main.py` only the API error-logging suite plus those same three. Both selections were non-empty and plausible, so the zero-assertion CI warning did not fire and a composition-owner-only diff reached the targeted lane with no precipitation oracle executed. Both owners SHALL therefore reach the precipitation surface oracles, on the terms fixed below.

`scripts/select_ci_tests.py` SHALL route both composition owners to the full set of precipitation surface suites — `tests/test_precip_overlay.py`, `tests/test_openapi_drift.py`, `tests/test_openapi_31_contract.py` and `tests/test_api_contract.py` — while preserving each owner's existing riders: the registry SHALL keep both connection-attribution suites and `main.py` SHALL keep `tests/test_api_errors_logging.py`, and both SHALL keep the three broad `apps/api/**` suites. Because a duplicate pattern splits a module's ownership across two rules, `apps/api/route_registry.py` SHALL be removed from the shared connection-attribution path tuple and given a single path-exact rule whose targets merge both suite sets, exactly as `apps/api/routes/forecast.py` was handled; the comment above that tuple SHALL be corrected so it no longer claims the registry is a member. Neither owner rule SHALL carry `stop_on_match` or `only_when_any_changed`, because the `apps/api/**` rule matches later and its three suites must still accumulate. The selections of the remaining five route paths and three store paths in that tuple, and of `apps/api/routes/precip.py`, `services/precip/cache.py`, `apps/api/openapi_patching.py` and `apps/api/errors.py`, SHALL remain unchanged.

`tests/test_select_ci_tests.py` SHALL pin each owner's selection as an exact set written as literal test-file strings, and SHALL NOT derive the expectation from the production `PRECIP_SURFACE_TESTS` or `CONNECTION_ATTRIBUTION_TESTS` constants, so that an edit to either constant cannot move production and expectation together. It SHALL additionally carry, per owner, a reverse-missing assertion that `tests/test_precip_overlay.py`, `tests/test_openapi_drift.py` and `tests/test_openapi_31_contract.py` disappear when that owner's precipitation targets are removed. The fourth routed suite, `tests/test_api_contract.py`, is deliberately excluded from that assertion: it is also a rider of the broad `apps/api/**` rule, so it survives the removal and an assertion naming all four would fail. The anti-self-certification constraint is that every element of an expected set is a literal string and no value is read back from `scripts/select_ci_tests` — including `PATH_TEST_RULES` and single-suite constants such as `API_ERROR_LOGGING_TEST`; reading `PATH_TEST_RULES` solely to construct the monkeypatched mutant for a reverse-missing assertion is permitted.

#### Scenario: a route-registry diff selects the precipitation oracles and keeps its attribution riders

- **WHEN** the changed paths are exactly `apps/api/route_registry.py`
- **THEN** `select_tests` emits exactly `["tests/test_api.py", "tests/test_api_contract.py", "tests/test_monitoring_api.py", "tests/test_node27_connection_attribution.py", "tests/test_node27_connection_attribution_delegated.py", "tests/test_openapi_31_contract.py", "tests/test_openapi_drift.py", "tests/test_precip_overlay.py"]`

#### Scenario: a main.py diff selects the precipitation oracles and keeps its error-logging rider

- **WHEN** the changed paths are exactly `apps/api/main.py`
- **THEN** `select_tests` emits exactly `["tests/test_api.py", "tests/test_api_contract.py", "tests/test_api_errors_logging.py", "tests/test_monitoring_api.py", "tests/test_openapi_31_contract.py", "tests/test_openapi_drift.py", "tests/test_precip_overlay.py"]`

#### Scenario: the shared attribution path tuple keeps its other members unchanged

- **WHEN** the changed paths are exactly any one of `apps/api/routes/best_available.py`, `apps/api/routes/data_sources.py`, `apps/api/routes/models.py`, `apps/api/routes/pipeline.py` or `apps/api/routes/state_snapshots.py`
- **THEN** the selection still contains both connection-attribution suites and contains none of `tests/test_precip_overlay.py`, `tests/test_openapi_drift.py` or `tests/test_openapi_31_contract.py` (`tests/test_api_contract.py` is excluded because the broad `apps/api/**` rule supplies it to every path under `apps/api/`)

#### Scenario: neither owner rule carries a selection flag

- **WHEN** the `PATH_TEST_RULES` entries for `apps/api/route_registry.py` and `apps/api/main.py` are inspected
- **THEN** each has `stop_on_match` false and `only_when_any_changed` empty

### Requirement: the precipitation composition edges MUST be backed by constructive mutation proofs

A selector edge that pulls a suite into the lane is only justified if that suite actually fails when the edge it guards is broken, so each of the two precipitation composition edges SHALL carry a constructive mutation proof. Both mutations SHALL be exercised inside isolated app fixtures that leave production routing and OpenAPI behavior unchanged: `create_app()` builds a fresh application per call and `monkeypatch` restores the mutated module attribute at test teardown.

#### Scenario: removing precip_router from the business routers reds the published precipitation routes

- **WHEN** an application is built with `main.create_app()` with `route_registry._BUSINESS_ROUTERS` unmodified, and a second one is built with that tuple replaced by the same tuple minus `precip_router`
- **THEN** the first application's route table contains both `/api/v1/precip/{source}/{cycle}/index` and `/api/v1/precip/{source}/{cycle}/{valid_time}.png`, and the second application's route table contains neither. Route-table membership is asserted rather than an HTTP 404, because a 404 on those paths is also produced by the SPA fallback for any unmatched `api/`-prefixed path and by the precipitation routes themselves for a cycle that is not mirrored, so a 404 assertion would stay green under a monkeypatch that never took effect. The module-level singleton `main.app` is out of range of either monkeypatch and SHALL NOT be rebuilt or mutated.

#### Scenario: removing the precipitation OpenAPI patch reds the runtime/static alignment oracle

- **WHEN** an application is built with `main.create_app()` with `main._patch_precip_openapi` unmodified, and a second one is built with it replaced by a no-op
- **THEN** the first application's schema agrees with the committed `openapi/nhms.v1.yaml` at both the `/api/v1/precip/{source}/{cycle}/index` operation and the absence of a `PrecipIndexResponse` component schema, and the second application's schema disagrees at both — the patch pops `PrecipIndexResponse` and rewrites that operation's response, and the static YAML carries no `PrecipIndexResponse`. The two locations are compared individually rather than by whole-document equality, so the assertion does not depend on runtime-mode environment differences between the built application and the committed document.
