# ci-contract-baseline Specification

## Purpose
TBD - created by archiving change governance-0-ci-contract-baseline. Update Purpose after archive.
## Requirements
### Requirement: Governance cleanup starts from a green contract baseline

Governance PRs that change role boundaries, dead-code paths, documentation authority, or entropy automation MUST start from a passing master contract baseline. The baseline includes backend fast tests and the generated frontend types matching `openapi/nhms.v1.yaml`.

#### Scenario: generated frontend types drift blocks governance
- **WHEN** `openapi/nhms.v1.yaml` generates TypeScript that differs from `apps/frontend/src/api/types.ts`
- **THEN** the governance baseline is not satisfied and cleanup PRs MUST wait until the generated type artifact is reconciled

#### Scenario: contract tests pass before downstream governance
- **WHEN** `tests/test_api_contract.py` and `tests/test_openapi_drift.py` pass on the current branch
- **THEN** downstream governance changes may use that branch as their baseline

#### Scenario: full backend fast gate passes before downstream governance
- **WHEN** Governance-1 through Governance-4 issues are started without an explicit maintainer waiver
- **THEN** the branch has passing evidence for `uv run pytest -q -m "not e2e and not grib and not integration"` in addition to focused OpenAPI and generated-type checks

#### Scenario: frontend contract generation check passes before downstream governance
- **WHEN** Governance-1 through Governance-4 issues are started without an explicit maintainer waiver
- **THEN** `cd apps/frontend && corepack pnpm run check:api-types` passes or equivalent CI evidence proves generated frontend types match the committed OpenAPI contract

### Requirement: Python tooling commands use the repository-managed environment

Developer entrypoints for backend Python work MUST use `uv run` so local, CI, and production-like checks resolve the same locked environment.

#### Scenario: Makefile test and lint targets use uv
- **WHEN** a developer runs `make test` or `make lint`
- **THEN** the underlying commands run through `uv run pytest` and `uv run ruff check .`

#### Scenario: Makefile app and migration targets use uv
- **WHEN** a developer runs `make dev`, `make migrate`, `make seed-demo`, or `make seed-m1-model`
- **THEN** Python modules are invoked through `uv run python -m ...`

#### Scenario: Makefile reset-db preserves uv-backed child targets
- **WHEN** a developer inspects or runs `make reset-db`
- **THEN** the target preserves the existing database drop/create sequence and invokes `$(MAKE) migrate` and `$(MAKE) seed-demo`, so migration and seed Python modules run through the uv-backed child targets

### Requirement: Targeted CI selection MUST include a changed script's same-name test suite

The targeted-test selector SHALL map every changed backend Python source under
`apps/api/`, `packages/`, `services/`, `workers/`, or `scripts/` whose same-name
test file `tests/test_<basename>.py` exists to that test file, treating the hit
as a known mapping that suppresses the unknown-backend smoke fallback for that
path. The five-prefix source domain SHALL share one authority with backend
Python path classification; the same-name target remains file-level and
existence-gated. An explicit rule and same-name derivation SHALL contribute
their set union, while a source with neither an explicit rule nor an existing
same-name test keeps the existing fallback behavior. Mapping completeness over
the tracked tree SHALL be enforced by a mechanized selector test rather than a
hand-maintained rule list. When more than one source in the domain shares a
basename and therefore maps to one same-name suite, the mechanized guard SHALL
require that suite to import every colliding source module so basename
convergence cannot silently route an unrelated suite.

#### Scenario: changed backend source selects its own suite

- **WHEN** a PR changes only a Python source under one of the five backend
  prefixes and `tests/test_<basename>.py` exists in the tree
- **THEN** the selector output includes that same-name test and does not
  substitute the unrelated core-smoke fallback for that path

#### Scenario: explicit and derived mappings form a union

- **WHEN** a changed backend source matches an explicit path rule and also has
  an existing same-name suite
- **THEN** the selector output contains the union of both mappings without
  duplicate targets or removal of the explicit rule's suites

#### Scenario: missing same-name suite preserves fallback

- **WHEN** a changed backend Python source has neither an explicit rule nor an
  existing `tests/test_<basename>.py`
- **THEN** the selector retains the existing unknown-backend core-smoke
  fallback and does not treat the nonexistent derived target as a mapping

#### Scenario: completeness is mechanized

- **WHEN** a new source is added under any of the five backend prefixes with a
  same-name test but no explicit selector rule
- **THEN** the selector already reaches the test via the same-name mapping, and
  a tracked-tree guard derives the pair without a frozen source list and fails
  if the suite is not selected

#### Scenario: basename collisions remain semantically bound

- **WHEN** two or more tracked backend sources share a basename and therefore
  map to the same `tests/test_<basename>.py`
- **THEN** the tracked-tree guard requires that suite to import every colliding
  source module and fails by naming any source whose import edge is absent

#### Scenario: a same-name source change schedules the collision guard

- **WHEN** a PR changes a backend Python source whose same-name test file exists
- **THEN** the selector output also includes the selector meta-guard suite, so
  the collision/import contract runs in the PR targeted lane on exactly the
  source-only PRs that can add a colliding source

### Requirement: Targeted CI selection MUST include the container contract's dependent suites

A change to `packages/common/node27_container_contract.py` SHALL
select every test suite in its dependent closure — the tracked test
files that import the contract directly in either spelling form, plus
test files that import a `scripts/` module whose scripts-import graph
reaches the contract (computed to a fixed point, not one hop) —
instead of falling through to the core-smoke fallback, and a
meta-guard SHALL derive that closure from import analysis so that
closure growth reddens the guard rather than silently unselecting new
dependents.

#### Scenario: A contract-only diff selects the dependent closure

- **WHEN** the changed-file list contains only
  `packages/common/node27_container_contract.py`
