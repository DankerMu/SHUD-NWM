## ADDED Requirements

### Requirement: A selector rule naming consumers it cannot derive SHALL carry an independent exact-set anchor

A targeted-selection rule SHALL be pinned by an assertion whose expectation is written independently of the rule
table whenever it names consumer suites the derived importer closure cannot see — which happens when the
consumption is a function-body import that the module-level import scan deliberately excludes. The generic routing test
derives its expectation from the rule itself, so deleting a named consumer shrinks both sides together and the
test stays green while the consumer's suite silently stops running on a helper-only pull request. An anchor that
merely asserts membership is insufficient: it proves a named consumer is still present, not that the rule has
neither dropped nor gained one. The anchor SHALL therefore assert set equality against explicit literals.

#### Scenario: Deleting a function-body consumer from the rule turns the anchor red

- **GIVEN** a selector rule that names a consumer reached only through a function-body import
- **WHEN** that consumer is removed from the rule's target tuple
- **THEN** the independent exact-set anchor fails, while the rule-derived generic routing test stays green

### Requirement: The OpenAPI patch module's rule SHALL reach every suite that can observe its output, and SHALL NOT carry suites that cannot

The targeted-selection rule for the runtime OpenAPI patch module SHALL reach every suite that asserts against the
PATCHED runtime OpenAPI document, and SHALL NOT carry a suite that cannot observe that document. This module's only
production consumer is the application's schema hook, so its only observable output is the runtime OpenAPI document.
A suite is therefore an oracle for it if and only if that suite reads the patched document — by building the
application and calling its schema accessor, or by comparing that schema against the committed snapshot. How public
a capability's routes are, and how thoroughly a suite exercises them against a real client, decide nothing here: a
suite that reads the COMMITTED OpenAPI file rather than the runtime one, or that never reads a schema at all, stays
green under any mutation of this module and would be an assertion that runs without observing.

Both halves are load-bearing and neither may be inferred from route ownership. Membership SHALL be established by
mutation — neutering one patch implementation and recording which suites turn red — never by reasoning from which
capability a suite is named after. Padding the rule with routes-based candidates is the more tempting error,
because each addition looks like more coverage while buying none; the drift comparison already carries the residual
oracle for every implementation whose own capability suite cannot observe the document.

The rule SHALL enumerate its targets as explicit literals and SHALL NOT be widened to a directory or prefix
pattern, because a widened rule cannot be pinned by an exact-set anchor.

#### Scenario: A suite that asserts the runtime document is routed

- **GIVEN** a suite that builds the application and asserts against its generated OpenAPI schema, and that the
  module's rule does not currently name
- **WHEN** the patch implementation that suite observes is replaced by a no-op
- **THEN** that suite fails, so it is an oracle for the module and the rule names it

#### Scenario: A suite that reads the committed snapshot instead is not routed

- **GIVEN** a suite that exercises a patched capability's public routes but loads the committed OpenAPI file rather
  than the runtime document
- **WHEN** the patch implementation for that capability is replaced by a no-op
- **THEN** that suite stays green, so it is not an oracle for the module and the rule does not name it

#### Scenario: The exact-set anchor is the one place the membership is written

- **GIVEN** the rule's target tuple and its exact-set anchor
- **WHEN** a target is removed from the rule
- **THEN** the anchor fails, because its expectation is a literal set rather than a projection of the rule

### Requirement: The cross-PR review-gate memory file SHALL select its structural guard

The CI path filters SHALL name the tracked review-gate memory file, and targeted selection SHALL route it to the
suite that checks its structure. That file records, per issue, whether a prior pull request exhausted the review
round ceiling, and a successor pull request for such an issue requires a human decision. The file's realistic corruption
path is a hand-resolved cross-session merge conflict, and the commits that perform that resolution are typically
post-merge accounting commits touching no source file. The CI path filters SHALL therefore name this file so that
a change touching it alone still starts the backend lane, and targeted selection SHALL route it to the suite that
checks its structure. Without both edges the guard exists but never executes on the very commits that break the file.

