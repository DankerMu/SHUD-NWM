# runtime-evidence-and-operations Spec Delta

## ADDED Requirements

### Requirement: DB-free scheduler config path adjudication survives symlink loops

The db-free scheduler configuration lane SHALL normalize the required-path
values it adjudicates, and the config values it canonicalizes at construction
time, without relying on symlink-loop-unsafe resolution, and SHALL produce the
same canonical form on every supported CPython version. `Path.resolve()` is
not a usable loop predicate on the supported interpreter range — the
non-strict form stopped raising on symlink loops in CPython 3.13+, and the
strict form raises an errno-less `RuntimeError` on 3.12 and earlier — so this
lane SHALL normalize through `os.path.realpath`, whose strict form raises
`OSError` carrying an errno on every supported version and whose non-strict
form raises neither `OSError` nor `RuntimeError` on any of them. Neither form
is total: an unrepresentable path string (one carrying an embedded NUL) raises
`ValueError` from every resolution primitive, and each helper below states
whether it folds that case or leaves it as a pre-existing escape.

For the **required-path check**, which owns a structured blocker channel: a
value whose strict resolution fails with an errno other than `ENOENT` — a
symlink loop foremost — SHALL produce the existing
`db_free_required_path_unsafe` blocker carrying the errno-derived reason,
rather than being folded lexically and then attributed downstream as
`db_free_required_path_not_found`. Attribution SHALL therefore distinguish
"this path is a symlink loop" from "this path has not been created", which the
lexical fold previously conflated on CPython 3.13+ and which sends operators
to the wrong remedy. A value whose strict resolution fails with `ENOENT`
keeps its existing admitted semantics through a loop-filtered non-strict
fallback — a required path whose final components do not exist yet is
adjudicated by the existing downstream missing-parent and not-found blockers,
not by the resolution step — and a fallback that still carries a loop is
rejected as unsafe. A value that cannot be resolved because it is not a valid
path string SHALL take the same unsafe blocker rather than escaping. The
blocker record shape and its `error_type` evidence field are unchanged, and
**no new blocker code is introduced**. The containment comparison that follows
is itself unchanged but is no longer reached by values that fail resolution:
a value that is both unresolvable and outside the configured boundary SHALL
report the unsafe blocker rather than
`db_free_required_path_outside_boundary`, while a cleanly resolving
out-of-boundary value keeps the boundary blocker unchanged. The reason *values* carried by `db_free_required_path_unsafe`
do widen: today that code carries only `unsafe`, `traversal` and
`credential_component`, and adopting the errno-derived classification adds the
shared mapping's values (`unsafe_path` for a symlink loop or a non-directory
component, `not_writable` for a permission fault, `unavailable` otherwise).
That widening is the point of the requirement rather than a side effect, and
it SHALL include re-classifying the already-blocked under-a-loop case from
`unsafe` to the errno-derived value.

For the **config construction** helpers, which have no rejection channel and
must return a path, the db-free arm SHALL be normalized identically to the
already-converted database-backed arm (strict `os.path.realpath`, falling back
to the non-strict form on any `OSError` — and on `OSError` only, because the
fallback call would raise `ValueError` again for an unrepresentable path
string and turn a pre-existing escape into an escape from inside the handler).
That `ValueError` escape is therefore retained here exactly as it stands
today, neither introduced nor removed. Classification remains the
storage preflight's responsibility rather than construction's, so this arm
SHALL NOT introduce an errno split or a rejection of its own. This makes the
two interpreter arms agree on one canonical form: a loop-bearing value that
CPython 3.12 and earlier previously returned unresolved is now returned in the
same folded form CPython 3.13+ produces, and downstream classification acts on
one shape instead of two.

The path-identity comparison helper in the same lane SHALL likewise normalize
through non-strict `os.path.realpath` so that its comparison verdicts do not
depend on the interpreter version. Its pre-existing `ValueError` escape on an
unrepresentable path string is explicitly retained, neither introduced nor
removed by this requirement: the helper has no rejection channel and its
callers compare only its own products, so folding that case would require a
sentinel value this lane does not define. Values that resolve cleanly keep
their existing normalized values byte-for-byte in all of these helpers.

#### Scenario: A symlink-loop required path is attributed as unsafe, not as missing

- **GIVEN** a db-free required-path value that is a symlink loop, or lies
  under one, within the configured containment bases
- **WHEN** the db-free required-path check adjudicates it
- **THEN** it produces the existing `db_free_required_path_unsafe` blocker
  code carrying the errno-derived reason value `unsafe_path` on every
  supported CPython version, and not the `db_free_required_path_not_found`
  attribution the lexical fold previously produced — and a path lying *under*
  a loop, already blocked with that same code, likewise reports the
  errno-derived value in place of the generic `unsafe`

#### Scenario: A not-yet-created required path keeps its existing adjudication

- **GIVEN** a db-free required-path value under a cleanly resolving
  containment base whose final components do not exist yet
- **WHEN** the required-path check adjudicates it
- **THEN** the resolution step admits it with a value byte-identical to the
  pre-change normalized value, and the verdict is produced by the existing
  downstream missing-parent / not-found blockers exactly as before

#### Scenario: Config construction yields one canonical form on both interpreter arms

- **GIVEN** a db-free config path value that is a symlink loop, or lies under
  one
- **WHEN** the config construction helper canonicalizes it
- **THEN** it returns the folded canonical form — the same value on CPython
  3.12 and earlier as on 3.13+, where 3.12 and earlier previously returned the
  unresolved value — and no exception escapes construction

#### Scenario: Path identity comparison is version-independent

- **GIVEN** two db-free config path values whose canonicalization involves a
  symlink loop
- **WHEN** the lane's path-identity helper normalizes each for comparison
- **THEN** each normalization returns the folded form, so the resulting
  identity verdict is the same on every supported CPython version — where
  previously CPython 3.12 and earlier returned the unresolved value and 3.13+
  the folded one, making the verdict interpreter-dependent

#### Scenario: Clean values keep their behavior

- **GIVEN** db-free config or required-path values with no symlink loop
- **WHEN** any helper in this lane normalizes them
- **THEN** the resulting values and verdicts are byte-for-byte identical to
  the pre-change behavior
