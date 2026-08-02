# Spec Delta: ci-contract-baseline

## ADDED Requirements

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