- **THEN** the selected tests are a superset of the contract's
  dependent closure (currently the five node27 timeseries compression
  benchmark/capture/supervisor/live-evidence and decompression-replay
  suites) and share no member with the core-smoke fallback set

#### Scenario: The transitive dependent is derived, not grepped

- **WHEN** the meta-guard computes the contract's dependent closure
- **THEN** the closure includes the live-evidence suite — whose text
  never names the contract and is reachable only through import
  analysis of the scripts modules it imports — and is a superset of
  an independently derived direct-importer set, so a degenerate
  closure computation fails loudly without freezing the closure's
  size

#### Scenario: Removing the mapping rule is caught

- **WHEN** the explicit selector rule for the contract is removed
  while the dependent closure is non-empty
- **THEN** the meta-guard fails

### Requirement: Empty targeted-test selection MUST be loudly self-identifying

The `Unit Tests` job's collect-only fallback SHALL be independently
recognizable as a zero-assertion run whenever the PR backend gate is open
but the targeted-test selector maps the diff to zero test files: it MUST
emit a workflow
warning annotation and a step summary stating that no assertions were
executed, and the collection outcome MUST be surfaced in the job log — the
collected-count summary on success and the full collection output on
failure, with a collection failure still failing the step. The selector SHALL
NOT silently shrink its selection: when a rule-selected test target no
longer exists in the tree, the selector MUST emit a warning naming the
dropped target (stderr always; a workflow warning annotation when running
under GitHub Actions) while keeping its return-value semantics unchanged.
The collect-only branch's check name and pass/fail semantics are unchanged
by this requirement (gate-strength changes are out of scope).
Additionally, when the final selection collapses to exactly the selector
meta-guard suite (`meta_guard_only` — a selection-shape property that
also fires for selector-development PRs whose diff-specific target is
that suite), the selector SHALL expose the collapse as a
distinguishable GitHub-output field and the `Unit Tests` job SHALL run
the targeted selection AND the labeled full-tree collect-only smoke,
whose labeling on this branch MUST NOT claim zero assertions were
executed; a PR whose only backend change deletes a test file (or
touches only a `tests/` support module without derived non-gated
importer suites) thereby keeps the import-surface guard it had
before the meta-guard accumulation existed. Support modules WITH
derived non-gated importer suites are governed by the requirement
"Support-module changes MUST select their non-gated importer
suites", which routes them to assertion-level targets instead of
this collapse path.

#### Scenario: collect-only fallback is labeled as zero assertions

- **WHEN** a PR hits the `backend` paths-filter but the selector returns
  zero test files (e.g. a `schemas/**`-only change)
- **THEN** the `Unit Tests` job run shows a warning annotation and a step
  summary stating that 0 assertions were executed and only collect-only
  import/syntax smoke ran, and the pytest collected-count summary appears
  in the job log (full collection output on failure, which fails the step)

#### Scenario: stale rule target is dropped with a warning, not silently

- **WHEN** a selection rule maps a changed path to a test file that does
  not exist in the tree
- **THEN** the selector drops the target from its output but emits a
  warning naming the missing target, and emits no such warning when every
  selected target exists

#### Scenario: known empty-selection input classes are pinned

- **WHEN** the diff consists only of files in the known unmapped classes
  (`schemas/**`, unmapped `infra/**`, `.py` outside the five backend
  prefixes, non-`.py` under backend prefixes, non-`.py` under `tests/`,
  `.sh` files outside `scripts/`; `scripts/**/*.sh` left this list when it
  joined the backend gate — an unmapped one now arms the core-smoke fallback)
- **THEN** the selector returns an empty selection and the selector test
  suite pins each class explicitly as the route-C contract, so any future
  route-A/B policy change must flip a visible assertion

#### Scenario: meta-guard-only collapse restores the collect-only smoke

- **WHEN** a PR's only backend change deletes one `tests/test_*.py` file,
  so the missing-target filter leaves exactly
  `tests/test_select_ci_tests.py` in the selection
- **THEN** the selector's GitHub output reports `meta_guard_only=true`,
  and the `Unit Tests` job runs the meta-guard suite and additionally the
  labeled full-tree collect-only smoke, with a collection failure failing
  the step

#### Scenario: non-collapsed selections suppress the flag

- **WHEN** the selection contains any target other than the selector
  meta-guard suite, or is empty
- **THEN** the GitHub output reports `meta_guard_only=false` and the
  targeted branch behaves as before

#### Scenario: selector-development PRs fire the flag honestly

- **WHEN** the diff's only backend change is
  `scripts/select_ci_tests.py` or `tests/test_select_ci_tests.py`, so
  the diff-specific selection IS exactly the meta-guard suite
- **THEN** `meta_guard_only=true` and the collect-only smoke also runs
  — accepted by design (one extra collection pass on exactly the PR
  class that changes the gate), and the smoke labeling does not claim
  the run executed zero assertions

### Requirement: Guarded-module selector rules MUST cover their non-gated importer closure

The targeted-test selector SHALL, for each selector-guarded production module (currently `packages.common.display_coverage`, `services.slurm_gateway.real_backend`, `services.tiles.mvt`, and `apps.api.routes.hydro_display`), via the rule owning that module's path, select every tracked `tests/test_*.py` that imports the module at top level and carries no `integration`/`e2e` gating marker, and a mechanized selector test SHALL derive that importer set from the tracked tree (never a frozen list) so that a new importer suite or a removed rule entry fails the selector suite instead of silently falling out of the PR lane.
The derivation SHALL additionally extend exactly ONE import hop beyond the guarded module: tracked non-test modules importing the guarded module at top level contribute their own non-gated top-level importer suites to the required set. The single-hop bound is deliberate forward-looking policy — it forecloses unbounded transitive growth (an any-depth derivation reaches roughly five times the one-hop set for `real_backend`) while today's top-level-import fixed point equals the one-hop set, and both derivation and bound rationale live in the selector test suite, never as frozen lists.
Modules under the ten audited directory paths are additionally governed by the requirement "Directory-rule importer gaps MUST be dispositioned as selections or reasoned exclusions", which owns the normative disposition rule for their direct-importer gaps.

