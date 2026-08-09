# Spec Delta: hypertable-compression

## ADDED Requirements

### Requirement: The schema-dump-list capture MUST refuse a docker CLI that deviates from the pinned host CLI unless an explicit self-test seam is enabled

The schema-dump-list capture SHALL enforce, in code, that the docker
CLI it executes is the same pinned host CLI its recorded forensic
argv attests (it is the capture kind that records docker invocation
argvs into the bundle): when the injected docker executable differs
from the pinned constant and the explicit self-test opt-in flag is
not set, that capture SHALL fail closed before running any subprocess
or emitting any forensic document, and the error SHALL name the
observed docker value. The self-test opt-in remains a hidden
test-only flag; the production plan author SHALL NOT emit it. Capture
kinds that run docker only to record measured container facts (no
argv attestation pair) are outside this requirement.

#### Scenario: Deviating docker without the seam is refused before any bundle write

- **WHEN** the capture producer is invoked for the schema-dump-list
  kind with a docker executable different from the pinned host CLI and
  without the self-test opt-in flag
- **THEN** it exits non-zero with an error naming the observed docker
  value, emits no forensic document on stdout, and leaves the evidence
  directory empty

#### Scenario: Hermetic self-tests keep stub-docker injection via the explicit opt-in

- **WHEN** a hermetic test appends the self-test opt-in flag to the
  schema-dump-list capture argv and injects a stub docker
- **THEN** the capture succeeds, the recorded forensic argv still
  names the pinned host CLI, and the existing verifier literal pins
  accept the document unchanged

#### Scenario: The production default path is unaffected

- **WHEN** the capture producer runs with the docker executable equal
  to the pinned host CLI and no opt-in flag
- **THEN** behavior is unchanged from before the guard
