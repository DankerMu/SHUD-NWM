## MODIFIED Requirements

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
