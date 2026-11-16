## ADDED Requirements

### Requirement: Test-only production helpers are retired or justified

A production symbol that an entropy audit finds referenced only by its own tests SHALL be retired, together with the
tests whose only subject it is, unless it is required by a protocol, an abstract interface, a governed compatibility
list or a Requirement of a living capability spec, is used by a shared test harness to drive live production code,
or is a deliberate public, safety or operator API. A retained symbol SHALL carry a recorded keep reason. Retiring
a symbol SHALL NOT remove coverage of live behaviour.

#### Scenario: a legacy helper only its own tests use is retired

- **WHEN** a helper in `services/`, `packages/`, `workers/` or `scripts/` has no production caller, is not declared by
  any protocol, compatibility list or living spec Requirement, is not used by a shared test harness, and is referenced only by tests that exercise it alone
- **THEN** the helper and those tests are deleted, every touched module still imports, and the remaining suites,
  including the compatibility guards, pass
