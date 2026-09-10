# ci-contract-baseline delta

## ADDED Requirements

### Requirement: the river-segment routed root set MUST be derived from the scan's own source, not frozen a second time

`tests/test_select_ci_tests.py` SHALL derive the REQUIRED root set by parsing
`tests/test_river_segment_write_surface_scan.py` and reading its module-level
`PRODUCTION_DIRS` binding, then asserting that the selector's root constant
equals that set with each entry suffixed `/**`. A second hand-copied list of roots
in the meta-guard SHALL be rejected: the point of the derivation is that adding
a sixth directory to the scan without wiring it into the selector reddens by
name.

The derivation SHALL collect EVERY binding of `PRODUCTION_DIRS` anywhere in the
scan module — every `ast.Name` store of that identifier at any nesting depth,
which covers plain assignment, annotated assignment, augmented assignment,
`for` targets, `with ... as`, walrus and comprehension targets alike — and
SHALL assert that there is exactly one, rather than taking the first
module-level match. A first-match read is silently wrong under legal rewrites:
`PRODUCTION_DIRS += ("db",)`, or a module-level `if some_flag: PRODUCTION_DIRS =
(...)`, leaves such a derivation returning the stale five-element tuple, which
still agrees with the selector constant, so nothing reds while the scan really
walks six directories. The scan's own non-vacuity assertion cannot catch those
cases either, because at runtime `PRODUCTION_DIRS` and the scanned set still
agree — only the AST view is stale. The derivation SHALL then require that
single binding to be a module-level `ast.Assign` or `ast.AnnAssign` bound to an
`ast.Tuple` or `ast.List` of string constants, since the house style annotates
such constants, and SHALL assert loudly, naming `PRODUCTION_DIRS`, on any other
shape — so a rewrite the derivation cannot read fails rather than yielding an
empty required set that every later assertion would vacuously satisfy. A rebind
that produces no `ast.Name` store at all (`globals()["PRODUCTION_DIRS"] = ...`)
is out of the derivation's reach. The meta-guard SHALL record that boundary
rather than imply coverage it does not have, and SHALL state it accurately: the
scan's own runtime non-vacuity assertion is a backstop for such a rebind only if
the rebind executes AFTER the scan materializes its literals at
`tests/test_river_segment_write_surface_scan.py:110`. A store-less rebind placed
before that line is walked by the scan itself, so the assertion's two sides read
the same rebound name and agree at six directories exactly as they did at five —
that case is caught by nothing.

This is deliberately NOT the derivation shape #1656 uses for the timescale
write-guard invariant: that suite exposes a `_scan_roots` FunctionDef returning
a tuple of `REPO_ROOT / "workers"` `BinOp` chains, and its
`_invariant_scan_roots()` walker would find nothing here. Only the ROUTING
shape — supplemental, set-union, no `matched`, no stop-rule participation — is
copied from #1656.

The red leg SHALL be a positive oracle: a helper that takes the root set under
test and returns a violation naming each expected root it is missing, where the
expected set comes from the independent scan-source derivation and never from
the production constant under test. Its probe set SHALL be derived from that
same scan-source derivation rather than written as a fixed list of five paths,
so that a sixth scanned directory grows a probe automatically instead of going
untested. A mutant that drops one root SHALL be reported by that SAME helper,
naming the dropped root, and SHALL additionally be shown to change what
`select_tests` returns. A mutant that removes the routing entirely SHALL red an
assertion whose message embeds `tests/test_river_segment_write_surface_scan.py`
as a formatted value, not only as a value pytest happens to render.

#### Scenario: the selector root constant equals the scan's derived roots

- **WHEN** the scan's `PRODUCTION_DIRS` binding is parsed and each entry suffixed `/**`
- **THEN** that set equals `set(RIVER_SEGMENT_WRITE_SURFACE_ROOTS)`

#### Scenario: dropping a root is reported by the positive oracle by name

- **WHEN** the selector's root constant is replaced by the same tuple minus `scripts/**`
- **THEN** the violations helper returns a violation whose text names `scripts/**`, and the live (unreplaced) constant yields no violations

