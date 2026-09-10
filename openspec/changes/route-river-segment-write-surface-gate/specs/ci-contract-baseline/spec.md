# ci-contract-baseline delta

## ADDED Requirements

### Requirement: the river-segment write-surface scan MUST be routed to every production Python path it scans

`scripts/select_ci_tests.py` SHALL route every changed `.py` path under
`apps/`, `services/`, `workers/`, `packages/` or `scripts/` to
`tests/test_river_segment_write_surface_scan.py`, in addition to that path's
ordinary selection. Before this change no production path selected the scan at
all: measured over the 3653 tracked non-test paths at `c21bacf9`, the count
selecting it was zero, including
`workers/model_registry/basins_registry_import.py`, which holds the only
`UPDATE core.river_segment` in production code, and
`workers/model_registry/qhh_production_bootstrap.py`, which holds the only
`INSERT ... ON CONFLICT DO UPDATE` on that table. The gap is not reachable by
either of the selector's structural auto-derivations, because the scan imports
only `ast`, `re`, `dataclasses` and `pathlib` (no production module to close
over) and no production module shares its name. Because the selection of such a
diff is non-empty, the zero-assertion `--collect-only` degradation does not fire
and the missing guard is silent.

The routing SHALL be supplemental — set union only. It SHALL NOT assign
`matched`, SHALL NOT participate in `stop_on_match`, and SHALL NOT change
whether a path counts as known for the unknown-backend fallback, so that no
existing rule's targets can shrink and no path can lose its fallback smoke
coverage. The root globs SHALL be held in a single selector constant naming all
five roots, and that constant SHALL be the live routing authority rather than a
description of it: replacing it at runtime SHALL change what the selector
returns. The routing SHALL NOT be gated on the backend-prefix classification —
the scan walks `*.py` under those roots regardless of that classification, so
the root match is the only gate.

The five roots SHALL equal the scan's own `PRODUCTION_DIRS` mapped to globs,
`packages/**` included at full width. `packages/` holds `packages/scheduler/`
and a top-level `packages/__init__.py` besides `packages/common/`, and the
scan's non-vacuity assertion pins the set of scanned top-level directories to
`set(PRODUCTION_DIRS)`, so a SQL literal under `packages/scheduler/` is inside
the invariant's blast radius. Narrowing this root to `packages/common/**` — the
width the sibling `TIMESCALE_WRITE_GUARD_INVARIANT_ROOTS` uses for its own
different scan — SHALL be rejected.

For the same reason `apps/**` SHALL be routed at full width, `apps/frontend/`
and the top-level `apps/__init__.py` included, even though only `apps/api/` is
a backend-source prefix. Excluding the non-prefix part of `apps/` would break
the root set's derivation from the scan's `PRODUCTION_DIRS` — the only
mechanism that makes a newly scanned directory red by name — while being untrue
to a scan that really does parse every `.py` under `apps/`. The consequence for
the empty-selection contract is carried by the modified requirement below.

#### Scenario: each of the three named write-surface modules selects the scan

- **WHEN** the changed paths are exactly one of `workers/model_registry/basins_registry_import.py`, `workers/model_registry/qhh_production_bootstrap.py` or `packages/common/model_registry.py`
- **THEN** the returned selection contains `tests/test_river_segment_write_surface_scan.py`, and contains every target that path selected before this change

#### Scenario: a future path under any of the five roots selects the scan without an at-site rule

- **WHEN** the changed paths are exactly one of `apps/brand_new_thing.py`, `services/brand_new_thing.py`, `workers/brand_new_thing.py`, `packages/brand_new_thing.py` or `scripts/brand_new_thing.py`
- **THEN** the returned selection contains `tests/test_river_segment_write_surface_scan.py`

#### Scenario: the root constant is the live routing authority

- **WHEN** the selector's root constant is replaced at runtime by the same tuple minus one root, and a path under that removed root is selected
- **THEN** the returned selection no longer contains `tests/test_river_segment_write_surface_scan.py`, while a path under a retained root still does — so an implementation that spells the roots inline in the routing loop and leaves the constant as decoration is rejected

#### Scenario: a non-Python path under a scanned root does not select the scan

- **WHEN** the changed paths are exactly `apps/frontend/src/main.ts`
- **THEN** the returned selection does not contain `tests/test_river_segment_write_surface_scan.py`

#### Scenario: a Python path outside the scanned roots does not select the scan

- **WHEN** the changed paths are exactly `openspec/tools/x.py` or `db/brand_new_thing.py`
- **THEN** the returned selection does not contain `tests/test_river_segment_write_surface_scan.py`, and `db/brand_new_thing.py` still selects `tests/test_timescale_write_guard_wire_site_invariant.py` through the sibling supplemental route

#### Scenario: the supplemental route is gain-only across the whole tracked tree

- **WHEN** `select_tests` is run once per tracked non-test path, before and after this change
- **THEN** the set of paths that gain `tests/test_river_segment_write_surface_scan.py` is exactly the set of tracked non-test `.py` paths under the five roots, no path loses any target, and no path shows any other difference

