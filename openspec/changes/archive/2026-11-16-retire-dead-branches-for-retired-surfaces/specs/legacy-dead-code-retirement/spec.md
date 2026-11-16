## ADDED Requirements

### Requirement: Branches serving retired surfaces are removed or kept with a reason

A code branch, selector carry, allowlist entry or default that exists only for a retired route, template path, change
path or storage target SHALL be deleted once it is proven unreachable or purposeless. Otherwise it SHALL be kept with
a recorded reason: it still serves a live published contract, it is a fail-closed guard against the retired surface,
or it is a frozen identity whose tool refuses production use. A comment that states live facts about a retired
surface SHALL be updated to the current measured state.

#### Scenario: an allowlist branch keyed on an archived change path is removed

- **WHEN** an audit allowlist branch matches only a change path that has been archived elsewhere and can no longer
  occur in the tree
- **THEN** the branch, the tests that exist only for it and any reason mapping it alone produced are deleted, and the
  audit's budget-counted finding count and allowlisted/unallowlisted split are unchanged