#### Scenario: a sixth scanned directory reds the derivation without a hand-written probe

- **WHEN** a copy of the scan whose `PRODUCTION_DIRS` names a sixth directory is parsed by the derivation, against the unchanged selector constant
- **THEN** the equality assertion reds naming the sixth root, and the derived probe set has grown a probe under it — no fixed five-element probe list is consulted

#### Scenario: two module-level bindings of PRODUCTION_DIRS fail loudly

- **WHEN** a copy of the scan contains a second module-level binding of `PRODUCTION_DIRS`
- **THEN** the derivation raises an assertion naming `PRODUCTION_DIRS`, rather than returning the first binding's stale tuple

#### Scenario: an augmented or nested rebind of PRODUCTION_DIRS fails loudly

- **WHEN** a copy of the scan rebinds `PRODUCTION_DIRS` by `+=`, inside a module-level `if` body, or as a `for` target, in each case after an ordinary module-level binding
- **THEN** the derivation raises an assertion naming `PRODUCTION_DIRS`, rather than returning the ordinary binding's stale tuple — the shapes a `tree.body`-only, `Assign`/`AnnAssign`-only collection would silently accept

#### Scenario: an unreadable PRODUCTION_DIRS fails loudly rather than deriving nothing

- **WHEN** a copy of the scan no longer binds `PRODUCTION_DIRS` at module level to a tuple or list of string constants
- **THEN** the derivation raises an assertion naming `PRODUCTION_DIRS`, rather than returning an empty root set

#### Scenario: the suite-path literal is anchored to the selector constant

- **WHEN** the meta-guard's local scan-path literal is compared with `RIVER_SEGMENT_WRITE_SURFACE_TEST`
- **THEN** they are equal and the path is an existing file

## MODIFIED Requirements

### Requirement: Tree-scanning invariant suites MUST follow every scanned source root

A selector supplemental-rule authority SHALL map every Python source path under
a tree-scanning invariant suite's own scanned roots to that suite, for each of
the two suites named below. It is deliberately NOT a universal claim over
`tests/`: whether any other tree- or import-graph-scanning suite owes such a
route is undecided here, and adding one is a separate change that extends this
requirement rather than a silent obligation this one already imposes.
Supplemental mappings SHALL form a monotonic set union
with ordinary rules, SHALL NOT assign `matched`, SHALL NOT participate in
`stop_on_match`, SHALL NOT stop later rules, and SHALL NOT change whether a
path is known for fallback purposes — so no existing rule's targets can shrink
and no path can lose its fallback smoke coverage. A selector meta-test SHALL
derive each suite's roots from that suite's own source rather than freeze a
second list. Each root SHALL be carried at the width its suite actually scans,
even where that width exceeds the backend-source prefixes: the routing gate is
the root match, not the backend-prefix classification.

Two suites are covered.

`tests/test_timescale_write_guard_wire_site_invariant.py` scans four roots —
`workers/**`, `packages/common/**`, `scripts/**` and `db/**`. It exposes a
`_scan_roots` FunctionDef returning `REPO_ROOT / "workers"` `BinOp` chains, and
the meta-test derives its roots from that definition.

`tests/test_river_segment_write_surface_scan.py` scans five roots — `apps/**`,
`services/**`, `workers/**`, `packages/**` and `scripts/**` — held as a
module-level `PRODUCTION_DIRS` tuple, which is the shape its meta-test derives
from; the derivation contract is fixed by the requirement "the river-segment
routed root set MUST be derived from the scan's own source, not frozen a second
time". Before this route existed no production path selected that scan at all:
measured over the 3653 tracked non-test paths at `c21bacf9`, the count
selecting it was zero, including
`workers/model_registry/basins_registry_import.py`, which holds the only
`UPDATE core.river_segment` in production code, and
`workers/model_registry/qhh_production_bootstrap.py`, which holds the only
`INSERT ... ON CONFLICT DO UPDATE` on that table. The gap was reachable by
neither of the selector's structural auto-derivations, because the scan imports
only `ast`, `re`, `dataclasses` and `pathlib` (no production module to close
over) and no production module shares its name. Because the selection of such a
diff is non-empty, the zero-assertion `--collect-only` degradation did not fire
and the missing guard was silent.