#### Scenario: production-module change selects its importer suites

- **WHEN** a PR changes only `packages/common/display_coverage.py` or only `services/slurm_gateway/real_backend.py`
- **THEN** the selector output includes every non-gated top-level importer test suite of that module (for `real_backend`: `tests/test_real_slurm_gateway.py`, `tests/test_slurm_array_contract.py`, `tests/test_job_array.py`; for `display_coverage`: `tests/test_display_coverage_refresh.py`, `tests/test_display_coverage_parallel.py`, `tests/test_forecast_api.py`)
- **WHEN** a PR changes only `services/tiles/mvt.py`
- **THEN** the selector output includes every non-gated direct importer suite of `services.tiles.mvt` (including `tests/test_hhe_mvt_binding.py`, `tests/test_hydro_display_mvt_scaling.py`, `tests/test_node27_timeseries_compression_benchmark.py`, `tests/test_node27_timeseries_compression_live_evidence.py`) and every one-hop suite contributed by `apps/api/routes/hydro_display.py` (including `tests/test_direct_grid_display_cutover_flip.py`, `tests/test_direct_grid_display_cutover_history.py`, `tests/test_direct_grid_display_cutover_model_resolution.py`), and no core-smoke-only fallback suite
- **WHEN** a PR changes only `apps/api/routes/hydro_display.py`
- **THEN** the selector output includes its full non-gated direct union one-hop importer closure, including direct-grid cutover, display status, HHE/MVT, node-27 compression and attribution, OpenAPI, and runtime-mode suites

#### Scenario: closure completeness is mechanized

- **WHEN** a new non-gated test suite importing a guarded module at top level is added to the tree without extending the owning rule
- **THEN** the traversal guard in the selector test suite fails, naming the missing suite

#### Scenario: gated importer suites are deliberately excluded

- **WHEN** an importer test suite is gated by an `integration` or `e2e` marker (skipped in the PR lane)
- **THEN** the traversal guard does not require it in the rule, and the exclusion rationale is recorded next to the rule

#### Scenario: one-hop importer suites are selected

- **WHEN** a PR changes only `services/slurm_gateway/real_backend.py`
- **THEN** the selector output includes the non-gated top-level importer suites of the modules that import `real_backend` at top level (including `tests/test_reconcile_sacct_parse.py`, which pins the sacct parsing constants consumed by `services/orchestrator/reconcile.py`), and the guard derives this one-hop set from the tree without recursing further

#### Scenario: Existing MVT closure remains an exact compatibility anchor

- **WHEN** the selector and guarded-module registry change for this batch
- **THEN** the `services/tiles/mvt.py` exact-set pin and guarded closure delivered by #1597 remain green without removing an existing MVT target

### Requirement: Changed-test PRs MUST run the selector meta-guards

