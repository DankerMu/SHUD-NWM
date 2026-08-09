# Design: deterministic post-run checkpoint recovery

## Risk triage

```text
Issue type: bugfix
Project profile: NHMS (openspec/project-profile.md)
Blast radius: high
Fixture level: expanded (repair intensity: high)
Upstream suggested level: absent (hand-written issue #1315) — triaged locally
Why:
- Domain expanded-triggers hit: `shud_runtime`, `restart`, `IC`, solver/runtime behavior
- Core triggers hit: concurrency (sampling race), retry/rerun, file output/overwrite
- Failure path of every production forecast run; a wrong recovery poisons warm-start lineage
OpenSpec change: fix-shud-checkpoint-capture-race (generated, back-filled after PR #1316)
```

## Approach: keep the watcher, add deterministic recovery

Alternatives considered:

1. **Slow the solver / shorten the poll interval** — shrinks but never closes
   the race (header lifetime scales with solve speed, unbounded below);
   rejected.
2. **Patch SHUD to write per-checkpoint files** — correct long-term but
   requires a solver release + redeploy across engines (native, `.py` stubs);
   out of scope for an outage fix.
3. **Post-run deterministic rerun (chosen)** — SHUD's FINAL `cfg.ic.update`
   after a run ending exactly at the target hour IS the wanted checkpoint, by
   the same mechanism the watcher tries to sample. Rerun cost is bounded by
   the short horizon (≤ the original run, typically seconds-to-minutes) and
   only paid on a miss.

Determinism claim: the rerun uses the SAME staged IC, forcing, and cfg (only
END/OUTPUT_DIR rewritten), so its state at T+hour equals the original run's
state at T+hour up to solver reproducibility — which is exactly the
equivalence the watcher capture asserts today.

## Key mechanics

- Mode split follows `_is_shud_project_mode`: project mode rewrites `END`
  (days, tab-separated, `start_day + hour/24`); cfg mode rewrites `END_TIME`
  (`start_time + hours`, `" = "` separator). `OUTPUT_DIR` points at the
  scratch root in both modes.
- `cfg_path` is rewritten in place and restored in `finally`; the restore is
  best-effort — on a restore-write failure the hour records
  `cfg_restore_failed` (never masking its own outcome) and the workspace cfg
  may retain the shortened horizon until the next `execute()` re-templates
  it. The cfg never reaches the object store, so the exposure is
  workspace-local.
- Scratch root `workspace/state_checkpoint_recovery/f{hour:03d}/` is created
  with containment (`_ensure_directory(..., containment_root=workspace)`);
  copies go through `_read_staged_bytes`/`_write_staged_bytes` (no-follow,
  bounded).
- Rerun is bounded by `self.config.timeout_seconds` (kill + continue on
  expiry); per-hour logs land in
  `log_dir/state_checkpoint_recovery_f{hour:03d}.{out,err}.log`.
- Gates are shared, not reimplemented: `_read_cfg_ic_header_minute`,
  `_header_minute_matches_checkpoint`, `state_ic_structure_complete`
  (expected river count) — identical to watcher-capture acceptance, so
  e13ae809 partial-discard semantics are preserved.
- Diagnostics: tracker appends every distinct observed header minute;
  `write_manifest` emits it as a sibling top-level key
  `observed_header_minutes` and is written whenever checkpoint hours were
  requested — including the total-miss case; per-hour recovery outcomes are
  recorded alongside; the hard-failure message includes both.
- Containment boundary (round-2 CAND-1/CAND-2): each hour's entire body —
  cfg rewrite, spawn/wait, gate + install (`install_recovered`) — sits inside
  ONE `try/except (OSError, SHUDRuntimeError, SafeFilesystemError)`; a failure
  anywhere in the hour records that hour's outcome and continues.
  `SafeFilesystemError` must be in the tuple explicitly: it subclasses
  `RuntimeError` directly (sibling of `SHUDRuntimeError`), so the narrower
  tuple misses the `unlink_no_follow` lane. The `write_manifest()` call is
  likewise best-effort — a diagnostics-write failure never replaces
  `STATE_CHECKPOINTS_MISSING` as the run's error code.
- Budget: main solve + all recovery reruns share ONE `timeout_seconds`
  monotonic deadline (pre-change `run_shud` never exceeded 1× the budget; a
  per-rerun fresh timeout would multiply the task's wall time by the number
  of missing hours — an unbounded multiple of the configured timeout is not
  a bound at all, and risks a Slurm SIGKILL mid-loop losing the task-outcome
  receipt); an hour with no remaining budget is skipped with outcome
  `budget_exhausted`.
- Alignment precondition (pre-existing, recorded per review round 1
  cand-10): SHUD's `PrintInit` writes `cfg.ic.update` only when
  `t % Update_IC_STEP == 0` — including the final write at END. The recovery
  guarantee therefore requires `hour*60 % update_ic_step_minutes == 0`, which
  today is enforced only by `chain_manifests.py` setting
  `update_ic_step_minutes = min(checkpoint_hours)*60`. A misaligned
  configuration fails safe (gate discards, hard failure) both in the main run
  and in recovery; the systemic guard is tracked as follow-up issue #1317,
  not in this change.

## Downstream compatibility

`packages/common/state_cli.py::_load_state_checkpoint_manifest` reads only
`payload["checkpoints"]` and per-entry whitelisted fields
(`relative_path`, `valid_time`, ...); extra top-level key
`observed_header_minutes` and extra entry key `provenance` are ignored by
construction. No other production reader of `state_checkpoints.json` exists
(`git grep state_checkpoints.json` → state_cli + tests only).
