## ADDED Requirements

### Requirement: Slurm preflight tilde expansion never raises

The Slurm preflight storage-root checks SHALL expand a leading tilde in
configured root values (in both the allowed-roots walk and the per-root
storage check) without ever letting the expansion escape the preflight as
an exception: when the home directory cannot be determined (an unknown `~user`
prefix, or a plain `~` with no usable home-directory source), the unexpanded
value SHALL flow on as an ordinary path into the existing arms — the
allowed-roots walk's ENOENT tolerance arm (which admits a cwd-anchored
root with no blocker) and the per-root storage check's structured
containment/visibility verdicts — so the preflight always returns its
structured result instead of aborting the scheduling pass. Values whose tilde does expand, and values
without a tilde, keep their existing verdicts byte-for-byte, and no new
blocker reason is introduced.

#### Scenario: unexpandable tilde in allowed storage roots is tolerated without crashing the preflight

WHEN `allowed_storage_roots` contains `~nosuchuser/roots` (or a plain `~/…`
with no determinable home directory)
THEN `_slurm_preflight` returns its structured status/blockers result — no
RuntimeError escapes — and the affected root flows through the existing
ENOENT tolerance arm and is admitted as a cwd-anchored containment root
with no blocker (the existing arm never produces a blocker for
not-yet-existing roots; the resulting phantom-root geometry is the
already-tracked #1427 adjacency and is documented, not changed, here)

#### Scenario: unexpandable tilde in a storage root field yields the existing check verdict

WHEN a preflight storage root field (workspace/object-store/log/runtime) is
an unexpandable tilde value
THEN the per-root storage check produces its existing structured
configured/contained/visible verdict without raising

#### Scenario: expandable and tilde-free roots keep their behavior

WHEN a configured root has no tilde or its tilde expands normally
THEN the preflight verdict is byte-for-byte identical to the pre-change
behavior
