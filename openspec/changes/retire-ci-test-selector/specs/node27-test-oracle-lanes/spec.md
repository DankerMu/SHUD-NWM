## REMOVED Requirements

### Requirement: Oversized test suites are split without changing the collected set

**Reason**: Replaced by "Oversized test suites SHALL be split without changing the collected set": the selector scenarios of this requirement are cut (#3219), and OpenSpec refuses a MODIFIED block that drops a scenario, so the surviving text moves to a new header.
**Migration**: See the ADDED requirement "Oversized test suites SHALL be split without changing the collected set"; apart from the selector cuts its body and scenarios are unchanged.

## ADDED Requirements

### Requirement: Oversized test suites SHALL be split without changing the collected set
The three test files `tests/test_mvt_national_identity_probe_integration.py`, `tests/test_river_ts_read_path_surrogate_keys_integration.py` and `tests/test_node27_mvt_cache_retention.py` SHALL be split into files that are each under 1000 lines. `.large-file-guard.json` SHALL NOT exempt them. The set of collected test node suffixes, markers and skip outcomes SHALL be identical before and after the split.

#### Scenario: Collect-only parity
- **WHEN** `pytest --collect-only -q` runs on master and on the split branch, in the default configuration, with integration enabled, and with `-m integration`
- **THEN** the multiset of `::<name>[params]` node suffixes is identical in every configuration
