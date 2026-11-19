## MODIFIED Requirements

### Requirement: A ceiling test SHALL forbid regrowth
`tests/test_time_helper_consolidation.py` SHALL count functions defined directly in a module body (`ast.Module.body`) whose name matches, excluding the files `packages/common/contracts/time_normalization.py` and `packages/common/source_identity.py` and allowlisted entries, over `apps/ packages/ services/ workers/ scripts/`, compare to `tests/time_helper_ceiling.json`, fail above the ceiling, and fail when the count is below the ceiling by more than 5 % (so each batch lowers the ceiling in its PR). The PR introducing the ceiling SHALL attach the hit list.

#### Scenario: New local helper fails
- **WHEN** a new `_utcnow` is defined in a non-canonical module without lowering another
- **THEN** the ceiling test fails