The targeted-test selector SHALL additionally select the selector's own
test suite (`tests/test_select_ci_tests.py`) whenever any
`tests/test_*.py` file is among the changed paths, so that
tracked-tree-derived meta-guards run on exactly the PR class that can
invalidate them, while preserving changed-test self-selection and the
existing redirect-rule semantics.
A changed `tests/` Python file whose BASENAME matches neither
`test_*.py` nor `*_test.py` (a support module such as `conftest.py`,
`integration_helpers.py`, or `__init__.py`) SHALL NOT self-select —
pytest returns `NO_TESTS_COLLECTED` (exit 5) for such a target, which
ci.yml's `check=True` renders as a misleading failure — and SHALL
instead map to its routed non-gated importer suites plus the selector
meta-guard suite when a support-module rule routes it (see
"Support-module changes MUST select their non-gated importer
suites"), or to exactly the selector meta-guard suite for support
modules with no derived non-gated importer suites and for the
recorded carve-outs (a module with derived importers but no rule is
the closure guard's red state, not a licensed selection), so
the emitted selection
always consists of collectible test files. The suite-vs-support
classification is basename-shaped at any depth and MUST equal pytest's
own collection rule (`testpaths = ["tests"]`, default `python_files` =
`test_*.py` and `*_test.py`), anchored by a test that derives the rule
from pytest itself rather than restating it: a nested
`tests/<pkg>/test_*.py` or `*_test.py` suite self-selects and drags
the meta-guards exactly like a top-level one.

#### Scenario: standalone changed test file selects the meta-guards

- **WHEN** a PR changes only one `tests/test_*.py` file with no matching
  redirect rule
- **THEN** the selector output includes both that file and
  `tests/test_select_ci_tests.py`

#### Scenario: redirect semantics preserved

- **WHEN** a changed test file matches an existing redirect rule (for
  example the orchestrator manifest-surface redirect together with its
  surface files)
- **THEN** the selector still emits the redirect's focused targets (not the
  whole changed suite) plus `tests/test_select_ci_tests.py`

#### Scenario: tests support files map to a collectible selection

- **WHEN** a PR changes only `tests/conftest.py` (or any tracked
  `tests/` Python file whose basename matches neither `test_*.py`
  nor `*_test.py`)
- **THEN** the selector output never contains the support file itself
  — it is the routed importer suites plus
  `tests/test_select_ci_tests.py` for rule-routed support modules,
  and exactly `tests/test_select_ci_tests.py` for modules without
  derived importers and for the recorded carve-outs (including
  `tests/conftest.py`) — and a tree-derived invariant test covers
  every current and future support module without hardcoding names

#### Scenario: nested test suites are suites, not support files

- **WHEN** a PR adds or changes a nested `tests/<pkg>/test_*.py` suite
- **THEN** the suite self-selects (its assertions run in the PR lane)
  and the selector meta-guards accumulate, identically to a top-level
  `tests/test_*.py` change

### Requirement: CI paths-filters MUST NOT carry dead file patterns

The CI workflow SHALL keep every literal (non-glob) file pattern in the
`changes` job paths-filters of
`.github/workflows/ci.yml` corresponding to a path that exists in the
tracked tree; a pattern left behind by a file deletion is removed together
with the deletion or in a follow-up hygiene change.

#### Scenario: dead database-filter pattern removed

- **WHEN** the `database` filter is read after this change
- **THEN** it contains no `tests/test_worker_chain_smoke.py` entry and every
  remaining pattern expands to at least one tracked file

### Requirement: Entropy hard gate MUST be green on master without weakening the gate

The repository SHALL keep the entropy hard gate
(`build_report(mode="hard-gate")`)
reporting `hard_gate_status == "pass"` on master, and restoring it after
a diagnostic-token finding SHALL change the flagged production-adjacent
text rather than adding checker exemptions, unless the checker itself is
provably wrong.

#### Scenario: diagnostic-token comment reworded

- **WHEN** the entropy hard gate flags a prose comment in
  `workers/shud_runtime/runtime.py` for naming a QHH diagnostic token
- **THEN** the comment is reworded to drop the literal token with zero
  executable-logic change, and
  `tests/test_entropy_audit_script.py` passes with
  `hard_gate_failing_count == 0`

### Requirement: Selector path-rule duplicate patterns MUST be allowlisted decisions

Every pattern appearing more than once in the selector's `PATH_TEST_RULES` SHALL be present in an explicit intentional-duplicate allowlist in the selector test suite, and every allowlist member SHALL actually be duplicated, so an unexplained duplicate (such as the `packages/common/display_coverage.py` collision an unmerged sibling PR would introduce) fails the suite instead of silently splitting rule ownership; the changed-test rule table is exempt because its duplicates are load-bearing design.

#### Scenario: today's deliberate layering stays green

- **WHEN** the guard runs against the current `PATH_TEST_RULES`
- **THEN** it passes: the only duplicated pattern
  (`services/orchestrator/scheduler.py`, a deliberate narrow+stop
  layering) is allowlisted, and the allowlist contains no
  non-duplicated member

#### Scenario: an unlisted duplicate goes red

- **WHEN** a rule list contains a second
  `packages/common/display_coverage.py` entry without an allowlist
  change (simulated in the suite against a constructed list)
- **THEN** the guard flags that pattern by name

### Requirement: Selector gating-marker exclusions MUST anchor to the conftest auto-skip set

The selector test suite's gating-marker exclusion set (`GATING_MARKER_NAMES`) SHALL be mechanically anchored to the auto-skip marker set derived from `tests/conftest.py`'s `pytest_collection_modifyitems` (AST-derived, failing loudly if the derivation finds nothing), such that the derived set minus the exclusion set is exactly the recorded deliberate absences (today `{"grib"}`, justified by zero file-level `pytestmark` users), and markers that are registered but not auto-skipped (`real_disk`, `timescaledb_210`) SHALL be asserted absent from the derived set so they can never be wrongly excluded from PR-lane selection.

#### Scenario: conftest skip-set drift forces a visible decision

- **WHEN** `tests/conftest.py` adds or removes an auto-skipped marker
  without a matching update to the exclusion set or the recorded
  absences
- **THEN** the anchor assertion fails, naming the drifted marker

#### Scenario: registered-but-running markers stay selectable

- **WHEN** the derivation runs against today's conftest
- **THEN** `real_disk` and `timescaledb_210` are not in the derived
  auto-skip set, and the suite asserts their absence explicitly

### Requirement: Directory-rule importer gaps MUST be dispositioned as selections or reasoned exclusions

For every tracked module under the ten audited directory paths (`workers/output_parser`, `workers/data_adapters`, `workers/forcing_producer`, `workers/shud_runtime`, `workers/model_registry`, `workers/mapping_builder`, `services/orchestrator`, `services/slurm_gateway`, `services/tile_publisher`, `services/production_closure` — including modules owned by earlier stop-rules rather than the directory rules themselves), a mechanized selector-suite guard SHALL derive the non-gated top-level importer suites the selector does not select for that module (node-id-only selection counts as a gap) and fail unless each such gap pair is either closed by a rule or present in an explicit exclusion table whose entries carry a reason token from {`fn-gated`, `redirect`, `edge-consumer`, `runtime-budget`} — where `fn-gated` denotes gating invisible to the file-level marker filter (function-level markers or environment opt-ins), established at disposition time by a recorded measurement in which the suite executed zero assertions, `edge-consumer` entries are guard-checked to be selected by at least one rule whose pattern does not match the excluded module, and `redirect` entries are guard-checked to still reach the suite via node-qualified ids in the module's own selection — and a stale exclusion (a pair that no longer derives as a gap) SHALL also fail the guard, so the audit that previously lived in a PR body can never rot silently again.

#### Scenario: an undispositioned gap reds the suite

- **WHEN** a non-gated test suite imports a directory-rule-owned module at top level and neither a rule selects it nor an exclusion entry names the pair
- **THEN** the disposition guard fails, naming the module and suite

#### Scenario: a stale exclusion reds the suite

- **WHEN** an exclusion entry's pair no longer derives as a gap (the rule now selects it, or the module/suite left the tree)
- **THEN** the disposition guard fails, naming the stale entry

#### Scenario: reasoned exclusions satisfy the guard by token and liveness

- **WHEN** a gap pair is present in the exclusion table with a valid reason token and the pair still derives as a gap (for `edge-consumer`: and a rule not matching the module selects the suite; for `redirect`: and the module's selection reaches the suite via node-qualified ids)
- **THEN** the guard passes for that pair — the guard checks token validity, pair liveness, and the structural `edge-consumer`/`redirect` conditions only; `fn-gated`/`runtime-budget` truthfulness is anchored by the recorded measurement table in the delivering PR, not re-executed by the guard

#### Scenario: mapping-builder joins the audited domain with owned state-clone edges dispositioned

- **WHEN** `workers/mapping_builder` is added to the audited directory paths with its package-wide selector rule
- **THEN** its eight mapping-builder-suite gaps are closed by the rule, the three state-clone importer pairs remain selected by non-matching state-clone owner rules and are recorded as `edge-consumer`, and the disposition guard reports zero undispositioned pairs without adding `tests/test_state_clone.py` to the mapping-builder rule

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

### Requirement: Meta-guard tree derivations MUST stay content-faithful under parse caching

The selector meta-guard suite's parse layer (`_parse_tracked`) SHALL
memoize parse results keyed by resolved file identity — the resolved
absolute path plus stat identity (mtime_ns and size) — so that
reusing parses within one suite run cannot change derivation
semantics: a working-directory change SHALL NOT alias a test-fixture
path onto a same-named repository file's cached parse, and a rewrite
of a previously parsed file SHALL be observed by subsequent parses.
Because cache hits hand every consumer the SAME `ast.Module`
instance, the suite SHALL mechanically guard its own source against
tree-mutation idioms — attribute stores and deletes in any
assignment form, subscript stores and deletes over an attribute
base, classes with a direct `NodeTransformer` base, calls to
`fix_missing_locations`/`copy_location`/`increment_lineno`,
bare-name `setattr`/`delattr` calls, and mutating list-method calls
on an attribute receiver — keeping the shared-instance premise a
standing assertion rather than a one-time audit, with the scan's
red and no-false-positive arms landed as standing tests.
(Recorded boundaries: a rewrite preserving resolved path, mtime_ns
and size is outside the cache's discrimination — unreachable under
the suite's no-tracked-mutation probe discipline; the `filename=`
argument to `ast.parse` affects only parse-time SyntaxError messages
and is not carried by the returned tree, so cache reuse cannot alter
any derivation through it; the mutation scan matches `setattr` only
as a bare name — `monkeypatch.setattr` is a legitimate attribute
callee in this module — so attribute-callee `setattr` aliases,
out-of-module helpers, and indirect `NodeTransformer` subclasses
evade it, a recorded tripwire limit.)

#### Scenario: cwd changes cannot alias fixture paths onto repository parses

- **WHEN** a test parses a tracked repository file via its
  repo-relative spelling, then chdirs into a temporary directory
  containing a different file at the same relative spelling and
  parses that spelling again
- **THEN** the second parse reflects the temporary file's content,
  not the cached repository parse

#### Scenario: rewrites of a parsed file are observed

- **WHEN** a file is parsed, rewritten with different content and a
  distinct stat identity, and parsed again
- **THEN** the second parse reflects the rewritten content

#### Scenario: tree-mutation idioms are mechanically barred

- **WHEN** a change to the meta-guard suite introduces an attribute
  store or delete (any assignment form), a subscript store or delete
  over an attribute base, a class with a direct `NodeTransformer`
  base, a call to
  `fix_missing_locations`/`copy_location`/`increment_lineno`, a
  bare-name `setattr`/`delattr` call, or a mutating list-method call
  on an attribute receiver
- **THEN** the shared-AST mutation guard fails, naming the offending
  construct and line

### Requirement: Shell wrapper changes MUST gate their guard suites

The CI change-detection gate SHALL treat tracked `scripts/**/*.sh` files as
backend surface: the `backend` paths-filter matches them, and the targeted
test selector maps each shell wrapper that has committed guard tests to those
guard test files. A `scripts/**/*.sh` path with no explicit mapping falls back
to the core smoke selection instead of an empty selection.

#### Scenario: sh-only change selects the wrapper's guard suite

WHEN a pull request changes only `scripts/scheduler_file_provider_refresh_once.sh`
THEN the `backend` filter reports true
AND the targeted selector output includes `tests/test_scheduler_file_provider_refresh.py`

#### Scenario: unmapped shell script falls back loudly, not empty

WHEN a pull request changes only a new `scripts/**/*.sh` file that has no
selector mapping
THEN the targeted selector returns at least the core smoke test set
AND does not return an empty selection

#### Scenario: sh plus py change selects the union of guards

WHEN a pull request changes both a mapped shell wrapper and a mapped backend
python module
THEN the targeted selector output contains both surfaces' guard suites

#### Scenario: a mapped shell wrapper does not pull core smoke

WHEN a pull request changes only a shell wrapper that has an explicit guard
mapping
THEN the selection contains its guard suite and no core-smoke fallback entries

### Requirement: CI concurrency MUST preserve every non-PR workflow run

The CI workflow MUST group pull-request runs by pull-request identity and cancel superseded runs for that same pull request, while every `push` and `workflow_dispatch` run MUST use its own workflow-run identity and MUST NOT cancel or replace another non-PR run that is running or pending.

#### Scenario: Superseded pull-request run is cancelled

- **WHEN** a new CI run starts for the same pull request
- **THEN** both runs resolve to the same PR-scoped concurrency group and `cancel-in-progress` is true for that event

#### Scenario: Master pushes cannot cancel or replace each other

- **WHEN** two `push` events on `master` create separate CI workflow runs
- **THEN** their groups differ by `github.run_id`, `cancel-in-progress` is false, and neither run is removed by this workflow's concurrency policy

#### Scenario: Manual full CI is isolated from master pushes

- **WHEN** a `workflow_dispatch` run overlaps a `push` run
- **THEN** each uses its own `github.run_id` group and neither can cancel or replace the other through this concurrency policy

### Requirement: CI workflow policy changes MUST execute their contract suite

A pull request that changes `.github/workflows/ci.yml` MUST open the backend targeted-test gate and the selector MUST choose `tests/test_select_ci_tests.py`, so concurrency, paths-filter, and workflow-consumer contracts execute assertions on the same pull request that changes them. The paths-filter PR files result MUST be the single changed-file authority consumed by the selector; the selector MUST NOT recompute a merge-base diff that can diverge after master changes while the pull request is open.

#### Scenario: Workflow-only change reaches assertion-level tests

- **WHEN** the changed-file set contains `.github/workflows/ci.yml`
- **THEN** the backend paths-filter matches and targeted selection contains `tests/test_select_ci_tests.py` rather than collapsing to zero-assertion collect-only smoke

#### Scenario: Removing either routing leg fails a guard

- **WHEN** the workflow path is removed from either the backend filter or the selector rule
- **THEN** the selector contract suite fails and names the missing self-routing leg

#### Scenario: Selector consumes the same pull-request changed-file set as paths-filter

- **WHEN** master changes while a pull request remains open, including an identical change to a file also touched by the pull request
- **THEN** the targeted selector consumes the paths-filter `all_files` output for that pull request and cannot silently drop the file through a separate merge-base diff

### Requirement: OpenAPI error validation MUST be green without weakening rules

The exact CI OpenAPI Validate command using pinned Redocly 1.25.13 and only the existing `no-unused-components` skip MUST exit zero with no errors. The repair MUST NOT downgrade the declared dialect, disable `spec`, `security-defined`, or `no-empty-servers`, add a broad config suppression, or replace validation with a warning-only/collect-only check.

#### Scenario: Declared 3.1 contract passes the pinned validator

- **WHEN** CI lints `openapi/nhms.v1.yaml` with the repository command
- **THEN** all dialect, security-defined, and server errors are absent and the command exits zero

#### Scenario: Removing a load-bearing repair leg fails

- **WHEN** a mutation removes null normalization, the same-origin server, root security, or one protected-operation override
- **THEN** a contract test or the pinned Redocly command fails and names the missing invariant

### Requirement: OpenAPI changes MUST execute drift and generated-type assertions

A pull request changing `openapi/**` MUST open the backend targeted-test gate and select `tests/test_openapi_drift.py`, `tests/test_api_contract.py`, and `tests/test_openapi_31_contract.py`; a change to `apps/api/openapi_patching.py` MUST also select the drift and 3.1 invariant suites while preserving its existing API consumer tests. Generated frontend types MUST match the static schema, and a semantics-preserving dialect repair MUST leave the committed business type artifact byte-identical.

#### Scenario: OpenAPI-only change reaches assertions

- **WHEN** the changed-file set contains only `openapi/nhms.v1.yaml`
- **THEN** the backend filter is true and targeted selection includes the drift, API contract, and 3.1 dialect/security invariant suites without core-smoke fallback or zero-assertion collapse

#### Scenario: Patch-owner change reaches all consumers

- **WHEN** `apps/api/openapi_patching.py` changes
- **THEN** targeted selection includes the drift suite, 3.1 dialect/security invariant suite, generated-type/API contract suite, and the existing broad API consumer suites

#### Scenario: Generated business types do not drift

- **WHEN** all legacy nullable nodes are re-expressed with equivalent 3.1 unions and security/server metadata is added
- **THEN** `check:api-types` and the Python generated-type byte comparison pass with no change to `apps/frontend/src/api/types.ts`

### Requirement: Shared-library targeted selection MUST retain its baseline

Targeted CI selection SHALL include the core-smoke baseline for every changed backend Python source under `packages/common/**`, in addition to every explicit, same-name, and supplemental mapping. A narrow rule SHALL NOT replace baseline coverage for this shared-library root. Other backend roots retain their existing known-rule and unknown-path fallback semantics.

#### Scenario: Narrow shared-library rule is additive

- **WHEN** a PR changes only `packages/common/state_manager.py` or only `packages/common/state_cli.py`
- **THEN** the selector output contains the narrow state suites and every core-smoke suite, including `tests/test_production_scheduler.py`

#### Scenario: Unmapped shared-library source keeps the same baseline

- **WHEN** a PR changes only an unmapped `packages/common/**` Python source
- **THEN** the selector output contains the same core-smoke baseline exactly once and does not depend on whether another explicit rule exists

#### Scenario: Non-shared rules preserve existing suppression semantics

- **WHEN** a mapped backend source outside `packages/common/**` changes
- **THEN** its existing explicit/same-name selection and core-smoke suppression behavior remain unchanged unless another requirement explicitly adds a supplemental oracle

### Requirement: Tree-scanning invariant suites MUST follow every scanned source root

A selector supplemental-rule authority SHALL map every Python source path under the four roots scanned by `tests/test_timescale_write_guard_wire_site_invariant.py` (`workers/**`, `packages/common/**`, `scripts/**`, and `db/**`) to that invariant suite. Supplemental mappings SHALL form a monotonic set union with ordinary rules, SHALL NOT stop later rules, and SHALL NOT change whether a path is known for fallback purposes. A selector meta-test SHALL derive the roots from the invariant suite's `_scan_roots` definition rather than freeze a second list.

#### Scenario: Existing writer and guard sources select the invariant

- **WHEN** a PR changes only `workers/output_parser/parser.py`, `workers/forcing_producer/store.py`, `packages/common/forcing_domain_handoff_apply.py`, or `packages/common/timescale_write_guard.py`
- **THEN** the selector output includes `tests/test_timescale_write_guard_wire_site_invariant.py` in addition to each source's ordinary selection

#### Scenario: Future file under a scanned root is covered

- **WHEN** the changed-file input names a previously nonexistent Python path under each scanned root, such as `scripts/brand_new_thing.py`
- **THEN** the selector output includes the invariant suite without requiring an at-site rule

#### Scenario: Root or supplemental rule drift is caught

- **WHEN** a scanned root is added or a supplemental mapping is deleted or narrowed
- **THEN** the selector meta-test fails and names the uncovered root or source

### Requirement: Selector-development changes MUST retain full-tree collection smoke

The selector SHALL expose a `collection_smoke_required` GitHub output whose provenance is independent of the final selected-target list shape. It SHALL be true when the final selection is exactly the selector meta-guard or when the changed-file set contains `scripts/select_ci_tests.py` or `tests/test_select_ci_tests.py`; otherwise it SHALL be false for non-empty ordinary selections. The `Unit Tests` workflow SHALL use this field to run the labeled full-tree collect-only smoke in addition to targeted assertions. Targeted assertions SHALL execute before this smoke; when the smoke is required, the full-tree collect command SHALL be executable and reachable, its failure SHALL emit its log and return nonzero, and its label SHALL NOT claim zero assertions after targeted assertions ran. One full-workflow positive contract SHALL prove both effective Actions metadata and shell behavior. It SHALL require the audited named-step identity, exact targeted-test environment binding, default root checkout/shell semantics, and fail-closed step/job policy. It SHALL reject any unapproved `run` payload before execution. A bounded probe SHALL execute only an independently authored trusted fixture and finite test-owned semantic variants, clean its process group on every exit, and prove the behavior matrix without running real tests, database, network, or arbitrary workflow commands. The existing `meta_guard_only` field SHALL remain a final-list shape property and zero-selection behavior SHALL remain unchanged.

#### Scenario: Selector source change keeps supplemental and collection oracles

- **WHEN** the only changed path is `scripts/select_ci_tests.py`
- **THEN** the selected targets include both `tests/test_select_ci_tests.py` and `tests/test_timescale_write_guard_wire_site_invariant.py`, `meta_guard_only=false`, `collection_smoke_required=true`, and the workflow also runs `pytest tests/ -q --collect-only`

#### Scenario: Selector test change keeps its collapse semantics

- **WHEN** the only changed path is `tests/test_select_ci_tests.py`
- **THEN** `meta_guard_only=true` and `collection_smoke_required=true`, so assertions and full-tree collection both run

#### Scenario: Ordinary non-collapsed selection does not pay collection smoke

- **WHEN** a non-empty selection contains an ordinary target and neither selector-development path changed
- **THEN** `collection_smoke_required=false` and the targeted job does not add the full-tree collection pass

#### Scenario: Existing meta-guard collapse still requires collection

- **WHEN** missing-target filtering or an unrouted support-module change leaves exactly the selector meta-guard suite
- **THEN** both `meta_guard_only=true` and `collection_smoke_required=true`

#### Scenario: Collection consumer cannot be satisfied by inert shell text

- **WHEN** a valid workflow mutation leaves required condition, targeted-test, collect, label, log, or exit tokens only in comments, quoted fragments, no-op/dead branches, or unreachable control-flow positions
- **THEN** the same positive helper used for the live named step reports the corresponding condition, ordering, collection, truthful-label, or fail-closed violation

#### Scenario: Collection failure remains an observable job failure

- **WHEN** `collection_smoke_required=true` and the trusted behavior fixture makes the full-tree collect command fail
- **THEN** the audited named-step program invokes targeted assertions first, invokes full-tree collection, emits the collection log, and returns nonzero

#### Scenario: Workflow payload and metadata cannot bypass the collection oracle

- **WHEN** a valid workflow mutation changes the named step payload, condition, continuation policy, `TARGETED_TESTS_JSON` binding, shell, working directory, or an inherited job/workflow `defaults.run` field
- **THEN** the full-workflow positive helper reports a named identity or effective-metadata violation before any unapproved payload executes

#### Scenario: Collection probe cleans timed-out descendants

- **WHEN** a test-owned controlled fixture starts a descendant and the bounded probe times out
- **THEN** the probe returns a named timeout violation and no process in its new process group remains alive

### Requirement: Irregular source and package routes MUST select their owned suites

The targeted selector SHALL map every tracked module under `workers/mapping_builder/**` to every tracked `tests/test_mapping_builder_*.py` suite, map `packages/common/state_clone_hook.py` to `tests/test_state_clone_cutover_hook.py`, and map `scripts/node22_clone_direct_grid_cutover_states.py` to both state-clone recalibration suites. Variable package sets SHALL be derived from the tracked tree where the naming/domain is stable, while intentionally irregular file-to-suite names remain explicit.

#### Scenario: Mapping-builder package selects all package suites

- **WHEN** any one of the eight tracked `workers/mapping_builder/*.py` modules changes
- **THEN** the output includes all eight tracked `tests/test_mapping_builder_*.py` suites, and the directory importer-gap guard covers the package

#### Scenario: State-clone hook selects its irregular suite

- **WHEN** a PR changes only `packages/common/state_clone_hook.py`
- **THEN** the output includes `tests/test_state_clone_cutover_hook.py`

#### Scenario: Node-22 clone script selects both recalibration suites

- **WHEN** a PR changes only `scripts/node22_clone_direct_grid_cutover_states.py`
- **THEN** the output includes `tests/test_state_clone_recalibration.py` and `tests/test_state_clone_recalibration_cli.py`

### Requirement: Integration-owned production sources MUST trigger real-database CI

The CI `database` paths filter SHALL match every production source surface in the finite integration-trigger registry defined by this change: `packages/common/forecast_store.py`, `packages/common/display_coverage.py`, `services/tiles/mvt.py`, `apps/api/routes/hydro_display.py`, `apps/api/main.py`, `scripts/node27_autopipeline.py`, `workers/output_parser/parser.py`, `packages/common/timescale_write_guard.py`, `packages/common/object_store.py`, `packages/common/model_registry.py`, `packages/common/grid_registry_store.py`, `workers/grid_registry/**`, `workers/model_registry/**`, `workers/forcing_producer/**`, and `services/orchestrator/scheduler.py`. The selector contract suite SHALL parse the `database:` filter and mechanically assert that each registered path or tracked member of a registered root matches at least one filter pattern; a workflow change SHALL self-select that contract suite. Matching the filter SHALL open the existing `real-db-integration` job, which runs the full `-m integration` suite with its PostgreSQL/Timescale service and dedicated `NHMS_INTEGRATION_DATABASE_URL`. The job SHALL expose node-level pass/skip evidence with pytest `-vv -rs`; this verbosity-only evidence change SHALL NOT alter its marker expression, dedicated DSN, service, job gate, or suite selection. One full-workflow positive job-contract helper SHALL validate those properties and the named step's effective execution context: step-level environment SHALL NOT replace either integration gate variable, a step condition SHALL NOT disable the command, step/job error-continuation policy SHALL NOT make a failing integration command non-blocking, and direct or inherited custom shell/working-directory metadata SHALL NOT alter the audited root-checkout command semantics. Deletion or effective blanking/opt-out of the dedicated integration context SHALL produce a named contract violation rather than a green job whose required nodes all skip.

#### Scenario: Forecast-store-only diff opens the parity oracle lane

- **WHEN** a non-draft PR changes only `packages/common/forecast_store.py`
- **THEN** the `changes` job reports `database=true`, `SQL Migration Dry Run` runs, and all seven tests in `tests/test_display_coverage_residual_debt_integration.py` appear as executed `PASSED` node IDs rather than skips

#### Scenario: Integration source registry and filter cannot drift silently

- **WHEN** any registered source path no longer matches a `database` pattern, including a mutation that removes the `packages/common/forecast_store.py` pattern
- **THEN** `tests/test_select_ci_tests.py` fails and names the uncovered source

#### Scenario: Dedicated integration DSN cannot disappear silently

- **WHEN** the `real-db-integration` workflow block loses `NHMS_INTEGRATION_DATABASE_URL` while retaining generic `DATABASE_URL`, `NHMS_RUN_INTEGRATION`, and `pytest -vv -rs -m integration`
- **THEN** the same positive job-contract helper used for the live workflow reports a violation naming `NHMS_INTEGRATION_DATABASE_URL`, because the integration fixture ignores generic `DATABASE_URL` without an explicit compatibility flag

#### Scenario: Named integration step cannot override or bypass its job contract

- **WHEN** a valid workflow mutation adds a step-level opt-out or blank dedicated DSN, disables the named integration step with its `if`, enables step/job `continue-on-error`, or sets direct/inherited custom shell or working-directory metadata
- **THEN** the same full-workflow structured positive helper used for the live job reports a named effective-environment, execution-condition, fail-closed-policy, shell, or working-directory violation

#### Scenario: Workflow changes execute the trigger contract

- **WHEN** `.github/workflows/ci.yml` changes
- **THEN** targeted selection includes `tests/test_select_ci_tests.py`, so the database-filter and real-DB job contracts execute on that PR

#### Scenario: Existing CI lanes retain their contracts

- **WHEN** the database filter gains the finite integration-owned source patterns and the real-DB pytest command gains `-vv -rs`
- **THEN** `unit-test-targeted`, master `unit-test`, frontend/docs/openapi/schema filters, draft gating, the integration marker expression, dedicated integration DSN, PostgreSQL/Timescale service, real-DB job gate, and suite selection remain unchanged

### Requirement: Changed test suites MUST select their non-gated top-level importer suites

When a changed Python test suite reaches the targeted selector's ordinary self-selection branch, the selector SHALL select the changed suite, the selector meta-guard suite, and every other repository test suite that imports the changed suite's dotted module at module scope and has no file-level `integration` or `e2e` gate. The importer set SHALL be a mechanically derived, direct one-hop closure over the repository tree supplied to the selector, never a frozen filename snapshot. Function-local imports and importers reached only through another importer SHALL remain outside this closure. Existing explicit changed-suite redirects and non-suite support-module routing SHALL retain their current semantics.

#### Scenario: Changed suite selects its current direct importers

- **WHEN** `tests/test_real_slurm_gateway.py` or `tests/test_production_scheduler.py` reaches ordinary changed-suite selection
- **THEN** the output contains the owner, selector meta-guard, and every current non-gated suite that imports that owner at module scope, including all five current direct importers of `tests/test_production_scheduler.py`

#### Scenario: New module-scope edge automatically joins the closure

- **WHEN** a repository test tree gains a non-gated suite with a module-scope `import tests.test_owner`, `from tests.test_owner import helper`, or `from tests import test_owner` edge and `tests/test_owner.py` changes
- **THEN** selection includes the new importer without adding its filename to a routing table

#### Scenario: Malformed suite source fails closure selection loudly

- **WHEN** an ordinary changed suite requires the importer closure and a discovered test-suite file under the supplied repository root contains unparsable Python
- **THEN** targeted selection fails with the parse error instead of returning a partial importer index that silently omits the suite

#### Scenario: Function-local and transitive edges stay excluded

- **WHEN** one suite imports `tests.test_owner` only inside a function, or imports another suite that itself imports `tests.test_owner`
- **THEN** neither edge alone adds that suite to the owner's direct importer closure

#### Scenario: File-level gated importers stay outside the pull-request lane

- **WHEN** a suite importing the changed owner at module scope has a file-level `integration` or `e2e` marker
- **THEN** the selector does not add that gated suite through the importer closure

#### Scenario: Existing changed-suite selection semantics remain intact

- **WHEN** an ordinary changed suite has zero or more derived importers
- **THEN** its own path and `tests/test_select_ci_tests.py` remain selected and GitHub output reports `meta_guard_only=false`
- **WHEN** a changed suite matches an existing explicit redirect rule
- **THEN** the selector preserves that redirect's focused target set plus the selector meta-guard instead of applying the ordinary importer closure
