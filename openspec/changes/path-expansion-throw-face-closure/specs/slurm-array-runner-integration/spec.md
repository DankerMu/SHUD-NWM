## ADDED Requirements

### Requirement: Scheduler configuration construction SHALL survive an undeterminable home directory on both database arms

Scheduler configuration construction SHALL treat a configured path whose leading `~` component has no determinable home directory as a value to hand down to the storage preflight, not as a construction-time abort, on the database-backed arm exactly as on the database-free arm, because classification of unusable roots belongs to the preflight and a construction-time abort produces no structured blocker at all.

The two arms SHALL produce byte-identical products for the same such input, so that the convergence is pinned by an equality of results rather than merely by the absence of an exception. This applies to every configuration helper in the module that expands a leading `~` outside its error boundary, including the helper reached through the workspace-root preflight path, not only the helpers reachable through the allowed-storage-roots and log-root fields.

#### Scenario: An allowed storage root naming an unknown user constructs successfully on the database-backed arm

- **GIVEN** an allowed storage root configured as `~<unknown user>/roots`
- **WHEN** the scheduler configuration is constructed with the database-backed arm selected
- **THEN** construction succeeds instead of aborting with an errno-less `RuntimeError`
- **AND** the resulting value is byte-identical to the value the database-free arm produces for the same input

#### Scenario: A log root naming an unknown user constructs successfully on the database-backed arm

- **GIVEN** a log root configured as `~<unknown user>/logs`
- **WHEN** the scheduler configuration is constructed with the database-backed arm selected
- **THEN** construction succeeds and the value is byte-identical to the database-free arm's value

#### Scenario: A workspace root naming an unknown user constructs successfully on the database-backed arm

- **GIVEN** a workspace root configured as `~<unknown user>/workspace`
- **WHEN** the scheduler configuration is constructed with the database-backed arm selected
- **THEN** construction succeeds and the value is byte-identical to the database-free arm's value

#### Scenario: The storage preflight still classifies the unusable root structurally

- **GIVEN** a configuration constructed from such a value
- **WHEN** the storage preflight runs
- **THEN** it returns its structured result carrying a blocker for that field rather than raising

### Requirement: The safe-directory guard SHALL resolve a final-segment symlink identically on every supported interpreter

The safe-directory final-component guard SHALL decide a final-segment symlink by strict real-path resolution and SHALL refuse a resolution loop with the module's structured configuration error on every supported CPython, because non-strict resolution raises an errno-less `RuntimeError` on CPython 3.11 and 3.12 and silently adopts the loop as the field's value on 3.13 and later.

A target that does not exist SHALL fall back to non-strict real-path resolution rather than being refused, preserving today's acceptance of a dangling final-segment symlink. Both database arms SHALL execute the same subsequent decision steps — the containment recheck and the directory check — rather than one arm skipping them because an intermediate wrapper swallowed the errno-less throw.

#### Scenario: A final-segment loop inside the workspace is refused identically on both interpreter arms

- **GIVEN** an evidence directory whose final segment is a symlink loop located inside the workspace root
- **WHEN** the scheduler configuration is constructed
- **THEN** it raises the structured configuration error on CPython 3.11 and 3.12 and on 3.13 and later alike
- **AND** the loop is never adopted as the field's value

#### Scenario: Both database arms run the same decision steps on that geometry

- **GIVEN** the same in-workspace final-segment loop
- **WHEN** the configuration is constructed with the database-free arm and with the database-backed arm
- **THEN** both arms reach the same verdict
- **AND** neither arm skips the containment recheck or the directory check

#### Scenario: A dangling final-segment symlink is still accepted

- **GIVEN** a final-segment symlink whose target does not exist
- **WHEN** the guard runs
- **THEN** it returns without raising, exactly as before this change

#### Scenario: The four pre-existing final-segment verdicts are unchanged

- **GIVEN** in turn a healthy directory symlink, a symlink to a file, a symlink escaping the workspace, and an absent final component
- **WHEN** the guard runs on each
- **THEN** the healthy symlink is accepted, the file target is refused as not a directory, the escaping target is refused as not under the workspace root, and the absent component returns without raising

### Requirement: A workspace-root loop refusal SHALL name the operator's own knob and carry the offending path

The configuration refusal raised when the workspace root itself is a symlink loop SHALL carry the offending path under the module's existing redaction treatment and SHALL let an operator reach the workspace-root knob, because the refusal is currently attributed to a derived evidence-directory field the operator never configured and carries neither a path nor an environment variable name.

The refusal SHALL remain a configuration `ValueError`, the message SHALL be identical on CPython 3.11 and 3.12 and on 3.13 and later, and the lock-root containment refusal SHALL keep its current wording verbatim.

#### Scenario: The workspace-root loop refusal is attributable to the workspace root

- **GIVEN** a workspace root configured as a symlink loop and no evidence-root override set
- **WHEN** the scheduler configuration is constructed
- **THEN** the raised configuration error carries the offending path
- **AND** it lets the operator reach the workspace-root knob rather than pointing only at the derived evidence directory

#### Scenario: The refusal text is identical across interpreter arms

- **GIVEN** the same workspace-root loop
- **WHEN** the configuration is constructed on CPython 3.11 or 3.12 and on 3.13 or later
- **THEN** the two messages are identical

#### Scenario: The lock-root refusal is untouched

- **GIVEN** a lock root configured as a symlink loop
- **WHEN** the configuration is constructed
- **THEN** the refusal keeps its existing containment wording verbatim

### Requirement: The compatibility-surface path helpers SHALL converge on a normalized result instead of diverging by interpreter

The two compatibility-surface configuration path helpers exposed through the runtime-roots forwarders SHALL apply the module's established strict-then-non-strict real-path paradigm, so that a symlink loop yields the same normalized result on CPython 3.11 and 3.12 as on 3.13 and later instead of raising an errno-less `RuntimeError` on the former and silently adopting the loop on the latter.

Their existing contract SHALL be preserved exactly: a `None` or empty value still returns `None` early, a relative value is still joined onto the supplied base, and a path that does not exist still returns a non-strict normalized result rather than raising.

#### Scenario: A loop yields the same normalized result on both interpreter arms

- **GIVEN** a symlink loop passed to either compatibility-surface helper, as an absolute value and as a relative value
- **WHEN** the helper is called on CPython 3.11 or 3.12 and on 3.13 or later
- **THEN** both interpreters return the same normalized result
- **AND** neither raises an errno-less `RuntimeError`

#### Scenario: The early-return and relative-join contracts are unchanged

- **GIVEN** a `None` value, an empty value, and a relative value with a base
- **WHEN** the helpers are called
- **THEN** `None` and empty still return `None`, and the relative value is still joined onto the base

#### Scenario: A nonexistent path still returns a normalized result

- **GIVEN** a path that does not exist
- **WHEN** either helper is called
- **THEN** it returns the non-strict normalized result rather than raising
