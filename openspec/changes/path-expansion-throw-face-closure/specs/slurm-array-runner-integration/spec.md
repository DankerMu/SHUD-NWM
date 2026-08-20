## ADDED Requirements

### Requirement: Scheduler configuration construction SHALL survive an undeterminable home directory on both database arms

Scheduler configuration construction SHALL treat a configured path whose leading `~` component has no determinable home directory as a value to hand down to the storage preflight, not as a construction-time abort, on the database-backed arm exactly as on the database-free arm, because classification of unusable roots belongs to the preflight and a construction-time abort produces no structured blocker at all.

The two arms SHALL produce byte-identical products for the same such input, so that the convergence is pinned by an equality of results rather than merely by the absence of an exception. This applies to the two configuration fields that bypass the deliberate re-raise helper and reach the module's own bare expansions — the allowed storage roots and the log root. Every other root field aborts earlier in the deliberate re-raise, which is an existing design ruling and is out of scope; a third bare expansion in the module is reachable only through the compatibility forwarders and is closed as a ledger item, not because a production field reaches it.

#### Scenario: An allowed storage root naming an unknown user constructs successfully on the database-backed arm

- **GIVEN** an allowed storage root configured as `~<unknown user>/roots`
- **WHEN** the scheduler configuration is constructed with the database-backed arm selected
- **THEN** construction succeeds instead of aborting with an errno-less `RuntimeError`
- **AND** the resulting value is byte-identical to the value the database-free arm produces for the same input

#### Scenario: A log root naming an unknown user constructs successfully on the database-backed arm

- **GIVEN** a log root configured as `~<unknown user>/logs`
- **WHEN** the scheduler configuration is constructed with the database-backed arm selected
- **THEN** construction succeeds and the value is byte-identical to the database-free arm's value

#### Scenario: The compatibility-surface expansion helper stops throwing bare on the same input

- **GIVEN** the module's non-relative preserve-final-component helper, `_config_path_preserve_final_component`, called directly with `~<unknown user>/workspace`
- **WHEN** it expands the value
- **THEN** it returns the same product the database-free arm would produce
- **AND** it does not raise an errno-less `RuntimeError`

#### Scenario: The storage preflight still classifies the unusable root structurally

- **GIVEN** a configuration constructed from such a value
- **WHEN** the storage preflight runs
- **THEN** it returns its structured blocked result rather than raising, the unusable root being admitted by the tolerance arm for a merely missing path and classified through the consequential root blockers

### Requirement: The safe-directory guard SHALL resolve a final-segment symlink identically on every supported interpreter

The safe-directory final-component guard SHALL decide a final-segment symlink by strict real-path resolution and SHALL refuse a resolution loop with the module's structured configuration error on every supported CPython, because non-strict resolution raises an errno-less `RuntimeError` on CPython 3.11 and 3.12 and silently adopts the loop as the field's value on 3.13 and later.

Every strict real-path failure other than a loop SHALL fall back to non-strict real-path resolution rather than being refused, because the guard accepts all of them today — a target that does not exist, a target whose parent denies traversal, and a target reached through a regular file are each accepted on the current code — and refusing any of them would silently turn acceptance into rejection. The loop refusal SHALL be recognised by the loop error number obtained from the `errno` module rather than by a hard-coded integer, because that number differs between the development platform and the deployment platform. The guard's separate metadata-lookup failure handler SHALL instead keep its existing refusal for every error number other than a loop, so that the non-loop geometries already pinned against that handler stay green verbatim.

#### Scenario: A final-segment loop inside the workspace is refused identically on both interpreter arms

- **GIVEN** an evidence directory whose final segment is a symlink loop located inside the workspace root
- **WHEN** the scheduler configuration is constructed on the database-backed arm
- **THEN** it raises the structured configuration error on CPython 3.11 and 3.12 and on 3.13 and later alike
- **AND** the loop is never adopted as the field's value

#### Scenario: The guard reaches the same verdict on both interpreter arms when called directly

- **GIVEN** the same in-workspace final-segment loop
- **WHEN** the safe-directory guard is called directly on CPython 3.11 or 3.12 and on 3.13 or later
- **THEN** both interpreters raise the same structured refusal
- **AND** neither interpreter reaches the containment recheck or the directory check with an adopted loop value

#### Scenario: A dangling final-segment symlink pointing inside the workspace is still accepted

- **GIVEN** a final-segment symlink whose target does not exist and lies inside the workspace root
- **WHEN** the guard runs
- **THEN** it returns without raising, exactly as before this change

#### Scenario: The other non-loop strict-resolution failures are still accepted

- **GIVEN** in turn a final-segment symlink whose target sits under a parent that denies traversal, and one whose path is reached through a regular file
- **WHEN** the guard runs
- **THEN** each returns without raising, exactly as before this change

#### Scenario: A dangling final-segment symlink pointing outside the workspace is still refused

- **GIVEN** a final-segment symlink whose target does not exist and lies outside the workspace root
- **WHEN** the guard runs
- **THEN** it keeps today's containment refusal naming the workspace root

#### Scenario: The four pre-existing final-segment verdicts are unchanged

- **GIVEN** in turn a healthy directory symlink, a symlink to a file, a symlink escaping the workspace, and an absent final component
- **WHEN** the guard runs on each
- **THEN** the healthy symlink is accepted, the file target is refused as not a directory, the escaping target is refused as not under the workspace root, and the absent component returns without raising

### Requirement: A workspace-root loop refusal SHALL name the operator's own knob and carry the offending path

The configuration refusal raised when the workspace root itself is a symlink loop SHALL carry the offending path as its sibling refusals in the same guard carry their operands and SHALL let an operator reach the workspace-root knob, because the refusal is currently attributed to a derived evidence-directory field the operator never configured and carries neither a path nor an environment variable name.

The refusal SHALL remain a configuration `ValueError`, the message SHALL be identical on CPython 3.11 and 3.12 and on 3.13 and later, and the lock-root containment refusal SHALL keep its current wording verbatim. The path SHALL be carried as the guard's sibling refusals carry their operands, because that guard receives no evidence-redaction flag and cannot reproduce the preflight lane's redaction treatment. The non-loop geometries that reach the same handler — a workspace root that is a regular file, and one whose mode denies traversal — SHALL keep their existing message verbatim, so the enriched wording is reached only by the loop.

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

#### Scenario: The non-loop workspace-root refusals are untouched

- **GIVEN** in turn a workspace root that is a regular file and a workspace root whose mode denies traversal
- **WHEN** the configuration is constructed
- **THEN** each keeps its existing refusal message verbatim

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
