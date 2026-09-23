# safe-filesystem-primitive-contract Specification

## Purpose

The error contract of `packages/common/safe_fs.py`, the shared write-side filesystem primitive
layer that every lane funnels its directory creation, size-limited reads, and recursive deletes
through. This capability governs what those primitives are allowed to throw, not what they are
allowed to touch: a failure inside a primitive must arrive at the caller as the module's own
structured error carrying a `kind` classification, never as a bare stdlib exception that no
caller's `except` tuple was written for. It exists because the module is a shared base — a single
unstructured throw defeats the advertised contract on all of its callers at once — and because
these are *write-side* primitives, where the alternative to refusing is acting on a path the
operator never named.

## Requirements

### Requirement: The shared safe-filesystem primitives SHALL report an undeterminable home directory as a structured refusal

Every public entry point of the shared safe-filesystem module SHALL surface a failure to expand a leading `~` component as the module's own structured error carrying its `kind` classification, never as a bare errno-less `RuntimeError`, because that expansion is the shared prelude of all of them and a bare throw defeats the module's advertised error contract on every caller at once.

The refusal SHALL reuse the existing `unsafe` classification rather than introducing a new one, so that callers which branch on the existing classification values keep a total set of branches. The expansion failure SHALL NOT be degraded into a permissive pass that keeps the literal `~` component, because the write-side primitives would then create, delete, or overwrite a path the operator never named.

#### Scenario: A write-side primitive refuses an undeterminable home directory without touching the filesystem

- **GIVEN** a path whose leading component names a user for whom no home directory can be determined
- **WHEN** the directory-creating primitive is invoked with it
- **THEN** it raises the module's structured filesystem error carrying the `unsafe` classification
- **AND** it does not raise a bare `RuntimeError`
- **AND** no directory whose name literally begins with `~` is created anywhere under the working directory

#### Scenario: The read-side and delete-side primitives refuse the same input identically

- **GIVEN** the same undeterminable-home path
- **WHEN** the size-limited read primitive and the recursive-delete primitive are each invoked with it
- **THEN** each raises the module's structured filesystem error carrying the `unsafe` classification
- **AND** neither leaves a literal `~`-prefixed entry behind

#### Scenario: A CLI configuration lane converts the refusal into its own structured rejection

- **GIVEN** a command-line environment-file argument whose value has an undeterminable home directory
- **WHEN** the environment file is applied
- **THEN** the lane reports its existing structured configuration rejection
- **AND** no bare `RuntimeError` escapes to the operator as a traceback

#### Scenario: An evidence-root preparation lane converts the refusal into its own structured code

- **GIVEN** an evidence root whose configured value has an undeterminable home directory
- **WHEN** the lane prepares its evidence directories
- **THEN** it reports its existing structured evidence error code rather than aborting with a bare `RuntimeError`

#### Scenario: An evidence-root configuration and validation lane converts the refusal into its own structured code

- **GIVEN** an evidence root whose configured value has an undeterminable home directory
- **WHEN** `ProductionMetConfig.from_env` resolves the root, and when `validate_met` revalidates an equivalent config
- **THEN** each entrypoint reports `PRODUCTION_MET_EVIDENCE_PATH_UNSAFE` rather than aborting with a bare `RuntimeError`
- **AND** neither creates a literal `~`-prefixed entry under the working directory

### Requirement: The mid-open inode-identity refusal SHALL carry its own structured discriminator, and its documented meaning SHALL match what it actually detects

