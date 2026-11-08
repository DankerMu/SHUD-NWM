## ADDED Requirements

### Requirement: Canonical converter diffs SHALL select the converter and adapter suites
A change confined to `workers/canonical_converter/**` SHALL select `tests/test_canonical_converter.py`, `tests/test_ifs_canonical.py`, `tests/test_era5_canonical.py`, `tests/test_e2e_ifs.py`, `tests/test_ifs_adapter.py`, `tests/test_gfs_adapter.py`, `tests/test_era5_adapter.py` and `tests/test_grib_packing_reader.py`, in addition to whatever the same-name derivation selects. Importer suites outside the package SHALL NOT be carried by the rule; each is dispositioned in `INTENTIONAL_RULE_GAP_EXCLUSIONS` with a reason token (`edge-consumer`, or `runtime-budget` with its measurement). `workers/canonical_converter` SHALL be one of the directories audited by the importer-gap guard, so a module-level static importer of the package without a rule or a reasoned exclusion makes the selector meta-suite fail.

#### Scenario: Converter-only diff
- **WHEN** only `workers/canonical_converter/converter.py` changes
- **THEN** the selection includes the eight converter and adapter suites
- **AND** it is not only `CORE_SMOKE_TESTS`

#### Scenario: GRIB packing diff
- **WHEN** only `workers/canonical_converter/grib_packing.py` changes
- **THEN** the selection includes `tests/test_grib_packing.py` and `tests/test_grib_packing_reader.py`

#### Scenario: CLI diff
- **WHEN** only `workers/canonical_converter/cli.py` changes
- **THEN** the selection includes the eight converter and adapter suites
- **AND** it does not include `tests/test_slurm_array_contract.py` or `tests/test_source_identity.py`, which are edge consumers owned by other rules

#### Scenario: Unrouted importer
- **WHEN** a non-gated suite statically imports `workers.canonical_converter` at module level and neither the rule nor `INTENTIONAL_RULE_GAP_EXCLUSIONS` covers it
- **THEN** `tests/test_select_ci_tests.py` fails naming that suite