`packages/**` and `apps/**` SHALL be routed at FULL width for that scan.
`packages/` holds `packages/scheduler/` and a top-level `packages/__init__.py`
besides `packages/common/`, and the scan's non-vacuity assertion pins the set of
scanned top-level directories to `set(PRODUCTION_DIRS)`, so a SQL literal under
`packages/scheduler/` is inside the invariant's blast radius; narrowing this
root to the `packages/common/**` width the timescale suite uses for its own
different scan SHALL be rejected. Likewise `apps/frontend/` and the top-level
`apps/__init__.py` lie inside `apps/**` even though only `apps/api/` is a
backend-source prefix: excluding the non-prefix part of `apps/` would break the
root set's derivation from `PRODUCTION_DIRS` — the only mechanism that makes a
newly scanned directory red by name — while being untrue to a scan that really
does parse every `.py` under `apps/`. The consequence of that width for the
empty-selection contract is carried by the requirement "Empty targeted-test
selection MUST be loudly self-identifying".

Because these routes are supplemental and set-union only, they may add a target
to a selection that another requirement pins as exact. A requirement that pins
an exact selection for a path under a scanned root SHALL name the supplemental
target in that pin rather than treat the route as a violation.

#### Scenario: Existing writer and guard sources select the invariant

- **WHEN** a PR changes only `workers/output_parser/parser.py`, `workers/forcing_producer/store.py`, `packages/common/forcing_domain_handoff_apply.py`, or `packages/common/timescale_write_guard.py`
- **THEN** the selector output includes `tests/test_timescale_write_guard_wire_site_invariant.py` in addition to each source's ordinary selection

#### Scenario: Future file under a scanned root is covered

- **WHEN** the changed-file input names a previously nonexistent Python path under each scanned root, such as `scripts/brand_new_thing.py`
- **THEN** the selector output includes the invariant suite without requiring an at-site rule

#### Scenario: Root or supplemental rule drift is caught

- **WHEN** a scanned root is added or a supplemental mapping is deleted or narrowed
- **THEN** the selector meta-test fails and names the uncovered root or source

#### Scenario: every module holding a river-segment write surface selects the scan

- **WHEN** the changed paths are exactly one of `workers/model_registry/basins_registry_import.py`, `workers/model_registry/qhh_production_bootstrap.py` or `packages/common/model_registry.py`
- **THEN** the returned selection contains `tests/test_river_segment_write_surface_scan.py`, and contains every target that path selected before this change

#### Scenario: a future path under any of the five river-segment roots selects the scan without an at-site rule

- **WHEN** the changed paths are exactly one of `apps/brand_new_thing.py`, `services/brand_new_thing.py`, `workers/brand_new_thing.py`, `packages/brand_new_thing.py` or `scripts/brand_new_thing.py`
- **THEN** the returned selection contains `tests/test_river_segment_write_surface_scan.py`

#### Scenario: the river-segment root constant is the live routing authority

- **WHEN** the selector's root constant is replaced at runtime by the same tuple minus one root, and a path under that removed root is selected
- **THEN** the returned selection no longer contains `tests/test_river_segment_write_surface_scan.py`, while a path under a retained root still does — so an implementation that spells the roots inline in the routing loop and leaves the constant as decoration is rejected

#### Scenario: a non-Python path under a scanned root does not select the river-segment scan

- **WHEN** the changed paths are exactly `apps/frontend/src/main.ts`
- **THEN** the returned selection does not contain `tests/test_river_segment_write_surface_scan.py`

#### Scenario: a Python path outside the river-segment roots does not select that scan

- **WHEN** the changed paths are exactly `openspec/tools/x.py` or `db/brand_new_thing.py`
- **THEN** the returned selection does not contain `tests/test_river_segment_write_surface_scan.py`, and `db/brand_new_thing.py` still selects `tests/test_timescale_write_guard_wire_site_invariant.py` through the sibling supplemental route

#### Scenario: the river-segment route is gain-only across the whole tracked tree

