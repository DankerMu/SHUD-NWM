## Why

A pull request that changes a test suite currently runs that suite and the selector meta-guard, but not other suites that import its top-level helpers. Renaming or removing such a helper can therefore make an importer fail during collection only after merge, while the pull-request lane stays green.

## What Changes

- Derive a one-hop reverse import closure for changed test suites from the repository test tree.
- Select every non-gated suite that imports the changed suite at module scope, while retaining self-selection and selector meta-guards.
- Add mechanically derived and synthetic regression evidence so new top-level suite-import edges cannot silently escape the selector.
- Keep function-local imports, existing redirect rules, support-module routing, and `meta_guard_only` semantics unchanged.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `ci-contract-baseline`: Extend targeted-test selection to cover module-scope suite-to-suite import edges.

## Impact

- `scripts/select_ci_tests.py`: changed-suite selection and repository-local import discovery.
- `tests/test_select_ci_tests.py`: selection, anti-vacuity, exclusion, and GitHub-output regression evidence.
- No workflow shape, dependency, public application API, or production runtime change.
