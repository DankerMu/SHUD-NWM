## ADDED Requirements

### Requirement: Calibration declaration changes MUST execute their consumer contract suites

A pull request that changes `config/calibration_overrides.yaml` MUST open the backend targeted-test gate and the targeted selector SHALL select `tests/test_publish_scheduler_file_registry.py`, `tests/test_basins_package.py`, and `tests/test_select_ci_tests.py`. The route MUST be exact to that declaration path and SHALL NOT substitute core-smoke or collect-only execution for these assertion-level consumers.

#### Scenario: Declaration-only change reaches publication and package assertions

- **WHEN** the changed-file set contains only `config/calibration_overrides.yaml`
- **THEN** the CI `backend` paths filter matches the change
- **AND** targeted selection contains the publisher, package-manifest, and selector contract suites
- **AND** targeted selection does not fall back to the unrelated core-smoke set or zero-assertion collection

#### Scenario: Backend-filter leg cannot disappear silently

- **WHEN** the declaration path is deleted from the `backend` filter or moved under a different filter
- **THEN** `tests/test_select_ci_tests.py` fails a block-scoped assertion naming the missing backend-gate leg

#### Scenario: Selector leg cannot disappear silently

- **WHEN** the exact selector rule for the declaration is deleted or loses either consumer suite
- **THEN** `tests/test_select_ci_tests.py` fails by naming the missing assertion-level target
