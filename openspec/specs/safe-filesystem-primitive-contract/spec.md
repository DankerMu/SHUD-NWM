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

The evidence-root lane's own separate expansion, which sits outside the shared module, is not covered by this requirement and continues to throw bare; it is tracked on its own.

#### Scenario: An evidence-root preparation lane converts the refusal into its own structured code

- **GIVEN** an evidence root whose configured value has an undeterminable home directory
- **WHEN** the lane prepares its evidence directories
- **THEN** it reports its existing structured evidence error code rather than aborting with a bare `RuntimeError`

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