The no-follow file open compares the target's identity before and after opening and refuses when the inode changed in between. That refusal SHALL carry a discriminator distinct from the primitive's other refusals, because it is the only one a caller can legitimately choose to absorb, and a caller must be able to select it by field rather than by matching message text. The discriminator's documented meaning SHALL state what the check actually detects: the target was replaced by a different regular file while it was being opened — which an ordinary atomic rename and a hostile swap produce identically at this layer, since the primitive cannot distinguish them. The documentation SHALL NOT describe it as the symlink defense, because a symlink appearing in that window is refused by the no-follow open flag and the symlink mode checks and never reaches the identity comparison; describing it as the symlink defense would lead a later reader to treat absorbing it as a security regression when the actual symlink barriers are untouched. The comparison itself SHALL remain in place and SHALL keep refusing; only its labelling changes here. The primitive SHALL NOT retry internally, because it is shared by callers with opposite needs — some absorb a concurrent rename, others must reject any inode movement — and a retry policy fixed inside the primitive would deny one of those groups its required semantics. Adding this discriminator SHALL NOT alter the meaning of any existing discriminator value and SHALL NOT change which conditions are refused.

#### Scenario: The identity refusal is selectable by field

- **WHEN** a caller catches the refusal raised because the target's inode changed mid-open
- **THEN** the error carries a discriminator distinguishing it from safety refusals and from I/O failures, and the caller can branch on it without inspecting the message

#### Scenario: Safety refusals keep their existing discriminator

- **WHEN** the open is refused because the target is a symlink, is not a regular file, or violates containment
- **THEN** the discriminator is unchanged from before this change, so callers that branch on it see no behavioral difference

#### Scenario: The primitive itself does not retry

- **WHEN** the identity comparison fails
- **THEN** the primitive raises immediately, leaving any retry decision to the caller

### Requirement: Write-side configured-path wrappers SHALL preserve their owning structured error contracts

A production wrapper that expands a configured path before invoking shared write-side filesystem primitives SHALL translate an undeterminable-home failure into its existing owning-module error contract before any filesystem access. It SHALL NOT leak an errno-less `RuntimeError`, retain a literal `~` component, or add a new public error code where an existing path/write refusal already represents the failure.

#### Scenario: Published log paths reject an undeterminable artifact root at every public chain seam

- **GIVEN** `NHMS_PUBLISHED_ARTIFACT_ROOT` names a user whose home directory cannot be determined
- **WHEN** gateway-log persistence, local-stage-log writing, or published-log path derivation consumes that root
- **THEN** each seam raises the orchestrator's existing `PUBLISHED_LOG_WRITE_FAILED` error
- **AND** no bare `RuntimeError` escapes
- **AND** no literal `~`-prefixed path is created under the working directory

#### Scenario: Valid configured roots retain their existing products

- **GIVEN** an existing absolute published-artifact root and an existing absolute production-met evidence root
- **WHEN** their respective wrappers resolve and use those roots
- **THEN** the resulting paths and successful write behavior are unchanged from before this change

### Requirement: Path canonicalization SHALL resolve strictly unless named, and a tolerated non-strict fallback SHALL be justified by dereference

Every function under `services/`, `workers/`, `packages/`, or `apps/` that canonicalizes a filesystem path with `os.path.realpath` SHALL either pass `strict=True` on at least one of those calls, or appear in a named exemption list that records which admission clause below justifies it; a function in neither state is a violation. Passing `strict=True` makes a symlink loop available to the handler as an `OSError` carrying an errno, rather than folding it into a partially resolved product before any handler can see it; it does not by itself mean the loop is reported, because a handler MAY still admit the `ENOENT` arm under the clauses below. `Path.resolve()` SHALL NOT be used as a symlink-loop predicate in this family: within the interpreter range this project supports, its non-strict form does not raise on a symlink loop on CPython 3.13+, and its strict form raises an errno-less `RuntimeError` on CPython 3.12 and earlier, so neither form states the same truth on both arms.

