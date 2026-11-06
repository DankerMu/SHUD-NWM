# Proposal: stall-probe-preflight-boundaries

## Why

Issue #2969, the residual gap #2912 (`node22-config-hygiene` design D3) recorded on purpose. The node-22 stall probe
`scripts/node22_scheduler_stall_health.py` grades the guard-less pre-lock root preflight boundary
`scheduler_root_preflight_blocked` as `scheduler_root_blocked`, but the writer
(`services/orchestrator/scheduler_runtime.py`) has three more guard-less pre-candidate `preflight_blocked` boundaries
whose artifact lands: `db_free_runtime_preflight_blocked` (pre-lock, the `/ghdc` registry / index / copyback paths and
the DB-free configuration), `scheduler_rollback_fence_prepared` and `db_free_registry_blocked` (post-lock, before
candidate selection). `classify_pass` reads all of them as neutral and the root-blocked run ends at any of them, so a
lane that emits only these passes for hours grades `ok` with `root_blocked_run.passes == 0`.

The concrete path: with `/ghdc` unmounted the operator repairs the `/scratch` roots the root preflight checks, the
boundary flips from `scheduler_root_preflight_blocked` to `db_free_runtime_preflight_blocked`, the root alert clears
and the probe returns to `ok` while the scheduler still submits nothing. node-22 sat preflight-blocked for weeks from
2026-10-14 (first `PUBLISHED_ARTIFACT_ROOT_NOT_WRITABLE`, then `SCHEDULER_ROOT_TEMP_ROOT_NOT_FOUND`); that is exactly
the class of outage the probe must surface, whichever preflight it stops at.

## What Changes

- A pinned table in the probe maps every guard-less pre-candidate `preflight_blocked` boundary to the verdict of its
  remedy class: root -> `scheduler_root_blocked` (unchanged), `db_free_runtime_preflight_blocked` ->
  `scheduler_db_free_runtime_blocked`, `scheduler_rollback_fence_prepared` -> `scheduler_rollback_fenced`,
  `db_free_registry_blocked` -> `scheduler_registry_blocked`.
- Slot 10 grades one `preflight_blocked_run`: contiguous newest-first passes whose boundary is any table key (a flip
  between table boundaries does not end it), threshold `NHMS_SCHEDULER_STALL_ROOT_BLOCKED_MINUTES` (same variable,
  same bounds), verdict picked by the newest pass's boundary.
- The receipt gains a `preflight_blocked_run` block (`passes`, `span_minutes`, `threshold_minutes`, `newest_boundary`,
  `remedy_verdict`, `boundary_passes`). `root_blocked_run` keeps its four keys and its root-exact values.
- A parity test enumerates the writer's `preflight_blocked` literals and requires the guard-less ones to equal the
  table plus an explicit exemption (`scheduler_timing_level_unrecognised`) and the guarded ones to equal
  `{scheduler_progress_guard_blocked}`; it is routed from the writer module in CI test selection.
- `docs/runbooks/production-ops/stuck-detection.md`: the root-blocked section describes the shared run and both
  receipt blocks; one new section per new verdict; the "known residual, see #2969" note is replaced.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `scheduler-stall-alerting`: the neutral-shape requirement names all four guard-less boundaries; the root-blocked
  requirement grades on the shared run (its scenario "A non-root pass ends the run" is rewritten: another table
  boundary ends the root-exact run but not the graded run); a new requirement covers the three sibling verdicts, the
  receipt block and the writer parity test.

## Impact

- Code: `scripts/node22_scheduler_stall_health.py` only. `services/orchestrator/scheduler_runtime.py` is not touched,
  so no line-number citation moves.
- Behaviour: three new non-zero verdicts (exit 1, through the existing `OnFailure=` mail lane). The root-blocked
  verdict now also fires when a root run directly follows another table boundary and the combined run meets the
  threshold (strictly more alerting on a lane that is submitting nothing). No new configuration variable, no change to
  pass shapes, to the streak, to the in-flight gate or to frontier selection. `RECEIPT_SCHEMA_VERSION` stays v1
  (additive block).
- Deployment: node-22 picks the probe up at the next ff-only sync; the live `--now` replay receipt is an operator item
  of the issue's Verification field, not part of this change's local evidence.
