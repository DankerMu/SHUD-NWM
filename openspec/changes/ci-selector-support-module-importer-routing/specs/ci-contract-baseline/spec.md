# ci-contract-baseline delta

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
  `scripts/**/*.sh`)
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
suites"), or to exactly the selector meta-guard suite otherwise, so
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
  and exactly `tests/test_select_ci_tests.py` for the rest (including
  `tests/conftest.py`) — and a tree-derived invariant test covers
  every current and future support module without hardcoding names

#### Scenario: nested test suites are suites, not support files

- **WHEN** a PR adds or changes a nested `tests/<pkg>/test_*.py` suite
- **THEN** the suite self-selects (its assertions run in the PR lane)
  and the selector meta-guards accumulate, identically to a top-level
  `tests/test_*.py` change

## ADDED Requirements

### Requirement: Support-module changes MUST select their non-gated importer suites

For every tracked non-suite Python module under `tests/` (classified by the selector's canonical `is_test_suite_path` predicate), the targeted-test selector SHALL select the module's derived non-gated top-level importer suites plus the selector meta-guard suite (derivation authority: the selector test suite's mechanical importer derivation over the tracked tree, including its deliberate package-`__init__`-to-package aliasing, never a frozen list), and a selector-suite closure guard SHALL fail naming the module and missing suite whenever a derived importer suite is absent from the selection — with exactly two carve-outs, both guard-checked: modules on the explicit issue-scope carve-out allowlist (`tests/integration_helpers.py`, `tests/conftest.py` — recorded with their measured partial external coverage, not claimed as full compensation) are exempt only while each allowlisted path appears verbatim inside the `database:` paths-filter block of `.github/workflows/ci.yml`, and modules deriving zero importer suites SHALL keep selecting exactly the selector meta-guard suite, so a support-module-only PR runs assertion-level targets whenever assertion-level coverage exists in the tree.

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
  top level is added to the tree without extending the module's rule
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

#### Scenario: zero-importer support modules keep the collapse route

- **WHEN** the changed path is a tracked non-suite `tests/` module
  with no derived non-gated importer suite (e.g.
  `tests/mock_shud_omp.py`,
  `tests/fixtures/mapping_builder/keliya/build.py`)
- **THEN** the selection is exactly the selector meta-guard suite,
  preserving the meta-guard-only collapse and its full-tree
  collect-only smoke
