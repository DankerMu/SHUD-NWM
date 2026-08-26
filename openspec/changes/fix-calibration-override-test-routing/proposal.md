## Why

`config/calibration_overrides.yaml` grew from one declaration to seven, but two checked-in declaration tests still assumed a singleton and master became deterministically red. The config path also opens no backend CI lane and has no selector route, so the producer change that broke those consumers merged without running either suite.

## What Changes

- Make the default-declaration publication oracle valid for every currently declared basin and parameter while preserving byte-level proof that all undeclared values remain unchanged.
- Pin the checked-in declaration's current seven-entry contract, including the original hetianhe measurement evidence and the global absence of `SOIL_ALPHA` overrides.
- Route `config/calibration_overrides.yaml` through both CI legs: open the backend targeted-test job and select the publisher, package-manifest, and selector contract suites.
- Keep production override validation, fail-closed unknown-basin behavior, and the seven declaration values unchanged.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `ci-contract-baseline`: A calibration declaration change must execute its package/publication consumer assertions and the selector meta-guard in the same pull request.

## Impact

- `.github/workflows/ci.yml`: one exact backend filter entry.
- `scripts/select_ci_tests.py`: one exact config-to-consumer route.
- `tests/test_select_ci_tests.py`: positive and mutation evidence for both route legs.
- `tests/test_publish_scheduler_file_registry.py`: multi-entry checked-in declaration fixtures and assertions.
- No production publisher, declaration, schema, package format, node-22/node-27 operation, or public API change.
