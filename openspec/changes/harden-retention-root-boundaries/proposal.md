## Why

Retention has three related boundary defects left after #1318: a blank or relative primary root can become a working-directory deletion surface, overlapping configured roots can select the same subtree twice, and an internal symlink permanently prevents an additional-root run tree from being reclaimed. These issues share one root-admission and deletion boundary and should close in one reviewed change.

## What Changes

- Apply one root-input hygiene path to the primary and additional roots while retaining lane-specific receipt reasons.
- Reject roots whose potential retention target trees intersect, with the primary root and then the first additional root taking precedence; admit ordinary parent/child layouts whose `runs/` and primary cycle lanes are disjoint.
- Reclaim selected additional-root run trees by unlinking descendant symlinks without following them; retain the existing refusal for a symlinked `runs/` root.
- Add two-pass, physical-disk regression coverage through direct retention, scheduler-pass, and cleanup-CLI seams.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `production-scheduler-orchestration`: Strengthen retention root admission, overlap rejection, and contained additional-root deletion semantics.

## Impact

- Code: `services/orchestrator/retention.py`, `services/orchestrator/scheduler_config.py`, and `services/orchestrator/scheduler_runtime.py`; existing `packages/common/safe_fs.py` primitive is reused without a new dependency.
- Repository guard: exact legacy-large-file exclusions are added for the two mandatory touched files; structural splits and exclusion removal are tracked by #1872.
- Tests: `tests/test_retention.py`, `tests/test_cli_cleanup_frontier.py`, and `tests/test_safe_fs.py` only if helper-level coverage is missing.
- Runtime: scheduler pass and manual cleanup share the hardened behavior; no DB, API, schema-version, Slurm submission, or frontend change.
- Issues: #1616, #1615, #1617.

Design is required because this is an expanded, high-intensity file-deletion and receipt-contract change.