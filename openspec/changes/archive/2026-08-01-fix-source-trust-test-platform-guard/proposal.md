# Fix source-trust self-test: unguarded node-22 host path makes macOS pytest permanently red (#1127)

## Why

`tests/test_two_node_docker_source_trust.py:72` hardcodes the node-22 host
path `/scratch/frd_muziyao/nwm-test/source-trust-explicit/docker-security` as
a real on-disk evidence root with no platform guard. On macOS the root
filesystem is read-only, the validator's preflight cannot create `/scratch`,
and `test_source_trust_single_role_report_is_role_scoped_and_explicit_run_bound`
fails 100% of the time (`1 failed, 11 passed` on master) for a reason
unrelated to the contract under test. PR #1126 fixed the two same-root-cause
guards in `tests/test_two_node_docker_runtime.py` (`:3758-3761`, `:4419-4422`)
but missed this orphan — the noise reduction #1106 targeted is only half done,
and the test's real contract (single-role report is role-scoped, bound to the
explicit `evidence_run_id`, and produces no aggregate report) is never
exercised on macOS, so a regression there would be invisible to local sweeps.

## What Changes

- Add the verbatim #1126 skip guard to the one affected test:
  `@pytest.mark.skipif(not os.access("/scratch/frd_muziyao", os.W_OK),
  reason="requires writable /scratch/frd_muziyao (node-22 host contract)")`.
- Add `import pytest` to `tests/test_two_node_docker_source_trust.py` (the
  file currently imports `os` but not `pytest`).
- No other file changes. The production validator
  `scripts/validate_two_node_docker_source_trust.py` (including its
  evidence-root whitelist at `:26,298`) stays byte-identical. The string-only
  whitelist-message assertion at
  `tests/test_two_node_docker_source_trust.py:131` stays unguarded by design
  (it never writes to disk).

## Impact

- Affected specs: `two-node-docker-runtime` (extends the existing
  cross-platform determinism requirement to the source-trust self-test file).
- Affected code: `tests/test_two_node_docker_source_trust.py` only
  (one decorator + one import).
- node-22 and CI behavior unchanged: CI creates a writable
  `/scratch/frd_muziyao` (`.github/workflows/ci.yml:205-209` for
  `unit-test` full, `:236-240` for `unit-test-targeted`), so the guard
  evaluates false there and the test runs exactly as before.

## Design exemption

Fixture level compact: `design.md` exempt — single-test guard with a
prescribed diff, zero production-code surface, direct #1126 precedent.
