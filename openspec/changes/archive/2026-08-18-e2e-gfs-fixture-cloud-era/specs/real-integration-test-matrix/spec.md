## ADDED Requirements

### Requirement: Mocked e2e GFS lane MUST stay executable against the cloud-era adapter

The `-m e2e` mocked GFS pipeline tests SHALL drive the adapter over the
NOMADS-bundle backend only, with the backend chain pinned hermetically in the
fixture (immune to ambient GFS_SOURCE_BACKENDS), and SHALL serve every
manifest bundle entry a payload carrying all of that entry's bundle
variables, so the download and canonical stages execute real assertions
instead of failing on fixture drift.

#### Scenario: e2e lane runs green under the marker gate

WHEN `NHMS_RUN_E2E=1 pytest tests/test_e2e.py -m e2e` runs on a clean tree
THEN the m1 and m2 pipeline tests execute their assertions and pass

#### Scenario: f000 bundle short-count is asserted truthfully

WHEN forecast hour 0 is part of the exercised cycle
THEN the canonical product count expectation excludes the f000-unavailable
variables instead of demanding the full per-hour variable set
