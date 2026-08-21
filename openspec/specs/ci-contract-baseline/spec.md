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

The targeted-test selector SHALL map every changed `scripts/**/*.py` whose
same-name test file `tests/test_<basename>.py` exists to that test file,
treating the hit as a known mapping that suppresses the unknown-backend
smoke fallback for that path; the derivation applies to `scripts/**/*.py`
only (other backend prefixes keep today's behavior even when a same-name
test exists), a script with neither an explicit rule nor a same-name test
keeps the existing fallback behavior, and mapping completeness over the
tracked tree is enforced by a mechanized selector test rather than a
hand-maintained rule list.

#### Scenario: changed script selects its own suite

- **WHEN** a PR changes only `scripts/<name>.py` and
  `tests/test_<name>.py` exists in the tree
- **THEN** the selector output includes `tests/test_<name>.py` and does not
  substitute the unrelated core-smoke set for that path

#### Scenario: completeness is mechanized

- **WHEN** a new script is added with a same-name test but no explicit
  selector rule
- **THEN** the selector already reaches the test via the same-name mapping,
  and the completeness guard test derives the pair list from the tracked
  tree and asserts each pair's selection both includes the same-name test
  and shares no member with the core-smoke set, so a future orphan pair —
  or a mapping that still drags the smoke set along — fails the selector
  suite

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

