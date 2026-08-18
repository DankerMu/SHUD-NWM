## ADDED Requirements

### Requirement: Configuration construction survives symlink loops in containment bases and parent segments

Scheduler configuration construction SHALL apply the established
strict-realpath-then-non-strict paradigm to the containment-base confinement
helper and to both preserve-final parent-segment helpers, so that a symlink
loop in a containment base's final segment or in any configured path's parent
segment produces the same exception type and the same subsequent verdict on
every supported CPython version, and never aborts construction with an
errno-less RuntimeError on any version.

#### Scenario: containment-base final-segment loop converges to the structured containment refusal

WHEN WORKSPACE_ROOT or NHMS_SCHEDULER_LOCK_ROOT is a symlink loop's final segment
THEN configuration construction raises the existing structured containment
ValueError (carrying a field name) identically on CPython 3.11/3.12 and 3.13+

#### Scenario: parent-segment loop no longer aborts construction on 3.11/3.12

WHEN any env-driven root's parent segment contains a symlink loop
THEN configuration construction on CPython 3.11/3.12 produces the same
canonical form and the same subsequent verdict as 3.13+ instead of raising an
errno-less RuntimeError

#### Scenario: ENOENT and non-loop containment semantics are unchanged

WHEN a configured path merely does not exist yet, or a non-loop path violates
workspace containment
THEN the existing no-existence-validation construction semantics and the
existing "must be under workspace_root" refusal are byte-for-byte unchanged
