# job-retry-mechanism (delta)

## ADDED Requirements

### Requirement: Retry Runtime-Root Safety Survives Symlink Loops

The retry submission path SHALL normalize local runtime roots (`workspace_dir`, `object_store_root`, `published_artifact_root`) without relying on symlink-loop-unsafe resolution, SHALL return the same safety verdict on every supported CPython version, and SHALL never let a fault inside that normalization (tilde expansion, canonicalization, or its fallback) escape the safety helper as an exception.

A root that fails strict resolution with `ENOENT` is re-admitted through
non-strict realpath normalization only after a loop-filtering re-check: the
fallback value is strictly re-resolved, and only a second `ENOENT` (the root
genuinely does not exist yet — a not-yet-created directory or an unmounted
share) keeps the admitted verdict, byte-compatible with the pre-change
resolved value. A fallback that still fails for any other reason — including
the `<missing>/../<loop>` phantom form whose fallback retains a symlink loop
— is rejected. A root that fails strict resolution for any reason other than
`ENOENT` (a symlink loop, a permission fault, a stale file handle, a
non-directory component) is likewise rejected. Every rejection uses the
existing reason `unresolvable_local_root` and feeds the existing
unsafe-rejection wiring. Rejection is per-candidate: the rejected root SHALL
NOT enter that candidate's resolved set, comparable-roots overlap baseline,
or submission-manifest contribution, and the rejection SHALL be recorded in
the evidence's bounded `rejected` list — or accounted in the rejection
counters when the evidence cap elides the entry; when no complete candidate
remains, the submission fails with the structured error code
`RETRY_RUNTIME_ROOTS_UNSAFE`. A value whose tilde expansion cannot be
completed (an unknown user home) SHALL fail closed through the existing
non-absolute rejection arm instead of raising.

#### Scenario: A symlink-loop runtime root never escapes as an exception

- **GIVEN** a retry candidate whose `workspace_dir`, `object_store_root`, or
  `published_artifact_root` is a symlink loop, or a value whose tilde
  expansion cannot resolve a home directory
- **WHEN** the retry submission path (the DB manual-retry leg or the db-free
  journal leg) resolves its runtime roots
- **THEN** the safety helper returns a fail-closed verdict without raising on
  any supported CPython version, the rejection is recorded in the
  `runtime_root_resolution` evidence naming the rejected field and reason,
  and when no other complete candidate resolves the submission fails with
  error code `RETRY_RUNTIME_ROOTS_UNSAFE` — never the degraded
  `SBATCH_SUBMISSION_FAILED` attribution

#### Scenario: A loop root never enters the manifest or the overlap baseline

- **GIVEN** a retry candidate with a symlink-loop local runtime root
- **WHEN** that candidate's runtime roots are resolved on any supported
  CPython version
- **THEN** the loop root is absent from that candidate's resolved root set,
  absent from its submission-manifest contribution, and absent from the
  comparable-roots baseline that feeds the workspace/object-store overlap
  guard — the `unresolvable_local_root` rejection is recorded instead

#### Scenario: A not-yet-created root keeps its admitted semantics

- **GIVEN** a retry candidate whose local runtime root fails strict
  resolution with `ENOENT` and whose non-strict fallback also strictly
  re-resolves to `ENOENT` (a not-yet-created directory, an unmounted share)
  or resolves cleanly (a `<missing>/../<real>` form)
- **WHEN** the candidate's runtime roots are resolved
- **THEN** the root stays admitted with verdict "ok" and a value
  byte-compatible with the pre-change resolved value, and no rejection is
  emitted

#### Scenario: A phantom loop-carrying root is rejected on every version

- **GIVEN** a retry candidate whose local runtime root is a
  `<missing>/../<loop>` form — strict resolution fails with `ENOENT` at the
  missing component, but the non-strict fallback still contains a symlink
  loop
- **WHEN** the candidate's runtime roots are resolved
- **THEN** the loop-filtering re-check rejects the root with
  `unresolvable_local_root` on every supported CPython version — the
  admitted-root arm never returns a loop-carrying value into the manifest or
  the overlap baseline

#### Scenario: A permission-fault root is rejected like a loop root

- **GIVEN** a retry candidate whose local runtime root fails strict
  resolution with an errno other than `ENOENT` (for example `EACCES` on an
  untraversable parent)
- **WHEN** the candidate's runtime roots are resolved
- **THEN** the root is rejected with `unresolvable_local_root` exactly as a
  symlink loop is, on every supported CPython version
