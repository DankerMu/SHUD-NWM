# Spec Delta

## ADDED Requirements

### Requirement: Selector data tables and logic SHALL be separate modules with identical output
`scripts/select_ci_tests.py` SHALL keep `select_tests`, `main`, `_write_github_output` and the globals the tests patch by name; `PathTestRule` and the `*_TESTS`/`*_PATH`/`*_ROOTS`/`*_MODULES` constants SHALL move to `scripts/select_ci_tests_tables.py`; `CHANGED_TEST_FILE_RULES`, `SUPPORT_MODULE_TEST_RULES` and `PATH_TEST_RULES` SHALL move to `scripts/select_ci_tests_rules.py`; the import-graph derivation functions SHALL move to `scripts/select_ci_tests_imports.py`. Before any move, `PATH_TEST_RULES` SHALL gain a rule row for each sibling and `_collection_smoke_required` SHALL match the siblings as it matches the facade. For a fixed changed-path list recorded in tasks.md, `uv run python scripts/select_ci_tests.py` SHALL produce byte-identical `count`, `tests_json`, `meta_guard_only` and `collection_smoke_required` outputs before and after each step, and `tests/test_select_ci_tests.py` SHALL pass with only its source-text pins (the node-id scan and the `select_tests` exec) updated to the new file paths.

#### Scenario: Routing output unchanged
- **WHEN** the recorded changed-path list is fed to the selector after the tables move
- **THEN** the four GitHub outputs are byte-identical to the pre-move run

#### Scenario: Sibling edit selects the meta guard
- **WHEN** `scripts/select_ci_tests_rules.py` is the only changed path
- **THEN** `select_tests` returns the same meta-guard set it returns for `scripts/select_ci_tests.py`
