## ADDED Requirements

### Requirement: Configuration construction survives symlink loops in containment bases and parent segments

Scheduler configuration construction SHALL apply the established
strict-realpath-then-non-strict paradigm to the containment-base confinement
helper, to both preserve-final parent-segment helpers, and to the
safe-directory final-component guard's parent-segment resolution, so that a
symlink loop in WORKSPACE_ROOT's or NHMS_SCHEDULER_LOCK_ROOT's final segment,
or in any configured path's parent segment, produces the same exception type
and the same subsequent verdict on CPython 3.11/3.12 as on 3.13+, and never
aborts construction with an errno-less RuntimeError in those geometries. The
safe-directory guard's own final-segment resolve (a loop as the evidence
directory's final segment inside the workspace) remains version-divergent and
is tracked separately as issue #1544 — it is outside this requirement.

#### Scenario: WORKSPACE_ROOT final-segment loop converges to the structured safe-directory refusal

WHEN WORKSPACE_ROOT is a symlink loop's final segment
THEN configuration construction raises the existing structured safe-directory
ValueError identically on CPython 3.11/3.12 and 3.13+ (the current message
attributes the evidence_dir field; that attribution is a known defect tracked
as issue #1545, and this scenario locks the refusal class, not the string)

#### Scenario: NHMS_SCHEDULER_LOCK_ROOT final-segment loop converges to the structured containment refusal

WHEN NHMS_SCHEDULER_LOCK_ROOT is a symlink loop's final segment
THEN configuration construction raises the existing structured containment
ValueError (carrying the field name) identically on CPython 3.11/3.12 and
3.13+

#### Scenario: parent-segment loop no longer aborts construction on 3.11/3.12

WHEN any env-driven root's parent segment contains a symlink loop under the
db-backed configuration arm
THEN configuration construction on CPython 3.11/3.12 produces the same
canonical form and the same subsequent verdict as 3.13+ instead of raising an
errno-less RuntimeError. The db-free preserve-final arm
(`_safe_preserve_final_component`) is outside this requirement: on 3.11/3.12
it still swallows the loop and returns the raw path — that residue belongs to
issue #1400

#### Scenario: ENOENT and non-loop containment semantics are unchanged

WHEN a configured path merely does not exist yet, or a non-loop path violates
workspace containment
THEN the existing no-existence-validation construction semantics and the
existing "must be under workspace_root" refusal are byte-for-byte unchanged
