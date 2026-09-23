## ADDED Requirements

### Requirement: A `Path.resolve()` call that is handled for resolution failure SHALL be handled for the pin's loop failure too

Every strict `Path.resolve()` call (a `strict` argument that is present and not the literal `False`) under `services/`, `workers/`, `packages/`, or `apps/` that sits inside a `try` whose handlers catch `OSError` itself SHALL also be covered, by that `try` or an enclosing `try` in the same function, by a handler that catches `RuntimeError` (or a base of it), and that handler SHALL produce the same outcome as the `OSError` arm. On CPython 3.12 and earlier strict `Path.resolve()` raises an errno-less `RuntimeError` on a symlink loop, while on CPython 3.13+ it raises `OSError` with `ELOOP`; an `OSError`-only handler is therefore dead code on the production pin for exactly the input it exists to classify. Non-strict calls are outside this requirement: on CPython 3.13+ they fold the loop without raising, so adding a `RuntimeError` arm to them would make the two interpreters disagree rather than agree; they are adjudicated site by site against ADR 0009's clauses. This criterion is independent of the `os.path.realpath` guard's `strict=True` criterion, which cannot be reused on this surface: there `strict=True` is the form that raises the errno-less `RuntimeError`. A family guard SHALL assert that the set of violating calls is empty; it SHALL NOT assert a member list. Handlers catching only subclasses of `OSError` are outside this requirement, because a loop escapes them on every supported interpreter alike.

#### Scenario: An OSError-only handler around `.resolve()` is refused

- **GIVEN** a function that calls `path.resolve(strict=True)` inside `try: ... except OSError: continue`
- **WHEN** the `.resolve()`-surface guard scans the tree
- **THEN** the call is reported as a violator and the guard fails until the handler also catches `RuntimeError`

#### Scenario: A loop candidate is skipped, not raised, on the pin

- **GIVEN** a Python-runtime candidate path that is a symlink loop
- **WHEN** the sbatch export-line generator resolves it on CPython 3.11
- **THEN** the candidate is skipped exactly as an `OSError` candidate is, and no `RuntimeError` escapes

#### Scenario: A non-strict `.resolve()` inside an OSError handler is not a violator

- **GIVEN** a function that calls `path.resolve(strict=False)` inside `try: ... except OSError: ...`
- **WHEN** the `.resolve()`-surface guard scans the tree
- **THEN** the call is not reported, because on CPython 3.13+ it raises nothing on a loop and a `RuntimeError` arm would introduce a cross-interpreter divergence
