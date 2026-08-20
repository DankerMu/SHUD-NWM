# job-retry-mechanism Spec Delta

## ADDED Requirements

### Requirement: DB-free selector containment survives symlink loops at both the root and the path level

The db-free retry submission lane SHALL normalize its selector containment
bases (the `NHMS_SCHEDULER_ALLOWED_ROOTS` entries and their equivalent
profile sources) **and** the selector path values judged against them without
relying on symlink-loop-unsafe resolution, SHALL return the same verdict on
every supported CPython version, and SHALL never let a fault inside that
normalization escape the adjudicator as an exception. Neither level may reach
`Path.resolve()` in either form: the non-strict form stopped raising on
symlink loops in CPython 3.13+, and the strict form raises an errno-less
`RuntimeError` on 3.12 and earlier, so neither is a usable loop predicate on
the supported interpreter range. Normalization SHALL go through
`os.path.realpath`, whose strict form raises `OSError` carrying an errno on
every supported version.

At **both** levels a value that fails strict resolution with `ENOENT` keeps
its existing admitted semantics — a configured root may legitimately point at
a not-yet-created directory or an unmounted share, and a selector path is
deliberately not existence-checked at submission time — but that admission
SHALL be loop-filtered rather than errno-scoped alone: the non-strict
fallback value is strictly re-resolved, and only a second `ENOENT` (the
target genuinely does not exist yet) or a clean strict re-resolution (a
`<missing>/../<real>` form) keeps the admitted verdict. A fallback that still
fails for any other reason — including the `<missing>/../<loop>` phantom
form, whose strict resolution stops at the missing component but whose
fallback still carries a symlink loop — SHALL be rejected. This loop-filtered
admission is deliberately the same doctrine the runtime-root lane already
carries and deliberately **not** the admitted-phantom residual recorded for
the local artifact guard: the selector path level consumes the selector root
level's output, so a bare fallback at either level would reproduce at the
next level the very containment fail-open the other level rejects.

A value that fails strict resolution for any reason other than `ENOENT` (a
symlink loop, a permission fault, a stale file handle, a non-directory
component) SHALL likewise be rejected, and a value that cannot be resolved at
all because it is not a valid path string (an embedded NUL, which raises
`ValueError` rather than `OSError`) SHALL fall into the same rejection rather
than escaping. Rejections SHALL use the lane's existing reasons —
`db_free_allowed_root_unresolvable` at the root level and
`db_free_selector_path_unresolvable` at the path level — with no new reason
vocabulary and no change to the rejection record shape or to the
adjudicators' signatures. Rejection remains per-value: a rejected root SHALL
NOT enter the containment bases, and the existing cascade SHALL be preserved
— when every configured root is rejected the path-level adjudicator still
reports `db_free_allowed_roots_missing`, and a path that **resolves cleanly**
yet lies outside the surviving bases still reports
`db_free_selector_path_outside_allowed_roots`. Resolution now precedes the
containment comparison, so a value that is *both* unresolvable and outside the
bases SHALL report the resolution reason rather than the containment one: that
re-ordering is deliberate — an unresolvable value has no trustworthy location
to compare against — and it changes the reported reason only within the
already-rejected class, never a verdict from admitted to rejected or back.

Values that resolve cleanly, and values whose strict resolution fails with
`ENOENT` and whose fallback re-resolves cleanly or to a second `ENOENT`,
SHALL keep their existing verdicts and their existing resolved values
byte-for-byte. Because both db-free legs (the retry submission leg and the
file-orchestration journal leg) consume the same pair of adjudicators, this
requirement governs both.

#### Scenario: A phantom loop-carrying selector root is rejected on every version

- **GIVEN** a db-free allowed-roots entry of the `<missing>/../<loop>` form —
  strict resolution fails with `ENOENT` at the missing component, but the
  non-strict fallback still resolves onto a symlink loop
- **WHEN** the db-free selector allowed-roots adjudicator normalizes it
- **THEN** no root is admitted and exactly one
  `db_free_allowed_root_unresolvable` rejection is recorded, on every
  supported CPython version — the same verdict the direct loop form already
  receives, so one physical target can no longer draw two opposite verdicts

#### Scenario: A not-yet-created selector root keeps its admitted semantics

- **GIVEN** a db-free allowed-roots entry that fails strict resolution with
  `ENOENT` and whose non-strict fallback either strictly re-resolves to a
  second `ENOENT` (a not-yet-created directory, an unmounted share) or
  resolves cleanly (a `<missing>/../<real>` form)
- **WHEN** the adjudicator normalizes it
- **THEN** the root stays admitted with a value byte-compatible with the
  pre-change resolved value and no rejection is emitted

#### Scenario: A symlink-loop selector path is rejected instead of lexically admitted

- **GIVEN** a db-free selector path value that is a symlink loop, or lies
  under one, while every configured allowed root resolves cleanly
- **WHEN** the selector path adjudicator judges it
- **THEN** it is rejected with the existing
  `db_free_selector_path_unresolvable` reason on every supported CPython
  version — the value never reaches the resolved selector fields or the
  submission manifest through a purely lexical containment comparison, and no
  `RuntimeError` escapes to the submission path's broad exception handler,
  so the structured rejection evidence is preserved instead of the attribution
  degrading to `SBATCH_SUBMISSION_FAILED`

#### Scenario: A phantom loop-carrying selector path is rejected

- **GIVEN** a db-free selector path of the `<missing>/../<loop>` form lying
  lexically under a cleanly resolving allowed root
- **WHEN** the selector path adjudicator judges it
- **THEN** the loop-filtered re-check rejects it with
  `db_free_selector_path_unresolvable` — the path level carries the same
  admission doctrine as the root level it consumes

#### Scenario: A not-yet-created selector path stays admitted

- **GIVEN** a db-free selector path under a cleanly resolving allowed root
  whose final components do not exist yet
- **WHEN** the selector path adjudicator judges it
- **THEN** no rejection is produced and the normalized value is byte-identical
  to the pre-change resolved value — submission-time adjudication still
  performs no existence check

#### Scenario: An unrepresentable selector value is rejected rather than raising

- **GIVEN** a db-free allowed-roots entry or selector path value carrying an
  embedded NUL, for which resolution raises `ValueError` rather than `OSError`
- **WHEN** the corresponding adjudicator normalizes it
- **THEN** it takes the lane's existing `*_unresolvable` rejection and no
  exception escapes the adjudicator

#### Scenario: An unresolvable out-of-root value reports the resolution reason

- **GIVEN** a db-free selector path that is a symlink loop and that also lies
  outside every configured allowed root
- **WHEN** the selector path adjudicator judges it
- **THEN** it reports `db_free_selector_path_unresolvable` rather than
  `db_free_selector_path_outside_allowed_roots`, while a cleanly resolving
  out-of-root value keeps the containment reason unchanged

#### Scenario: The all-roots-rejected cascade is preserved

- **GIVEN** a db-free configuration in which every configured allowed root is
  rejected by the root-level adjudicator
- **WHEN** a selector path is judged against the resulting empty bases
- **THEN** the path-level adjudicator reports the existing
  `db_free_allowed_roots_missing` reason, unchanged