- **WHEN** `select_tests` is run once per tracked non-test path, before and after this change
- **THEN** the set of paths that gain `tests/test_river_segment_write_surface_scan.py` is exactly the set of tracked non-test `.py` paths under the five roots, no path loses any target, and no path shows any other difference

#### Scenario: the unknown-backend fallback and the sibling supplemental route are unaffected

- **WHEN** the changed paths are exactly `services/brand_new_thing.py`
- **THEN** the selection still contains all of `CORE_SMOKE_TESTS` and still does not contain `tests/test_timescale_write_guard_wire_site_invariant.py`, whose supplemental route stays scoped to its own four roots

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

The pinned empty-selection class "`.py` outside the five backend prefixes" is
narrowed by the river-segment write-surface routing. `BACKEND_PYTHON_SOURCE_PREFIXES`
is `apps/api/`, `packages/`, `services/`, `workers/`, `scripts/`, so before that
routing every `.py` under `apps/` that was not under `apps/api/` fell in this
class. Those paths are inside the write-surface scan's `PRODUCTION_DIRS`, so
they now select that one suite and are no longer empty. The class SHALL
therefore be respelled as the `.py` paths that are under NONE of: a backend
prefix, the write-surface scan's five roots, or the timescale write-guard
invariant's four roots — the last of which matters because `db/**` is neither a
backend prefix nor a write-surface root, yet a `.py` path under it selects the
timescale invariant and the migration suite, so a two-clause spelling would
wrongly claim `db/x.py` is empty. Its tracked members today are the `.py` paths under
`openspec/**` and `.agents/**`; a `.py` under `docs/**`, `.github/**` or an
unmapped `infra/**` would join them, and none is tracked today. This is the route-A
selector-widening the class was explicitly left open for, and it is a real
change today, not only for future paths: `apps/__init__.py` is a tracked file
that moves from an empty selection to exactly the write-surface scan, losing the
zero-assertion full-tree collect-only smoke it used to receive and gaining an
assertion-executing targeted run instead. The mechanism is the selector's
`count` output: the job's collect-only branch is guarded by `count == 0`, so a
one-element selection takes the targeted branch. Neither carve-out re-arms the
smoke — `meta_guard_only` fires only for a selection that is exactly the
selector meta-guard suite, and `collection_smoke_required` is false for this
class both before and after, since neither the selector source nor its suite is
in the diff.

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
  (`schemas/**`, unmapped `infra/**`, `.py` under none of the backend
  prefixes, the write-surface scan's roots and the timescale invariant's
  roots, non-`.py` under backend prefixes, non-`.py` under `tests/`,
  `.sh` files outside `scripts/`; `scripts/**/*.sh` left this list when it
  joined the backend gate — an unmapped one now arms the core-smoke fallback)
- **THEN** the selector returns an empty selection and the selector test
  suite pins each class explicitly as the route-C contract, so any future
  route-A/B policy change must flip a visible assertion

#### Scenario: a Python path under apps but outside apps/api leaves the pinned-empty class

- **WHEN** the changed paths are exactly `apps/__init__.py`, a tracked file, or `apps/frontend/scripts/gen.py`, a future-shaped one
- **THEN** the returned selection is exactly `["tests/test_river_segment_write_surface_scan.py"]`, the selector's GitHub output reports a count of 1 with `meta_guard_only=false`, and neither path appears among the pinned empty-selection classes

#### Scenario: a Python path under db stays out of the pinned-empty class

- **WHEN** the changed paths are exactly `db/brand_new_thing.py`
- **THEN** the returned selection is non-empty — it contains `tests/test_timescale_write_guard_wire_site_invariant.py` — and does not contain `tests/test_river_segment_write_surface_scan.py`

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

### Requirement: precip service tree changes MUST select the prewarm reader suite

