# ci-contract-baseline delta

## MODIFIED Requirements

### Requirement: Guarded-module selector rules MUST cover their non-gated importer closure

The targeted-test selector SHALL, for each selector-guarded production
module (currently `packages.common.display_coverage`,
`services.slurm_gateway.real_backend`, and `services.tiles.mvt`), via the rule
owning that module's path, select every tracked `tests/test_*.py`
that imports the module at top level and carries no `integration`/`e2e`
gating marker, and a mechanized selector test SHALL derive that importer
set from the tracked tree (never a frozen list) so that a new importer
suite or a removed rule entry fails the selector suite instead of
silently falling out of the PR lane.
The derivation SHALL additionally extend exactly ONE import hop beyond
the guarded module: tracked non-test modules importing the guarded
module at top level contribute their own non-gated top-level importer
suites to the required set. The single-hop bound is deliberate
forward-looking policy — it forecloses unbounded transitive growth
(an any-depth derivation reaches roughly five times the one-hop set
for `real_backend`) while today's top-level-import fixed point equals
the one-hop set, and both derivation and bound rationale live in the
selector test suite, never as frozen lists.
Modules under the nine audited directory paths are additionally
governed by the requirement "Directory-rule importer gaps MUST be
dispositioned as selections or reasoned exclusions", which owns the
normative disposition rule for their direct-importer gaps.

#### Scenario: production-module change selects its importer suites

- **WHEN** a PR changes only `packages/common/display_coverage.py` or only
  `services/slurm_gateway/real_backend.py`
- **THEN** the selector output includes every non-gated top-level importer
  test suite of that module (for `real_backend`:
  `tests/test_real_slurm_gateway.py`, `tests/test_slurm_array_contract.py`,
  `tests/test_job_array.py`; for `display_coverage`:
  `tests/test_display_coverage_refresh.py`,
  `tests/test_display_coverage_parallel.py`, `tests/test_forecast_api.py`)
- **WHEN** a PR changes only `services/tiles/mvt.py`
- **THEN** the selector output includes every non-gated direct importer suite
  of `services.tiles.mvt` (including `tests/test_hhe_mvt_binding.py`,
  `tests/test_hydro_display_mvt_scaling.py`,
  `tests/test_node27_timeseries_compression_benchmark.py`,
  `tests/test_node27_timeseries_compression_live_evidence.py`) and every
  one-hop suite contributed by `apps/api/routes/hydro_display.py` (including
  `tests/test_direct_grid_display_cutover_flip.py`,
  `tests/test_direct_grid_display_cutover_history.py`,
  `tests/test_direct_grid_display_cutover_model_resolution.py`),
  and no core-smoke-only fallback suite

#### Scenario: closure completeness is mechanized

- **WHEN** a new non-gated test suite importing a guarded module at top
  level is added to the tree without extending the owning rule
- **THEN** the traversal guard in the selector test suite fails, naming the
  missing suite

#### Scenario: gated importer suites are deliberately excluded

- **WHEN** an importer test suite is gated by an `integration` or `e2e`
  marker (skipped in the PR lane)
- **THEN** the traversal guard does not require it in the rule, and the
  exclusion rationale is recorded next to the rule

#### Scenario: one-hop importer suites are selected

- **WHEN** a PR changes only `services/slurm_gateway/real_backend.py`
- **THEN** the selector output includes the non-gated top-level importer
  suites of the modules that import `real_backend` at top level
  (including `tests/test_reconcile_sacct_parse.py`, which pins the sacct
  parsing constants consumed by `services/orchestrator/reconcile.py`),
  and the guard derives this one-hop set from the tree without recursing
  further
