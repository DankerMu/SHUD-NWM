# Spec Delta: scheduler-registry-refresh

## ADDED Requirements

### Requirement: Registry identity-field equality SHALL treat symmetric absence as identical and any asymmetric absence as drift

The registry classification identity predicate (`_rows_have_identical_identity`) SHALL classify two rows as identical when every identity field is equal on both sides — including both sides `None` (flat fields) and both sides missing (nested paths, sentinel-equal) — and SHALL classify them as differing when any identity field is asymmetric, including falsy-vs-None flat values (`0` vs `None`, `""` vs `None`) and missing-key vs explicit-null nested values; these semantics are pinned by direct regression tests.

#### Scenario: Symmetric absence stays unchanged

- **WHEN** two registry rows agree on every identity field, with a flat
  identity field `None` on both sides, the nested
  `resource_profile.source_inventory_checksum` `None` on both sides, or the
  top-level `resource_profile` key absent on both sides
- **THEN** the identity predicate SHALL return `True` (row classifies as
  `unchanged`, not `package_changed`)
- **AND** a flat identity field missing on one side and explicitly `None` on
  the other SHALL also return `True` (flat comparison collapses missing and
  null via `dict.get()`; a sentinel-based rewrite flipping this is a
  behavior change this requirement guards against)

#### Scenario: Asymmetric falsy values are drift, not identity

- **WHEN** a flat identity field is `None` on one side and a falsy non-None
  value (`0` for `segment_count`, `""` for `lifecycle_state`) on the other
- **THEN** the identity predicate SHALL return `False` — a truthiness-based
  comparison that conflates falsy values with `None` violates this
  requirement

#### Scenario: Missing nested key differs from explicit null

- **WHEN** one row lacks the top-level `resource_profile` key entirely and
  the other carries `resource_profile.source_inventory_checksum` with an
  explicit `null` value
- **THEN** the identity predicate SHALL return `False` (missing-key sentinel
  and JSON null are materially different identity facts)