`scripts/node27_mvt_prewarm.py` imports `services.precip.mirror.horizon_valid_times` at module level and `tests/test_node27_mvt_prewarm.py` imports that script at module level, so the prewarm suite is a one-hop importer suite of the `services/precip/**` tree whose valid-time grid assertions discriminate on `PRECIP_STEP_HOURS`. The `services/precip/**` `PathTestRule` in `scripts/select_ci_tests.py` SHALL target `tests/test_node27_mvt_prewarm.py` in addition to the four `PRECIP_SURFACE_TESTS` suites, by widening that rule's own target tuple only (neither `stop_on_match` nor `only_when_any_changed`). The shared `PRECIP_SURFACE_TESTS` tuple and the `apps/api/routes/precip.py` rule SHALL remain unchanged, so a route-only diff SHALL NOT select the prewarm suite. `tests/test_select_ci_tests.py` SHALL pin both outcomes with explicit literal expected lists rather than by referencing `PRECIP_SURFACE_TESTS`. Both pinned selections additionally carry `tests/test_river_segment_write_surface_scan.py`, contributed by the supplemental tree-scanning route under the requirement "Tree-scanning invariant suites MUST follow every scanned source root"; that target is named in each exact list below rather than treated as drift, and it is not routed by this requirement's rule.

#### Scenario: a precip tree module diff selects the prewarm reader suite

- **WHEN** the changed paths are exactly `services/precip/constants.py` or exactly `services/precip/mirror.py`
- **THEN** `select_tests` emits exactly `["tests/test_api_contract.py", "tests/test_node27_mvt_prewarm.py", "tests/test_openapi_31_contract.py", "tests/test_openapi_drift.py", "tests/test_precip_overlay.py", "tests/test_river_segment_write_surface_scan.py"]`

#### Scenario: a precip route-only diff keeps its existing selection

- **WHEN** the changed paths are exactly `apps/api/routes/precip.py`
- **THEN** `select_tests` emits exactly `["tests/test_api.py", "tests/test_api_contract.py", "tests/test_monitoring_api.py", "tests/test_openapi_31_contract.py", "tests/test_openapi_drift.py", "tests/test_precip_overlay.py", "tests/test_river_segment_write_surface_scan.py"]`, which contains no prewarm suite

### Requirement: the precipitation application-composition owners MUST select the precipitation surface oracles

`apps/api/route_registry.py` imports `precip_router` and lists it in `_BUSINESS_ROUTERS`, which `register_role_aware_routes` walks to register every business router; dropping that entry turns both published precipitation endpoints — `/api/v1/precip/{source}/{cycle}/index` and `/api/v1/precip/{source}/{cycle}/{valid_time}.png` — into 404. `apps/api/main.py` calls `_patch_precip_openapi(schema)` inside `_patch_openapi_schema`; dropping that call makes the runtime schema drift from the committed `openapi/nhms.v1.yaml`. Before this change neither owner selected `tests/test_precip_overlay.py` or `tests/test_openapi_drift.py`: the registry selected only the two connection-attribution suites plus the three broad `apps/api/**` suites, and `main.py` only the API error-logging suite plus those same three. Both selections were non-empty and plausible, so the zero-assertion CI warning did not fire and a composition-owner-only diff reached the targeted lane with no precipitation oracle executed. Both owners SHALL therefore reach the precipitation surface oracles, on the terms fixed below.

`scripts/select_ci_tests.py` SHALL route both composition owners to the full set of precipitation surface suites — `tests/test_precip_overlay.py`, `tests/test_openapi_drift.py`, `tests/test_openapi_31_contract.py` and `tests/test_api_contract.py` — while preserving each owner's existing riders: the registry SHALL keep both connection-attribution suites and `main.py` SHALL keep `tests/test_api_errors_logging.py`, and both SHALL keep the three broad `apps/api/**` suites. Because a duplicate pattern splits a module's ownership across two rules, `apps/api/route_registry.py` SHALL be removed from the shared connection-attribution path tuple and given a single path-exact rule whose targets merge both suite sets, exactly as `apps/api/routes/forecast.py` was handled; the comment above that tuple SHALL be corrected so it no longer claims the registry is a member. Neither owner rule SHALL carry `stop_on_match` or `only_when_any_changed`. Both flags are inert for these two entries today: `apps/api/**` is an earlier rule whose three suites have already accumulated by the time these trailing entries are reached, and `only_when_any_changed` is consulted only in the `CHANGED_TEST_FILE_RULES` loop, never for `PATH_TEST_RULES`. The pin is therefore structural rather than behavioural — it keeps a future `stop_on_match` from shadowing a rule appended after these two whose pattern also matches these paths, and keeps a field that would silently do nothing from being added here. The selections of the five route paths remaining in that tuple, of the three store paths in the sibling connection-attribution store tuple, and of `apps/api/routes/precip.py`, `services/precip/cache.py`, `apps/api/openapi_patching.py` and `apps/api/errors.py`, SHALL remain unchanged apart from targets contributed by the supplemental tree-scanning routes under the requirement "Tree-scanning invariant suites MUST follow every scanned source root" — every one of those paths lies under a scanned root, so each now additionally carries `tests/test_river_segment_write_surface_scan.py`. No rule owned by THIS requirement SHALL change to add or remove that target.

