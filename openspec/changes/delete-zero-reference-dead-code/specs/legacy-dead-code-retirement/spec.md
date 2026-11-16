## ADDED Requirements

### Requirement: Zero-reference production symbols are removed

A function, class, method, compatibility forwarder or script in production code that has no reference anywhere in
the repository SHALL be deleted. "Anywhere" covers production code, tests, deployment units, CI, string/dynamic
dispatch, monkeypatch targets and packaging entry points. A compatibility forwarder SHALL be deleted only after its
inventory's removal condition is met; its name SHALL be removed from the inventory rows, and no stale inventory name
SHALL remain for a deleted forwarder.

#### Scenario: an unreferenced helper is deleted without behaviour change

- **WHEN** a helper is defined in `services/`, `packages/`, `workers/`, `apps/api/` or `scripts/` and a whole-repo
  search finds no reference to it other than its definition
- **THEN** the helper and anything that becomes unused through its removal are deleted, every touched module still
  imports, and the existing tests pass unmodified
