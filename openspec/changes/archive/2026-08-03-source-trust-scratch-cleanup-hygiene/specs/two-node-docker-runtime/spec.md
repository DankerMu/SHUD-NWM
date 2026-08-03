# two-node-docker-runtime — delta for source-trust-scratch-cleanup-hygiene (#1209)

## ADDED Requirements

### Requirement: Scratch-writing host-contract tests clean up on failure paths without masking the failure

Two named host-contract tests SHALL clean up the real `/scratch`
evidence they create on failure paths as well as success:
`test_source_trust_single_role_report_is_role_scoped_and_explicit_run_bound`
(`tests/test_two_node_docker_source_trust.py`) and
`test_static_report_explicit_evidence_run_id_overrides_scratch_path_inference`
(`tests/test_two_node_docker_runtime.py`) run their action and
assertions inside `try:` with cleanup in `finally:`, so an assertion
failure (contract regression or host drift) cannot strand debris on
the shared node that is indistinguishable from legitimate preflight
output. Their cleanup SHALL be non-raising best-effort
(`shutil.rmtree(..., ignore_errors=True)`; a suppressed
`unlink(missing_ok=True)`) so the `finally` can never replace the
original failure signal with a cleanup exception, and green-path
cleanup SHALL remain at least as complete as before the wrap. The
third family member — the Class C scratch-layout smoke test — keeps
its own #1128-delivered cleanup contract (see the existing
requirement covering it in this spec); this requirement neither
widens to future tests nor re-judges that one.

#### Scenario: Assertion failure still cleans the scratch tree

- **WHEN** a scratch-writing host-contract test fails one of its
  assertions after the evidence was written
- **THEN** the `finally` cleanup still removes the evidence
  artifacts (for the source-trust explicit-run test: both report
  files and both directory levels under
  `source-trust-explicit/`), leaving no debris on the shared node

#### Scenario: Cleanup never masks the failure signal

- **WHEN** the test's action fails before creating the evidence
  tree, the cleanup target is already absent, or the fixed path is
  occupied by an unexpected entry (e.g. a leftover directory where
  a file is expected)
- **THEN** the `finally` raises nothing — `rmtree` carries
  `ignore_errors=True` and the `unlink(missing_ok=True)` is wrapped
  in `contextlib.suppress(OSError)` (`missing_ok` alone does not
  cover `IsADirectoryError`/`PermissionError`) — and the pytest
  failure output shows the original error, never a cleanup
  `OSError`/`FileNotFoundError`

#### Scenario: Green path stays complete

- **WHEN** the test passes
- **THEN** the cleanup removes everything the pre-wrap manual
  sequence removed (the wrapped forms strictly cover the old
  unlink/rmdir set), and the suite's pass/skip distribution is
  unchanged on hosts where the test is skipped
