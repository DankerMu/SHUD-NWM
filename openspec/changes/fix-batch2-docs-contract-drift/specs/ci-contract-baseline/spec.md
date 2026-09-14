## MODIFIED Requirements

### Requirement: Targeted CI selection MUST include the container contract's dependent suites

A change to `packages/common/node27_container_contract.py` SHALL
select every test suite in its dependent closure — the tracked test
files that import the contract directly in either spelling form, plus
test files that import a `scripts/` module whose scripts-import graph
reaches the contract (computed to a fixed point, not one hop) —
instead of falling through to the core-smoke fallback (the core-smoke
suites it additionally selects are the shared-library baseline retained by
policy under "Shared-library targeted selection MUST retain its baseline",
not a fallback leak), and a
meta-guard SHALL derive that closure from import analysis so that
closure growth reddens the guard rather than silently unselecting new
dependents.

#### Scenario: A contract-only diff selects the dependent closure

- **WHEN** the changed-file list contains only
  `packages/common/node27_container_contract.py`
- **THEN** the selected tests are a superset of the contract's
  dependent closure (currently the five node27 timeseries compression
  benchmark/capture/supervisor/live-evidence and decompression-replay
  suites)
- **AND** the selected tests also contain every `CORE_SMOKE_TESTS` member,
  because `packages/common/**` Python sources retain the core-smoke
  baseline by policy (#1744 path B), and the selector reports
  `collection_smoke_required=false`

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
meta-guard suite (`meta_guard_only` — a property of the final
selection's shape only: it fires for a PR whose only backend change is
`tests/test_select_ci_tests.py`, but not for a PR that changes
`scripts/select_ci_tests.py`, whose supplemental routing selects more
than the meta-guard suite; that PR keeps the full-tree smoke through
`collection_smoke_required` under "Selector-development changes MUST
retain full-tree collection smoke"), the selector SHALL expose the collapse as a
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
  `tests/test_select_ci_tests.py`, so the diff-specific selection IS
  exactly the meta-guard suite
- **THEN** `meta_guard_only=true` and the collect-only smoke also runs
  — accepted by design (one extra collection pass on exactly the PR
  class that changes the gate), and the smoke labeling does not claim
  the run executed zero assertions
- **AND** when the diff's only backend change is instead
  `scripts/select_ci_tests.py`, the selection is not collapsed: it
  also contains the supplemental invariant suites routed from
  `scripts/**` (the selection includes `tests/test_select_ci_tests.py`
  plus supplemental invariant suites such as
  `tests/test_river_segment_write_surface_scan.py` and
  `tests/test_timescale_write_guard_wire_site_invariant.py`), so
  `meta_guard_only=false` as "non-collapsed selections suppress the
  flag" requires, while `collection_smoke_required=true` still runs
  the collect-only smoke
