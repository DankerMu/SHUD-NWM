# real-integration-test-matrix Specification Delta

## ADDED Requirements

### Requirement: The per-run integration database name MUST be asserted as a generator property

Tests covering the integration lane's per-run database name SHALL assert
properties of the **generator** — what its output is a function of — and SHALL
NOT assert properties of a single random draw whose outcome depends on ambient
process state. An assertion whose pass or fail is decided by the value of
`os.getpid()`, the wall clock, or any other ambient input outside the code under
test does not express the property it claims: it reports a sampling accident,
and its failure rate is a function of the environment rather than of a defect.

Where the intended property is "the name does not depend on input X", the test
SHALL establish it by pinning the generator's declared source of randomness to a
known value and asserting the output equals the value derived from that pin
alone. Exact equality demonstrates that every unpinned input — including X —
contributes nothing.

#### Scenario: The name must be shown not to derive from the process id

- **WHEN** a test asserts that the per-run integration database name is not
  derived from the process id
- **THEN** it stubs the generator's randomness source to a fixed sentinel and
  asserts the produced name equals the name built from that sentinel alone,
  rather than searching the produced name for the process id's decimal digits

#### Scenario: A correct generator under an adversarial process id

- **WHEN** the test suite runs in an environment that assigns short process ids,
  such as inside a PID namespace where the pytest process is pid 1
- **THEN** the assertions on the database name pass deterministically, because
  no assertion's outcome depends on the process id's value or digit count

#### Scenario: Shape assertions remain

- **WHEN** the generator property is pinned by a stub
- **THEN** the unstubbed assertions on the real generator's output — that two
  consecutive names differ, that a name matches the declared `nhms_it_` plus
  32 hex-character shape, and that its suffix parses as a UUID — are retained,
  so the stub does not become the only thing under test
