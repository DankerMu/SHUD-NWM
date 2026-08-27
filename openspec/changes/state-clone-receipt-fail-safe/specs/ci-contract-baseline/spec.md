## MODIFIED Requirements

### Requirement: Irregular source and package routes MUST select their owned suites

The targeted selector SHALL map every tracked module under `workers/mapping_builder/**` to every tracked `tests/test_mapping_builder_*.py` suite, map `packages/common/state_clone_hook.py` to `tests/test_state_clone_cutover_hook.py`, map `scripts/node22_clone_direct_grid_cutover_states.py` to all four state-clone suites it owns (`tests/test_state_clone_recalibration.py`, `tests/test_state_clone_recalibration_cli.py`, `tests/test_state_clone_recalibration_cli_validation.py`, and `tests/test_state_clone_baseline_cutover_cli.py`), and map `tests/state_clone_recalibration_fixtures.py` to all four direct consumers of that shared fixture (`tests/test_state_clone_recalibration.py`, both recalibration CLI modules, and `tests/test_state_clone_baseline_cutover_cli.py`). Variable package sets SHALL be derived from the tracked tree where the naming/domain is stable, while intentionally irregular file-to-suite names remain explicit.

#### Scenario: Mapping-builder package selects all package suites

- **WHEN** any one of the eight tracked `workers/mapping_builder/*.py` modules changes
- **THEN** the output includes all eight tracked `tests/test_mapping_builder_*.py` suites, and the directory importer-gap guard covers the package

#### Scenario: State-clone hook selects its irregular suite

- **WHEN** a PR changes only `packages/common/state_clone_hook.py`
- **THEN** the output includes `tests/test_state_clone_cutover_hook.py`

#### Scenario: Node-22 clone script selects all four owned state-clone suites

- **WHEN** a PR changes only `scripts/node22_clone_direct_grid_cutover_states.py`
- **THEN** the output includes `tests/test_state_clone_recalibration.py`, `tests/test_state_clone_recalibration_cli.py`, `tests/test_state_clone_recalibration_cli_validation.py`, and `tests/test_state_clone_baseline_cutover_cli.py`

#### Scenario: Recalibration shared fixture selects every direct consumer

- **WHEN** a PR changes only `tests/state_clone_recalibration_fixtures.py`
- **THEN** the output includes `tests/test_state_clone_recalibration.py`, `tests/test_state_clone_recalibration_cli.py`, `tests/test_state_clone_recalibration_cli_validation.py`, and `tests/test_state_clone_baseline_cutover_cli.py`
