## MODIFIED Requirements

### Requirement: An `Any` count ratchet SHALL only decrease
`tests/test_any_ratchet.py` SHALL count `Any` occurrences inside function parameter annotations, return annotations and annotated assignments (AST walk; an occurrence is a `Name` `Any` or an `Attribute` whose attr is `Any`; string annotations are parsed with `ast.parse(mode="eval")` first) over `apps/ packages/ services/ workers/ scripts/`, excluding tests, compare to `tests/any_ratchet_ceiling.json` `{"ceiling": <int>}`, fail when the count exceeds the ceiling, and fail when the count is more than 5 % below the ceiling. The initial ceiling SHALL equal the count measured when the test is introduced.

#### Scenario: Added `Any` fails
- **WHEN** a PR adds annotations using `Any` beyond the ceiling
- **THEN** the ratchet test fails with the current count and ceiling

#### Scenario: Cleanup must lower the ceiling
- **WHEN** a PR removes more than 5 % of the ceiling worth of `Any` annotations without editing the ceiling file
- **THEN** the ratchet test fails asking for the ceiling to be lowered
