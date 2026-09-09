## ADDED Requirements

### Requirement: precip service tree changes MUST select the prewarm reader suite

`scripts/node27_mvt_prewarm.py` imports `services.precip.mirror.horizon_valid_times` at module level and `tests/test_node27_mvt_prewarm.py` imports that script at module level, so the prewarm suite is a one-hop importer suite of the `services/precip/**` tree whose valid-time grid assertions discriminate on `PRECIP_STEP_HOURS`. The `services/precip/**` `PathTestRule` in `scripts/select_ci_tests.py` SHALL target `tests/test_node27_mvt_prewarm.py` in addition to the four `PRECIP_SURFACE_TESTS` suites, by widening that rule's own target tuple only (neither `stop_on_match` nor `only_when_any_changed`). The shared `PRECIP_SURFACE_TESTS` tuple and the `apps/api/routes/precip.py` rule SHALL remain unchanged, so a route-only diff SHALL NOT select the prewarm suite. `tests/test_select_ci_tests.py` SHALL pin both outcomes with explicit literal expected lists rather than by referencing `PRECIP_SURFACE_TESTS`.

#### Scenario: a precip tree module diff selects the prewarm reader suite

- **WHEN** the changed paths are exactly `services/precip/constants.py` or exactly `services/precip/mirror.py`
- **THEN** `select_tests` emits exactly `["tests/test_api_contract.py", "tests/test_node27_mvt_prewarm.py", "tests/test_openapi_31_contract.py", "tests/test_openapi_drift.py", "tests/test_precip_overlay.py"]`

#### Scenario: a precip route-only diff keeps its existing selection

- **WHEN** the changed paths are exactly `apps/api/routes/precip.py`
- **THEN** `select_tests` emits exactly `["tests/test_api.py", "tests/test_api_contract.py", "tests/test_monitoring_api.py", "tests/test_openapi_31_contract.py", "tests/test_openapi_drift.py", "tests/test_precip_overlay.py"]`
