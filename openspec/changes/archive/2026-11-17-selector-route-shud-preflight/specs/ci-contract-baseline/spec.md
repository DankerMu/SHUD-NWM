## ADDED Requirements

### Requirement: SHUD executable preflight changes MUST select its behaviour suites

A change to `packages/common/shud_preflight.py` MUST select `tests/test_shud_runtime.py` and
`tests/test_production_slurm_validation.py`, the suites that assert the shared preflight's realpath, stub, identity and
inconclusive behaviour, directly or through `check_shud_executable`'s production callers. It MUST also keep its
existing canonicalisation family-guard selection. A selector pin MUST fail when the rule is removed.

#### Scenario: A preflight-only change runs its behaviour suites

- **WHEN** the only changed path is `packages/common/shud_preflight.py`
- **THEN** the targeted selection includes `tests/test_shud_runtime.py`, `tests/test_production_slurm_validation.py`
  and the canonicalisation family guard

#### Scenario: Removing the rule fails the selector pin

- **WHEN** the `packages/common/shud_preflight.py` rule is removed from `scripts/select_ci_tests_rules.py`
- **THEN** the routing pin in `tests/test_select_ci_tests.py` fails
