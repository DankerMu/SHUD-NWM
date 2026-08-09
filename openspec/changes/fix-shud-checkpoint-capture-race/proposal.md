# Recover watcher-missed SHUD restart checkpoints via deterministic short rerun

## Why

`workers/shud_runtime/runtime.py` captures T+N restart checkpoints by *sampling*
SHUD's in-place-rewritten `output/<proj>.cfg.ic.update` while the solver runs
(`_StateCheckpointTracker.capture_available`, polled at
`_state_checkpoint_poll_seconds`). SHUD rewrites the header every
`update_ic_step_minutes` model-minutes; a fast solve leaves each intermediate
header alive for a fraction of a second, so the watcher can lose the sampling
race and never observe the requested minute.

Field evidence (2026-08-08/09, node-22, gfs 2026072500): basin
`basins_xinanjiang_upstream` (dg_0a50ecb006879e7336361fc0b6191766) solved 7
days in 1.659 s, SHUD printed "The successful end.", yet f012 was never
sampled — `run_shud` raised `STATE_CHECKPOINTS_MISSING`, hard-failing a run
the solver itself completed and blocking the entire 17-basin cohort publish
(frontier stalled at 07-25 00:00 for ~8 h). Dose-response across the cycle's
17 runs: only the fastest (1.659 s) failed; the next-fastest (1.919 s) barely
passed. Root cause diagnosed in issue #1315; fix shipped as PR #1316
(commit e1d3b611).

## What Changes

- `run_shud`: after a successful solve, if requested checkpoint hours are
  missing, recover deterministically before failing — rerun SHUD from the SAME
  staged IC/forcing with END shortened to the missing f-hour into a scratch
  dir (`workspace/state_checkpoint_recovery/f{hour:03d}/`); the rerun's FINAL
  `cfg.ic.update` header lands exactly on the target minute regardless of
  solve speed.
- Recovered checkpoints pass the SAME acceptance gates as watcher captures
  (`_header_minute_matches_checkpoint` + `state_ic_structure_complete`); a
  failed gate unlinks the copy and leaves the hour missing. Entries carry
  `"provenance": "post_run_recovery"`.
- `STATE_CHECKPOINTS_MISSING` stays a hard failure, but only after recovery
  also fails; the message and `state_checkpoints.json` now carry the observed
  header-minute trail (`observed_header_minutes`) so a miss is locatable.

## Process deviation (recorded)

Implementation pre-exists this fixture: the fix was authored directly by the
orchestrator during an active production outage (frontier blocked on the
failing basin) and pushed as PR #1316 before the OpenSpec fixture was created.
This change back-fills the mandatory fixture; Phase 2/4/4.5/7 run against the
existing head as normal.

## Out of Scope

- No change to the in-flight watcher cadence, poll interval, or
  `update_ic_step_minutes` semantics.
- No change to `state_save_qc`, warm-start selection, or the state snapshot
  index; consumers keep reading `checkpoints` entries unchanged.
- No scheduler-level retry/repair behavior change (the #1203 null-URI blocker
  is a separate issue).
- No node-22 rollout steps in this change (pull + timer restart are ops
  actions tracked in the #1164 watch).
