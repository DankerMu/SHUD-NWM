## ADDED Requirements

### Requirement: The chain facade SHALL NOT keep imports that nothing reads

Every name bound by an import statement in `services/orchestrator/chain.py` SHALL have an in-repo reader: a use inside
`chain.py`, a read of the name through the facade by an owner module or a test, or a string patch target. The chain
facade's stable import surface is the set of names with such a reader; a name without one is not part of it.
- **Check:** a test SHALL collect the names bound by imports in `chain.py` and fail on any name that is neither used in
  `chain.py`, nor read through the facade according to the facade name-resolution scan, nor listed in a short literal
  list of names that tests read only through name lists.
- **Module loading:** an import kept only because other code depends on the module being loaded SHALL carry a comment
  naming that consumer, and is listed with it in the test.

#### Scenario: An unread import is rejected before merge

- **WHEN** `chain.py` imports a name that nothing in the repository reads
- **THEN** the test fails and names it

#### Scenario: A name read only through a test name list is kept

- **WHEN** a facade name is read by a test only through a list of names passed to `getattr`
- **THEN** the name is in the literal list and the import stays