For the `<missing>/../<loop>` input class this requirement adjudicates — the class for which non-strict `os.path.realpath` returns a partially resolved product instead of raising — a site MAY admit the `ENOENT` arm by falling back to the non-strict form only when at least one of three clauses holds, and the clause relied upon SHALL be recorded at the site. Every member of the authority set SHALL carry that record in its own body as a comment naming its disposition — `ADR 0009 clause N` for a site that admits under clause N, or `ADR 0009 loop-filtered` for a site that re-resolves strictly instead of admitting — and the family guard SHALL enforce it as a presence check, so that the set of authority members carrying no disposition marker is empty. The guard decides that a disposition is named, not that the named one is correct; correctness remains a design-review judgement against the three clauses below. The marker is required of every member rather than only of admitting members because deciding which members admit is a judgement about handler shape, and this family's whole lesson is that shape inference encodes the author's guess. The marker SHALL be an actual comment: a mention of the token inside a docstring or any other string literal SHALL NOT satisfy this requirement, because a prose mention is not a record of a disposition. Where one qualified name is bound more than once in a module, the guard SHALL report that name rather than merge the definitions, because the `(module, qualified function)` key cannot tell them apart — a merge would let the first definition escape both this requirement and the strict-resolution one above. This requirement asserts nothing about input classes it does not adjudicate: an embedded NUL byte, or a relative path whose working directory has been removed, make non-strict `os.path.realpath` raise, and those escapes are governed elsewhere.

Clause one, downstream dereference: before any decision derived from the normalized product is committed, the path under adjudication SHALL be dereferenced against the kernel (an existence probe, an `lstat`, an `open`), and a fault there SHALL change the verdict. The probe MAY target the raw input rather than the normalized product, because kernel resolution makes the two equivalent on every input for which the probe can succeed. Clause two, provable coincidence: the normalized product and the path the consequent action actually acts upon SHALL coincide on every input for which that action can succeed; this clause quantifies over inputs only and SHALL NOT be relied upon where a concurrent writer can change the path between the check and the action. Clause three, comparison operand: the normalized product SHALL be used only as a containment base or an identity-comparison operand, carrying no assertion that the path exists or is usable, while the value judged against it is dereferenced before the verdict is committed.

A site satisfying none of the three clauses SHALL loop-filter its admission, re-resolving the non-strict fallback strictly and keeping the admission only on a second `ENOENT` or a clean resolution. Whether a handler splits on errno or catches `OSError` bare is a separate question that this requirement does not adjudicate; a bare handler is broader-tolerant under the same dereference guarantee, not less safe.

#### Scenario: A new canonicalization site that never resolves strictly and is not named is refused

- **GIVEN** a function under `services/`, `workers/`, `packages/`, or `apps/` that calls `os.path.realpath`
- **WHEN** none of that function's `os.path.realpath` calls passes `strict=True` and the function is absent from the exemption list
- **THEN** the family guard reports the function as a violator, so the violator set is non-empty and the guard fails

#### Scenario: An exempt site is admitted only by name and reason

- **GIVEN** a canonicalization site with no strict call whose admission rests on a recorded clause
- **WHEN** the family guard enumerates violators
- **THEN** the site is exempt only through a named entry carrying the clause it relies on, never through a shape heuristic, so removing the entry restores the violation

#### Scenario: A canonicalization site that records no disposition fails the guard

- **GIVEN** a function in the authority set, whether it admits the `ENOENT` arm under a clause or loop-filters instead
- **WHEN** its body carries no comment of the form `ADR 0009 clause N` or `ADR 0009 loop-filtered`, or carries the token only inside a docstring or other string literal, or shares its qualified name with a second definition in the same module
- **THEN** the family guard reports it, so that deleting the marker, demoting it to prose, or duplicating the name turns the guard red

#### Scenario: A loop behind a missing component stays admitted where the path is dereferenced

- **GIVEN** a configured path of the `<missing>/../<loop>` shape at a site whose adjudicated path is later opened or probed
- **WHEN** the site canonicalizes that path and takes the `ENOENT` fallback
- **THEN** the fallback product is admitted without a blocker at canonicalization time, and the loop is reported by the later dereference rather than by this step

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
