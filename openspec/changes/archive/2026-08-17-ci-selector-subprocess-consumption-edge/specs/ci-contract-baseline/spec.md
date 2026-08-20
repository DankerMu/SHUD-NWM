# ci-contract-baseline delta

## MODIFIED Requirements

### Requirement: Support-module changes MUST select their non-gated importer suites

For every tracked non-suite Python module under `tests/` (classified by the selector's canonical `is_test_suite_path` predicate), the targeted-test selector SHALL select the module's derived non-gated importer suites plus the selector meta-guard suite — where "importer suites" denotes suites reaching the module by EITHER edge kind: a top-level import, or an exact-literal-path consumption edge (a tracked top-level suite's source carrying the module's exact repo-relative path as a string constant, covering subprocess-executed helpers such as `tests/mock_shud_omp.py`) — (derivation authority: the selector test suite's mechanical importer derivation over the tracked tree — including its deliberate package-`__init__`-to-package aliasing — UNIONED with its literal-path consumption derivation, whose scan source excludes the selector meta-guard suite itself because that suite enumerates support-module paths as data; never a frozen list), and a selector-suite closure guard SHALL fail naming the module and missing suite whenever a derived importer suite is absent from the selection — with exactly two carve-outs, both guard-checked: modules on the explicit issue-scope carve-out allowlist (`tests/integration_helpers.py`, `tests/conftest.py` — recorded with their measured partial external coverage, not claimed as full compensation) are exempt only while each allowlisted path appears verbatim inside the `database:` paths-filter block of `.github/workflows/ci.yml`, and modules deriving zero importer suites by either edge kind SHALL keep selecting exactly the selector meta-guard suite, so a support-module-only PR runs assertion-level targets whenever import-derived or exact-literal-path assertion-level coverage exists in the tree (path spellings that never materialize the full repo-relative literal remain outside the derivation, a recorded boundary).

#### Scenario: importer-bearing support modules select real suites

- **WHEN** a PR's only backend change is
  `tests/fixtures/mapping_builder/in_memory_grid_snapshot.py`,
  `tests/slurm_template_helpers.py`,
  `tests/river_identity_backfill_fakes.py`, or `tests/__init__.py`
- **THEN** the selection includes that module's derived non-gated
  importer suites (5, 2, 2, and 3 suites respectively at authoring
  time) plus the selector meta-guard suite, `meta_guard_only` is
  false, and the targeted lane executes real assertions

#### Scenario: closure completeness is mechanized

- **WHEN** a new non-gated suite importing a routed support module at
  top level, or carrying its exact repo-relative literal path, is
  added to the tree without extending the module's rule
- **THEN** the closure guard fails, naming the module and the missing
  suite

#### Scenario: carve-out modules keep their recorded scope boundary

- **WHEN** the changed path is `tests/integration_helpers.py` or
  `tests/conftest.py`
- **THEN** the selector's routing is unchanged (meta-guard fallback)
  per the recorded issue-scope carve-out — whose comment carries the
  measured partial coverage from ci.yml's `database` filter starting
  `real-db-integration` — and the guard reds if an allowlisted path
  is no longer listed inside that filter block, forcing the
  carve-out to be re-decided

#### Scenario: subprocess-consumed support modules select their consumer suites

- **WHEN** a PR's only backend change is `tests/mock_shud_omp.py`,
  which no suite imports but which three non-file-gated suites
  reference by the exact literal `"tests/mock_shud_omp.py"` for
  execution via the runtime's `[sys.executable, <path>]` lane
- **THEN** the selection includes those consumer suites
  (`tests/test_shud_runtime.py`, `tests/test_direct_grid_e2e.py`,
  and `tests/test_e2e.py` — the last routed for closure integrity
  although its consuming tests sit behind function-level e2e gating
  and contribute no PR-lane assertions — at authoring time) plus
  the selector meta-guard suite, and the closure guard reds if a
  consumption edge loses its rule

#### Scenario: zero-consumer support modules keep the collapse route

- **WHEN** the changed path is a tracked non-suite `tests/` module
  with no derived non-gated importer suite by either edge kind
  (e.g. `tests/fixtures/mapping_builder/keliya/build.py`, whose own
  docstring records that suites read the checked-in fixture files
  and never invoke the script)
- **THEN** the selection is exactly the selector meta-guard suite,
  preserving the meta-guard-only collapse and its full-tree
  collect-only smoke

