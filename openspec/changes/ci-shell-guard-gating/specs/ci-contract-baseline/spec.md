## ADDED Requirements

### Requirement: Shell wrapper changes MUST gate their guard suites

The CI change-detection gate SHALL treat tracked `scripts/**/*.sh` files as
backend surface: the `backend` paths-filter matches them, and the targeted
test selector maps each shell wrapper that has committed guard tests to those
guard test files. A `scripts/**/*.sh` path with no explicit mapping falls back
to the core smoke selection instead of an empty selection.

#### Scenario: sh-only change selects the wrapper's guard suite

WHEN a pull request changes only `scripts/scheduler_file_provider_refresh_once.sh`
THEN the `backend` filter reports true
AND the targeted selector output includes `tests/test_scheduler_file_provider_refresh.py`

#### Scenario: unmapped shell script falls back loudly, not empty

WHEN a pull request changes only a new `scripts/**/*.sh` file that has no
selector mapping
THEN the targeted selector returns at least the core smoke test set
AND does not return an empty selection
