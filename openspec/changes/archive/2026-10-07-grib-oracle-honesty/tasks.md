# Tasks

## 1. Implementation

- [x] 1.1 validate-met deterministic lane: no f000 interval variables; continuity expectation from the
      producer's row plan; exact deterministic entry count.
- [x] 1.2 Six validate-met tests: `grib` marker removed, expectations updated; lane named and shown executed.
- [x] 1.3 Producer guard in `_gfs_interval_row_times` with a unit test.
- [x] 1.4 `tests/grib2_fixture_support.py`; IFS e2e tests on real GRIB2 bundles with the eight-shortName and
      no-fallback assertions; one GFS decode test.
- [x] 1.5 Marker text (`pyproject.toml`, `tests/conftest.py`); `docs/runbooks/ci-test-routing.md` (lane text,
      `-rA` in the lane command).
- [x] 1.6 Review fix pass 1: a lane-level test that the continuity expectation follows the configured hours;
      refusal of a configuration with no forecast hour above 0; the runbook states what the lane does not
      cover; the large-file exemption is recorded in the proposal.

## 2. Evidence Floor

- [x] 2.1 Local (ecCodes installed): ruff; the grib lane command; `tests/test_production_met_validation.py`
      in full; the producer unit test; touched modules import with eccodes/cfgrib blocked; selector
      meta-guards after staging; `openspec validate grib-oracle-honesty --strict --no-interactive`.
- [x] 2.2 CI green on the PR; the Unit Tests job log shows the six validate-met tests executed.
- [x] 2.3a node-27 before merge: encode + cfgrib decode probe on ecCodes 2.47.0 (done 2026-10-08, see proposal).
- [x] 2.3 node-27 after merge: grib lane `-m grib -rA` all passed (files only, no database); the log shows
      the ecCodes version.

Deviation: no node-27 real-DB receipt applies (no database path is touched by the tests; the producer guard
has a unit test only). If any touched suite turns out to need a test database on node-27, that run is deferred
until the RAID link is repaired and listed here.
