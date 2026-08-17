# ci-contract-baseline delta

## MODIFIED Requirements

### Requirement: Guarded-module selector rules MUST cover their non-gated importer closure

The targeted-test selector SHALL, for each selector-guarded production
module (initially `packages.common.display_coverage` and
`services.slurm_gateway.real_backend`), via the rule
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

## ADDED Requirements

### Requirement: Directory-rule importer gaps MUST be dispositioned as selections or reasoned exclusions

For every tracked module under the nine audited directory paths (`workers/output_parser`, `workers/data_adapters`, `workers/forcing_producer`, `workers/shud_runtime`, `workers/model_registry`, `services/orchestrator`, `services/slurm_gateway`, `services/tile_publisher`, `services/production_closure` — including modules owned by earlier stop-rules rather than the directory rules themselves), a mechanized selector-suite guard SHALL derive the non-gated top-level importer suites the selector does not select for that module (node-id-only selection counts as a gap) and fail unless each such gap pair is either closed by a rule or present in an explicit exclusion table whose entries carry a reason token from {`fn-gated`, `redirect`, `edge-consumer`, `runtime-budget`} — where `fn-gated` denotes gating invisible to the file-level marker filter (function-level markers or environment opt-ins), established at disposition time by a recorded measurement in which the suite executed zero assertions, `edge-consumer` entries are guard-checked to be selected by at least one rule whose pattern does not match the excluded module, and `redirect` entries are guard-checked to still reach the suite via node-qualified ids in the module's own selection — and a stale exclusion (a pair that no longer derives as a gap) SHALL also fail the guard, so the audit that previously lived in a PR body can never rot silently again.

#### Scenario: an undispositioned gap reds the suite

- **WHEN** a non-gated test suite imports a directory-rule-owned
  module at top level and neither a rule selects it nor an exclusion
  entry names the pair
- **THEN** the disposition guard fails, naming the module and suite

#### Scenario: a stale exclusion reds the suite

- **WHEN** an exclusion entry's pair no longer derives as a gap
  (the rule now selects it, or the module/suite left the tree)
- **THEN** the disposition guard fails, naming the stale entry

#### Scenario: reasoned exclusions satisfy the guard by token and liveness

- **WHEN** a gap pair is present in the exclusion table with a valid
  reason token and the pair still derives as a gap (for
  `edge-consumer`: and a rule not matching the module selects the
  suite; for `redirect`: and the module's selection reaches the suite
  via node-qualified ids)
- **THEN** the guard passes for that pair — the guard checks token
  validity, pair liveness, and the structural
  `edge-consumer`/`redirect` conditions only; `fn-gated`/
  `runtime-budget` truthfulness is anchored by the recorded
  measurement table in the delivering PR, not re-executed by the
  guard