#### Scenario: An accounting commit touching only the memory file runs its guard

- **GIVEN** a pull request whose changed files are the review-gate memory file, the review-loop log, and archived
  OpenSpec documents, with no source file among them
- **WHEN** CI computes its path filters and targeted selection
- **THEN** the backend lane starts and the selection contains the memory file's structural guard suite

## MODIFIED Requirements

### Requirement: the precipitation application-composition owners MUST select the precipitation surface oracles

`apps/api/route_registry.py` imports `precip_router` and lists it in `_BUSINESS_ROUTERS`, which `register_role_aware_routes` walks to register every business router; dropping that entry turns both published precipitation endpoints — `/api/v1/precip/{source}/{cycle}/index` and `/api/v1/precip/{source}/{cycle}/{valid_time}.png` — into 404. `apps/api/main.py` calls `_patch_precip_openapi(schema)` inside `_patch_openapi_schema`; dropping that call makes the runtime schema drift from the committed `openapi/nhms.v1.yaml`. Before this change neither owner selected `tests/test_precip_overlay.py` or `tests/test_openapi_drift.py`: the registry selected only the two connection-attribution suites plus the three broad `apps/api/**` suites, and `main.py` only the API error-logging suite plus those same three. Both selections were non-empty and plausible, so the zero-assertion CI warning did not fire and a composition-owner-only diff reached the targeted lane with no precipitation oracle executed. Both owners SHALL therefore reach the precipitation surface oracles, on the terms fixed below.

`scripts/select_ci_tests.py` SHALL route both composition owners to the full set of precipitation surface suites — `tests/test_precip_overlay.py`, `tests/test_openapi_drift.py`, `tests/test_openapi_31_contract.py` and `tests/test_api_contract.py` — while preserving each owner's existing riders: the registry SHALL keep both connection-attribution suites and `main.py` SHALL keep `tests/test_api_errors_logging.py`, and both SHALL keep the three broad `apps/api/**` suites. Because a duplicate pattern splits a module's ownership across two rules, `apps/api/route_registry.py` SHALL be removed from the shared connection-attribution path tuple and given a single path-exact rule whose targets merge both suite sets, exactly as `apps/api/routes/forecast.py` was handled; the comment above that tuple SHALL be corrected so it no longer claims the registry is a member. Neither owner rule SHALL carry `stop_on_match` or `only_when_any_changed`. Both flags are inert for these two entries today: `apps/api/**` is an earlier rule whose three suites have already accumulated by the time these trailing entries are reached, and `only_when_any_changed` is consulted only in the `CHANGED_TEST_FILE_RULES` loop, never for `PATH_TEST_RULES`. The pin is therefore structural rather than behavioural — it keeps a future `stop_on_match` from shadowing a rule appended after these two whose pattern also matches these paths, and keeps a field that would silently do nothing from being added here. The selections of the five route paths remaining in that tuple, of the three store paths in the sibling connection-attribution store tuple, and of `apps/api/routes/precip.py`, `services/precip/cache.py` and `apps/api/errors.py`, SHALL remain unchanged apart from targets contributed by the supplemental tree-scanning routes under the requirement "Tree-scanning invariant suites MUST follow every scanned source root" — every one of those paths lies under a scanned root, so each now additionally carries `tests/test_river_segment_write_surface_scan.py`. No rule owned by THIS requirement SHALL change to add or remove that target. `services/precip/cache.py` additionally carries the `services/precip/**` tree-rule targets `tests/test_node27_mvt_prewarm.py` and `tests/test_node27_raw_retention.py`, which are owned by the requirement "precip service tree changes MUST select the prewarm reader suite", not by this requirement.

