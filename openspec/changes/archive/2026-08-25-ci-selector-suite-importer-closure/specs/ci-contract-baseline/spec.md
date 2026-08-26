## ADDED Requirements

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
