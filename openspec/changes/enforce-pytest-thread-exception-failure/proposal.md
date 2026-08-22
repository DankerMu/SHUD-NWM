## Why

Pytest currently reports an uncaught `threading.Thread` exception only as `PytestUnhandledThreadExceptionWarning`, so a test whose body otherwise returns can pass. Issue-owned harness bounds close known hang shapes, but the repository still lacks a shape-independent failure backstop and the issue's proposed timeout dependency is not calibrated to per-test or marker-lane lifecycles.

## What Changes

- Escalate only `pytest.PytestUnhandledThreadExceptionWarning` to an error in repository pytest configuration.
- Add a shipping-config subprocess regression with a source-derived removed-filter mutant and an unrelated-warning control.
- Route `pyproject.toml` and `uv.lock` changes to that policy suite and the selector meta-guard in targeted CI while retaining their existing core-smoke ownership.
- Keep local harness bounds and CI job timeouts; do not add `pytest-timeout` for either a global value or marker-only annotations without per-test marker-lane evidence and an explicit termination/teardown-method decision.
- Update the spin-wait contract so repository-wide escalation is defense in depth, not a replacement for local cause-first capture, cleanup, or whole-process terminability.

## Capabilities

### New Capabilities

- None.

### Modified Capabilities

- `real-integration-test-matrix`: repository-wide pytest handling of unhandled worker exceptions becomes fail-closed, while universal per-test timeout remains deliberately unconfigured and local harness contracts remain mandatory.

## Impact

- Configuration/dependency ownership: `pyproject.toml`, `scripts/select_ci_tests.py`, `tests/test_select_ci_tests.py`, and one new policy test suite; `uv.lock` is audited but remains unchanged because no dependency is added.
- Compatibility wording: one existing test docstring and the spin-wait OpenSpec requirement.
- No production code, API, data/schema, DB, Slurm/SHUD, frontend, deployment, marker expression, CI job timeout, or external-service behavior change.