`tests/test_select_ci_tests.py` SHALL pin each owner's selection as an exact set written as literal test-file strings, and SHALL NOT derive the expectation from the production `PRECIP_SURFACE_TESTS` or `CONNECTION_ATTRIBUTION_TESTS` constants, so that an edit to either constant cannot move production and expectation together. It SHALL additionally carry, per owner, a reverse-missing assertion that `tests/test_precip_overlay.py`, `tests/test_openapi_drift.py` and `tests/test_openapi_31_contract.py` disappear when that owner's precipitation targets are removed. The fourth routed suite, `tests/test_api_contract.py`, is deliberately excluded from that assertion: it is also a rider of the broad `apps/api/**` rule, so it survives the removal and an assertion naming all four would fail. `tests/test_river_segment_write_surface_scan.py` is excluded from it for the same reason — it is contributed by a supplemental route the owner rules do not own. The anti-self-certification constraint is that every element of an expected set is a literal string and no value is read back from `scripts/select_ci_tests` — including `PATH_TEST_RULES` and single-suite constants such as `API_ERROR_LOGGING_TEST`; reading `PATH_TEST_RULES` solely to construct the monkeypatched mutant for a reverse-missing assertion is permitted.

#### Scenario: a route-registry diff selects the precipitation oracles and keeps its attribution riders

- **WHEN** the changed paths are exactly `apps/api/route_registry.py`
- **THEN** `select_tests` emits exactly `["tests/test_api.py", "tests/test_api_contract.py", "tests/test_monitoring_api.py", "tests/test_node27_connection_attribution.py", "tests/test_node27_connection_attribution_delegated.py", "tests/test_openapi_31_contract.py", "tests/test_openapi_drift.py", "tests/test_precip_overlay.py", "tests/test_river_segment_write_surface_scan.py"]`

#### Scenario: a main.py diff selects the precipitation oracles and keeps its error-logging rider

- **WHEN** the changed paths are exactly `apps/api/main.py`
- **THEN** `select_tests` emits exactly `["tests/test_api.py", "tests/test_api_contract.py", "tests/test_api_errors_logging.py", "tests/test_monitoring_api.py", "tests/test_openapi_31_contract.py", "tests/test_openapi_drift.py", "tests/test_precip_overlay.py", "tests/test_river_segment_write_surface_scan.py"]`

#### Scenario: the shared attribution path tuple keeps its other members unchanged

- **WHEN** the changed paths are exactly any one of `apps/api/routes/best_available.py`, `apps/api/routes/data_sources.py`, `apps/api/routes/models.py`, `apps/api/routes/pipeline.py` or `apps/api/routes/state_snapshots.py`
- **THEN** the selection still contains both connection-attribution suites and contains none of `tests/test_precip_overlay.py`, `tests/test_openapi_drift.py` or `tests/test_openapi_31_contract.py` (`tests/test_api_contract.py` is excluded because the broad `apps/api/**` rule supplies it to every path under `apps/api/`)

#### Scenario: neither owner rule carries a selection flag

- **WHEN** the `PATH_TEST_RULES` entries for `apps/api/route_registry.py` and `apps/api/main.py` are inspected
- **THEN** each has `stop_on_match` false and `only_when_any_changed` empty
