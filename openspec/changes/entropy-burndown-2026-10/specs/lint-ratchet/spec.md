# Spec Delta

## Purpose
Broad exception handling and untyped `Any` cannot grow without an explicit, reviewable ratchet change.

## ADDED Requirements

### Requirement: ruff SHALL select `B` and `BLE` with a frozen inventory
`pyproject.toml` `[tool.ruff.lint] select` SHALL include `"B"` and `"BLE"`; `[tool.ruff.lint.per-file-ignores]` SHALL list, per file, exactly the `B*`/`BLE001` codes that file violated at the time of the change (generated from `ruff check --select B,BLE --output-format json`); `uv run ruff check .` SHALL pass on the resulting tree. Files not listed SHALL be fully subject to the rules.

#### Scenario: New broad except in a clean file fails lint
- **WHEN** a file with no `per-file-ignores` entry gains `except Exception:` that does not re-raise
- **THEN** `uv run ruff check .` reports `BLE001` and exits non-zero

#### Scenario: Frozen file still passes
- **WHEN** a file listed with `BLE001` keeps its existing broad excepts
- **THEN** `uv run ruff check .` passes

### Requirement: An `Any` count ratchet SHALL only decrease
`tests/test_any_ratchet.py` SHALL count `Any` occurrences inside function parameter annotations, return annotations and annotated assignments (AST walk; an occurrence is a `Name` `Any` or an `Attribute` whose attr is `Any`; string annotations are parsed with `ast.parse(mode="eval")` first) over `apps/ packages/ services/ workers/ scripts/`, excluding tests, compare to `tests/any_ratchet_ceiling.json` `{"ceiling": <int>}`, fail when the count exceeds the ceiling, and fail when the count is more than 5 % below the ceiling. The initial ceiling SHALL equal the count measured when the test is introduced. The test SHALL be routed by `scripts/select_ci_tests.py` for any change under `apps/ packages/ services/ workers/ scripts/` through an additive loop in `select_tests` in the #1656/#2185 shape (not a `PATH_TEST_RULES` row, which a `stop_on_match` owner would shadow).

#### Scenario: Added `Any` fails
- **WHEN** a PR adds annotations using `Any` beyond the ceiling
- **THEN** the ratchet test fails with the current count and ceiling

#### Scenario: Cleanup must lower the ceiling
- **WHEN** a PR removes more than 5 % of the ceiling worth of `Any` annotations without editing the ceiling file
- **THEN** the ratchet test fails asking for the ceiling to be lowered
