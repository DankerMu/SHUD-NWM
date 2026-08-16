# job-retry-mechanism (delta)

## ADDED Requirements

### Requirement: Local Artifact Path Tilde Expansion Never Raises

The failure-state local artifact guard SHALL expand a leading tilde in a
probed local artifact value without ever letting the expansion escape the
decision path as an exception: when the home directory cannot be determined
(an unknown `~user` prefix, or a plain `~` with no usable home-directory
source), the unexpanded value SHALL flow on as an ordinary path into the
existing containment verdicts and produce a deterministic missing-status
tuple under those rules instead of aborting the scheduling pass. Because
the unexpanded value is a relative path, its verdict is anchored at the
process working directory: with every configured root normalized
successfully and the working directory outside all of them the guard
reports the existing outside-allowed-roots containment reason, and with
the working directory under a configured root the existing
contained-and-probed verdicts apply unchanged. The existing root-fault
priority is untouched — when any configured root is unresolvable and no
resolvable root contains the anchored path, the root-fault reason still
wins exactly as the existing requirement reserves it. Values whose tilde does expand, and values without a tilde,
keep their existing behavior byte-for-byte, and the guard's root-side and
path-side normalization SHALL treat the same unexpandable-tilde input
consistently — neither side raises, and each side follows the existing
containment rules from the same working-directory anchor.

#### Scenario: An unknown-user tilde path fails closed instead of crashing the pass

- **GIVEN** a candidate whose probed artifact value is
  `~nosuchuser/output/summary.json`, whose configured artifact roots
  resolve normally, and a process working directory outside every
  configured root
- **WHEN** the failure-state artifact guard evaluates the value
- **THEN** no exception escapes and the guard returns the deterministic
  missing-status tuple with the existing outside-allowed-roots containment
  reason — the pass continues and evidence for the candidate is written

#### Scenario: A plain tilde with no determinable home directory fails closed

- **GIVEN** an environment where the home directory cannot be determined
  (no `HOME` and no password-database entry for the current uid), a probed
  artifact value of `~/output/summary.json`, and a process working
  directory outside every configured root
- **WHEN** the failure-state artifact guard evaluates the value
- **THEN** no exception escapes and the guard returns the same
  deterministic fail-closed missing-status shape as the unknown-user form

#### Scenario: Expandable and tilde-free values keep their behavior

- **GIVEN** a probed artifact value whose tilde expands against a real
  home directory, or a value with no tilde at all
- **WHEN** the failure-state artifact guard evaluates the value
- **THEN** the returned verdict is byte-for-byte identical to the
  pre-change behavior

#### Scenario: Root side and path side agree on the same unexpandable input

- **GIVEN** the same unexpandable `~user` string supplied both as a
  configured artifact root and as the probed artifact value
- **WHEN** the guard normalizes each side
- **THEN** neither side raises, both sides anchor the unexpanded literal
  at the same working directory, and the equal anchored paths yield the
  existing contained-but-absent verdict (a missing-status tuple with a
  null unsafe reason) rather than a containment rejection
