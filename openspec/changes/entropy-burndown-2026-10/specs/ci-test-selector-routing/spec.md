# Spec Delta

## ADDED Requirements

### Requirement: Selector data tables and logic SHALL be separate modules with identical output
`scripts/select_ci_tests.py` SHALL keep `select_tests`, `main`, `_write_github_output` and the globals the tests patch on the facade (`PATH_TEST_RULES`, `SUPPORT_MODULE_TEST_RULES`, the `*_ROOTS` names and any other facade global a test under `tests/test_select_ci_tests.py` patches, inventoried in the PR that adds the sibling rule rows); `PathTestRule` and the `*_TESTS`/`*_PATH`/`*_ROOTS`/`*_MODULES` constants SHALL be defined in `scripts/select_ci_tests_tables.py`; `CHANGED_TEST_FILE_RULES`, `SUPPORT_MODULE_TEST_RULES` and `PATH_TEST_RULES` SHALL be defined in `scripts/select_ci_tests_rules.py`; the import-graph derivation functions and the `_SUITE_IMPORTER_PARSE_STATS` dict they own SHALL move to `scripts/select_ci_tests_imports.py`. The facade binds the table and rule names by import, and `select_tests` reads them as facade globals, so patches on the facade stay effective. A moved function SHALL NOT read a facade-patched global through its own module binding: it receives the value as a parameter from `select_tests`. Before any move, `PATH_TEST_RULES` SHALL gain a rule row for each sibling and `_collection_smoke_required` SHALL match the siblings as it matches the facade. For the fixed changed-path list recorded in tasks.md, `uv run python scripts/select_ci_tests.py` SHALL produce byte-identical `count`, `tests_json`, `meta_guard_only` and `collection_smoke_required` outputs at the parent commit and at the head of each extraction PR (no committed snapshot; the comparison is run on both commits of the same PR). `tests/test_select_ci_tests.py` SHALL pass with only three kinds of edit to existing tests across the whole decomposition (new scenario tests that the tasks add are not edits): the two source-text pins (node-id scan, `select_tests` exec) and every reference to `_SUITE_IMPORTER_PARSE_STATS` (today four lines in two tests: two `setattr` and two reads).

#### Scenario: Routing output unchanged
- **WHEN** the recorded changed-path list is fed to the selector at an extraction PR's parent commit and head
- **THEN** the four GitHub outputs are byte-identical

#### Scenario: Patched facade global still steers a moved function
- **WHEN** a test patches `select_ci_tests.PATH_TEST_RULES` with a single rule
- **THEN** `select_tests` returns the set that rule yields

#### Scenario: Sibling edit selects the meta guard
- **WHEN** `scripts/select_ci_tests_rules.py` is the only changed path
- **THEN** `select_tests` returns the same meta-guard set it returns for `scripts/select_ci_tests.py`
