# job-retry-mechanism (delta)

## ADDED Requirements

### Requirement: Local Artifact Allowed-Roots Normalization Survives Symlink Loops

The failure-state local artifact guard SHALL normalize its containment bases (the candidate resource-profile artifact roots and their environment fallbacks) and the probed artifact path without relying on symlink-loop-unsafe resolution, SHALL return the same verdict on every supported CPython version, and SHALL never let a fault inside that canonicalization (the strict-realpath normalization and its fallback) escape the decision path as an exception.

A root that fails strict resolution with `ENOENT` keeps its existing admitted
semantics via non-strict realpath normalization (a root may legitimately point
at a not-yet-created directory or an unmounted share). This admission is
deliberately errno-scoped, not loop-free: a root whose strict resolution hits
a missing component before any loop (such as a `<missing>/../<loop>` form)
stays admitted even though the admitted base still contains the loop — a
known, recorded residual. Such a root never raises the root-fault reason (it
is admitted, so no root fault is flagged); the resulting verdicts depend on
how the probed path itself normalizes: a path that also normalizes through
the ENOENT fallback and lands under the phantom base keeps the admitted-root
null-reason verdict, a path that resolves straight into the loop reports
`local_artifact_path_unresolvable`, and a path outside the base reports
`local_artifact_path_outside_allowed_roots`. A root that fails
for any other reason (a symlink loop, a permission fault) is excluded from the
containment bases, and when the probed artifact is not contained by any
remaining resolvable root the guard SHALL report the artifact missing with the
distinguishable unsafe reason `local_artifact_root_unresolvable` — non-null,
and therefore refused by the operator-authorized repair channel under the same
doctrine as `artifact_probe_error`: a rebuild cannot clear a filesystem fault.
The existing reasons keep their meanings, with root faults taking priority:
`local_artifact_path_outside_allowed_roots` is reserved for a candidate whose
every configured root normalized successfully (resolved, or `ENOENT`-admitted
as above) and whose path is genuinely outside them, and
`local_artifact_path_unresolvable` for a probed path that itself fails
resolution while every configured root normalized successfully — whenever any
configured root is unresolvable and no resolvable root contains the path, the
root fault reason wins, so root faults and path faults stay distinguishable.
On this local leg, an "absent" verdict with a null unsafe reason SHALL arise
only from a path contained by a successfully normalized root (resolved, or
`ENOENT`-admitted as above) after that path was actually probed for
existence — the parallel of the object-branch null-reason clause, stated here
so the two legs read as a matched pair.

#### Scenario: A symlink-loop root never aborts the scheduler pass

- **GIVEN** a candidate whose object-store, copyback, or published-artifact
  root (resource profile or environment) is a symlink loop
- **WHEN** the failure-state local artifact guard evaluates any local artifact
  URI for that candidate
- **THEN** the guard returns a fail-closed verdict without raising on any
  supported CPython version, the scheduler pass continues evaluating the
  remaining candidates, and per-tick evidence is still written

#### Scenario: Artifacts judged against a loop root carry the distinguishable root fault reason

- **GIVEN** a candidate whose only configured artifact root fails strict
  resolution with an errno other than `ENOENT` (a symlink loop reached before
  any missing component, a permission fault)
- **WHEN** the guard evaluates a local artifact path — whether lexically under
  or outside the loop root
- **THEN** the artifact is reported missing with unsafe reason
  `local_artifact_root_unresolvable` (never a null-reason absent verdict that
  would feed the authorized rebuild channel, and never
  `local_artifact_path_outside_allowed_roots`, which would route the operator
  to artifact placement instead of the faulty root), and the
  operator-authorized repair channel refuses the resulting blocker

#### Scenario: Resolvable roots keep their existing admitted and containment semantics

- **GIVEN** a candidate with a mix of resolvable roots (including a root that
  does not exist yet) and an unresolvable loop root
- **WHEN** the guard evaluates an artifact path under one of the resolvable
  roots
- **THEN** containment succeeds exactly as before this change — the
  not-yet-created root stays admitted via non-strict realpath normalization
  and the loop root does not poison the verdict; on a candidate whose every configured
  root resolves successfully, an artifact genuinely outside all of them still
  reports `local_artifact_path_outside_allowed_roots`

#### Scenario: A probed path that itself fails resolution keeps the path-fault reason

- **GIVEN** a candidate with resolvable artifact roots and a probed local
  artifact path that is itself a symlink loop
- **WHEN** the guard evaluates that path
- **THEN** the artifact is reported missing with the existing unsafe reason
  `local_artifact_path_unresolvable`, keeping path faults distinguishable from
  root faults
