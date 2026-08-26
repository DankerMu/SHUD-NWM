## 1. Multi-entry Declaration Oracles

- [x] 1.1 Generalize the default checked-in declaration test to create every declared basin, apply every declared parameter, and prove the published calibration bytes differ only at declared fields.
- [x] 1.2 Replace the stale singleton assertion with the exact current seven-entry tuple contract while retaining hetianhe measurement anchors and the global `SOIL_ALPHA` absence assertion.
- [x] 1.3 Preserve and execute the independent `CALIBRATION_OVERRIDE_BASIN_NOT_IN_INVENTORY` tests for real and dry-run publication, zero artifacts, and source-tree immutability.

## 2. CI Producer-to-Consumer Routing

- [x] 2.1 Add exact `config/calibration_overrides.yaml` coverage to the CI backend filter.
- [x] 2.2 Add an exact selector rule for the publisher, package-manifest, and selector contract suites without adding core smoke.
- [x] 2.3 Add positive exact-selection and block-scoped mutation tests proving that removal or relocation of either route leg fails loudly.

## 3. Risk-Pack and Regression Evidence

- [x] 3.1 Public entry/config: a declaration-only changed-file input opens backend CI and selects all three contract suites.
- [x] 3.2 Schema/legacy/package: seven tuples, all declared byte changes, original hetianhe reason anchors, and unchanged undeclared values pass.
- [x] 3.3 Error handling: unknown inventory basin still refuses with the stable code and no publish/partial output.
- [x] 3.4 Non-goals: confirm no production publisher, declaration, schema, dependency, node-22/node-27, or public API changes.

## 4. Evidence Floor

- [x] 4.1 `uv run pytest -q tests/test_publish_scheduler_file_registry.py tests/test_basins_package.py`
- [x] 4.2 `uv run pytest -q tests/test_select_ci_tests.py`
- [x] 4.3 `uv run ruff check .`
- [x] 4.4 `openspec validate fix-calibration-override-test-routing --strict --no-interactive`
- [x] 4.5 `uv run pytest -q`
