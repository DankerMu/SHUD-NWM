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