#### Scenario: the unknown-backend fallback and the sibling supplemental route are unaffected

- **WHEN** the changed paths are exactly `services/brand_new_thing.py`
- **THEN** the selection still contains all of `CORE_SMOKE_TESTS` and still does not contain `tests/test_timescale_write_guard_wire_site_invariant.py`, whose supplemental route stays scoped to its own four roots

### Requirement: the routed root set MUST be derived from the scan's own source, not frozen a second time

`tests/test_select_ci_tests.py` SHALL derive the REQUIRED root set by parsing
`tests/test_river_segment_write_surface_scan.py` and reading its module-level
`PRODUCTION_DIRS` binding, then asserting that the selector's root constant
equals that set mapped to `<dir>/**` globs. A second hand-copied list of roots
in the meta-guard SHALL be rejected: the point of the derivation is that adding
a sixth directory to the scan without wiring it into the selector reddens by
name.

The derivation SHALL collect EVERY module-level binding of `PRODUCTION_DIRS`
and SHALL assert that there is exactly one, rather than taking the first match.
A first-match read is silently wrong under legal rewrites: a second module-level
binding such as `PRODUCTION_DIRS = PRODUCTION_DIRS + ("db",)` leaves the
derivation returning the stale five-element tuple, which still agrees with the
selector constant, so nothing reds while the scan really walks six directories.
The scan's own non-vacuity assertion cannot catch that case either, because at
runtime `PRODUCTION_DIRS` and the scanned set still agree — only the AST
first-match view is stale. The derivation SHALL accept both `ast.Assign` and
`ast.AnnAssign` targets and both `ast.Tuple` and `ast.List` values, since the
house style annotates such constants, and SHALL assert loudly, naming
`PRODUCTION_DIRS`, on any other shape — so a rewrite the derivation cannot read
fails rather than yielding an empty required set that every later assertion
would vacuously satisfy.

This is deliberately NOT the derivation shape #1656 uses for the timescale
write-guard invariant: that suite exposes a `_scan_roots` FunctionDef returning
a tuple of `REPO_ROOT / <part>` `BinOp` chains, and its
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

- **WHEN** the scan's `PRODUCTION_DIRS` binding is parsed and each entry mapped to `<dir>/**`
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

#### Scenario: an unreadable PRODUCTION_DIRS fails loudly rather than deriving nothing

- **WHEN** a copy of the scan no longer binds `PRODUCTION_DIRS` at module level to a tuple or list of string constants
- **THEN** the derivation raises an assertion naming `PRODUCTION_DIRS`, rather than returning an empty root set

#### Scenario: the suite-path literal is anchored to the selector constant

- **WHEN** the meta-guard's local scan-path literal is compared with `RIVER_SEGMENT_WRITE_SURFACE_TEST`
- **THEN** they are equal and the path is an existing file

## MODIFIED Requirements

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

The pinned empty-selection class "`.py` outside the five backend prefixes"
is narrowed by the river-segment write-surface routing. `BACKEND_PYTHON_SOURCE_PREFIXES`
is `apps/api/`, `packages/`, `services/`, `workers/`, `scripts/`, so before that
routing every `.py` under `apps/` that was not under `apps/api/` fell in this
class. Those paths are inside the write-surface scan's `PRODUCTION_DIRS`, so
they now select that one suite and are no longer empty. The remaining members of
the class are the `.py` paths that are under neither a backend prefix nor a
scanned root — `openspec/**` today. This is the route-A selector-widening the
class was explicitly left open for, and it is a real change today, not only for
future paths: `apps/__init__.py` is a tracked file that moves from an empty
selection to exactly the write-surface scan, losing the zero-assertion
full-tree collect-only smoke it used to receive and gaining four executed
assertions instead. The mechanism is the selector's `count` output: the job's
collect-only branch is guarded by `count == 0`, so a one-element selection
takes the targeted branch. Neither carve-out re-arms the smoke — `meta_guard_only`
fires only for a selection that is exactly the selector meta-guard suite, and
`collection_smoke_required` is false for this class both before and after,
since neither the selector source nor its suite is in the diff.

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
  (`schemas/**`, unmapped `infra/**`, `.py` under neither a backend prefix
  nor one of the write-surface scan's roots, non-`.py` under backend
  prefixes, non-`.py` under `tests/`, `.sh` files outside `scripts/`;
  `scripts/**/*.sh` left this list when it joined the backend gate — an
  unmapped one now arms the core-smoke fallback)
- **THEN** the selector returns an empty selection and the selector test
  suite pins each class explicitly as the route-C contract, so any future
  route-A/B policy change must flip a visible assertion

#### Scenario: a Python path under apps but outside apps/api leaves the pinned-empty class

- **WHEN** the changed paths are exactly `apps/__init__.py`, a tracked file, or `apps/frontend/scripts/gen.py`, a future-shaped one
- **THEN** the returned selection is exactly `["tests/test_river_segment_write_surface_scan.py"]`, the selector's GitHub output reports a count of 1 with `meta_guard_only=false`, and neither path appears among the pinned empty-selection classes

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
