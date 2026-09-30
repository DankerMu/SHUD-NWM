## ADDED Requirements

### Requirement: Oversized test suites are split without changing the collected set
The three test files `tests/test_mvt_national_identity_probe_integration.py`, `tests/test_river_ts_read_path_surrogate_keys_integration.py` and `tests/test_node27_mvt_cache_retention.py` SHALL be split into files that are each under 1000 lines. `.large-file-guard.json` SHALL NOT exempt them. The set of collected test node suffixes, markers and skip outcomes SHALL be identical before and after the split.

#### Scenario: Collect-only parity
- **WHEN** `pytest --collect-only -q` runs on master and on the split branch, in the default configuration, with integration enabled, and with `-m integration`
- **THEN** the multiset of `::<name>[params]` node suffixes is identical in every configuration

#### Scenario: Selector still routes every part
- **WHEN** a change touches an owner path that previously selected one of the split files
- **THEN** the CI test selector selects every file split from it, and the PR-lane exclusion of the national identity probe applies to every part

### Requirement: Real-disk forcing suite derives its cycle from the live store
`tests/test_object_store_forcing_real_disk.py` SHALL select, at run time, the newest cycle for which all four station/source combinations are present in `OBJECT_STORE_ROOT`. Every time the suite asserts SHALL be derived from that cycle. When no such cycle exists, the suite SHALL fail with an explicit diagnostic rather than skip.

#### Scenario: Newest complete cycle is chosen
- **WHEN** the store holds cycles where the newest is missing one combination
- **THEN** the suite uses the newest cycle that has all four combinations

#### Scenario: No complete cycle
- **WHEN** no cycle has all four combinations
- **THEN** the suite fails with a message naming the store root and the per-combination counts

### Requirement: GRIB opt-in fails loudly when ecCodes cannot load
When `NHMS_RUN_GRIB=1` and grib tests are collected, the test session SHALL verify that the ecCodes runtime loads. If it does not, the session SHALL stop with a diagnostic that names the missing ecCodes runtime. It SHALL NOT skip the tests.

#### Scenario: Missing ecCodes runtime
- **WHEN** `NHMS_RUN_GRIB=1` and ecCodes cannot be loaded
- **THEN** the session reports one explicit ecCodes-runtime diagnostic instead of per-test RuntimeErrors or skips

#### Scenario: Opt-in absent
- **WHEN** `NHMS_RUN_GRIB` is unset
- **THEN** grib tests are skipped as before and no preflight runs

### Requirement: Environment-sensitive tests control their own environment
The outside-repo subprocess test in `tests/test_canonical_precip_copyback_backfill.py` SHALL run its subprocess without an inherited `PYTHONPATH`. The JSON-schema validator lookup SHALL fall back to the `check-jsonschema` next to the running interpreter.

#### Scenario: PYTHONPATH exported by the operator
- **WHEN** the suite runs with `PYTHONPATH` pointing at the repository
- **THEN** the outside-repo subprocess still cannot import `scripts` and the test passes

#### Scenario: Venv bin not on PATH
- **WHEN** the venv interpreter is invoked directly without its `bin` on `PATH`
- **THEN** the schema tests find `check-jsonschema` beside `sys.executable` and pass
