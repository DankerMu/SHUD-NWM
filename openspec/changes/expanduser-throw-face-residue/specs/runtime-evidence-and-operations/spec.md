## ADDED Requirements

### Requirement: Slurm preflight tilde expansion never raises

The Slurm preflight storage-root checks SHALL expand a leading tilde in
configured root values (in both the allowed-roots walk and the per-root
storage check) without ever letting the expansion escape the preflight as
an exception: when the home directory cannot be determined (an unknown `~user`
prefix, or a plain `~` with no usable home-directory source), the unexpanded
value SHALL flow on as an ordinary path into the existing non-absolute and
containment arms and produce the existing structured blocker or verdict
shape, so the preflight always returns its structured result instead of
aborting the scheduling pass. Values whose tilde does expand, and values
without a tilde, keep their existing verdicts byte-for-byte, and no new
blocker reason is introduced.

#### Scenario: unexpandable tilde in allowed storage roots yields a structured preflight result

WHEN `allowed_storage_roots` contains `~nosuchuser/roots` (or a plain `~/…`
with no determinable home directory)
THEN `_slurm_preflight` returns its structured status/blockers result — no
RuntimeError escapes — and the affected root is adjudicated by the existing
fail-closed arms

#### Scenario: unexpandable tilde in a storage root field yields the existing check verdict

WHEN a preflight storage root field (workspace/object-store/log/runtime) is
an unexpandable tilde value
THEN the per-root storage check produces its existing structured
configured/contained/visible verdict without raising

#### Scenario: expandable and tilde-free roots keep their behavior

WHEN a configured root has no tilde or its tilde expands normally
THEN the preflight verdict is byte-for-byte identical to the pre-change
behavior