`tests/test_select_ci_tests.py` SHALL pin each owner's selection as an exact set written as literal test-file strings, and SHALL NOT derive the expectation from the production `PRECIP_SURFACE_TESTS` or `CONNECTION_ATTRIBUTION_TESTS` constants, so that an edit to either constant cannot move production and expectation together. It SHALL additionally carry, per owner, a reverse-missing assertion that `tests/test_precip_overlay.py`, `tests/test_openapi_drift.py` and `tests/test_openapi_31_contract.py` disappear when that owner's precipitation targets are removed. The fourth routed suite, `tests/test_api_contract.py`, is deliberately excluded from that assertion: it is also a rider of the broad `apps/api/**` rule, so it survives the removal and an assertion naming all four would fail. `tests/test_river_segment_write_surface_scan.py` is excluded from it for the same reason — it is contributed by a supplemental route the owner rules do not own. The anti-self-certification constraint is that every element of an expected set is a literal string and no value is read back from `scripts/select_ci_tests` — including `PATH_TEST_RULES` and single-suite constants such as `API_ERROR_LOGGING_TEST`; reading `PATH_TEST_RULES` solely to construct the monkeypatched mutant for a reverse-missing assertion is permitted.

`apps/api/openapi_patching.py` is deliberately excluded from that unchanged-selection pin, and its selection is owned instead by the requirement "The OpenAPI patch module's rule SHALL reach every suite that can observe its output, and SHALL NOT carry suites that cannot". That module implements the patch for every patched capability, so pinning its selection unchanged while requiring the composition owners to reach the precipitation oracles left the implementation site as the one owner that could be edited without executing what can observe it. Measurement showed the gap is not on the precipitation leg — `tests/test_precip_overlay.py` reads the committed OpenAPI file and stays green under a no-op `_patch_precip_openapi`, and that implementation is already pinned directly by the drift suite — but on the map-tile leg, whose suite asserts the runtime document and was unreached. Nothing else in this requirement changes: the composition owners' own selections, their riders, the flag pin and the reverse-missing assertions all stand as written.

#### Scenario: a route-registry diff selects the precipitation oracles and keeps its attribution riders

- **WHEN** the changed paths are exactly `apps/api/route_registry.py`
- **THEN** `select_tests` emits exactly `["tests/test_api.py", "tests/test_api_contract.py", "tests/test_monitoring_api.py", "tests/test_node27_connection_attribution.py", "tests/test_node27_connection_attribution_delegated.py", "tests/test_openapi_31_contract.py", "tests/test_openapi_drift.py", "tests/test_precip_overlay.py", "tests/test_river_segment_write_surface_scan.py"]`

#### Scenario: a main.py diff selects the precipitation oracles and keeps its error-logging rider

- **WHEN** the changed paths are exactly `apps/api/main.py`
- **THEN** `select_tests` emits exactly `["tests/test_api.py", "tests/test_api_contract.py", "tests/test_api_errors_logging.py", "tests/test_monitoring_api.py", "tests/test_openapi_31_contract.py", "tests/test_openapi_drift.py", "tests/test_precip_overlay.py", "tests/test_river_segment_write_surface_scan.py"]`

#### Scenario: the shared attribution path tuple keeps its other members unchanged

- **WHEN** the changed paths are exactly any one of `apps/api/routes/best_available.py`, `apps/api/routes/data_sources.py`, `apps/api/routes/models.py`, `apps/api/routes/pipeline.py` or `apps/api/routes/state_snapshots.py`
- **THEN** the selection still contains both connection-attribution suites and contains none of `tests/test_precip_overlay.py`, `tests/test_openapi_drift.py` or `tests/test_openapi_31_contract.py` (`tests/test_api_contract.py` is excluded because the broad `apps/api/**` rule supplies it to every path under `apps/api/`)

#### Scenario: neither owner rule carries a selection flag

- **WHEN** the `PATH_TEST_RULES` entries for `apps/api/route_registry.py` and `apps/api/main.py` are inspected
- **THEN** each has `stop_on_match` false and `only_when_any_changed` empty
