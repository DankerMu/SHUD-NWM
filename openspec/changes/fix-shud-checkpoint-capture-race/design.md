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
- `cfg_path` is rewritten in place and restored in `finally` — the published
  workspace cfg is byte-identical after recovery regardless of outcome.
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
  `observed_header_minutes`; the hard-failure message includes the trail.

## Downstream compatibility

`packages/common/state_cli.py::_load_state_checkpoint_manifest` reads only
`payload["checkpoints"]` and per-entry whitelisted fields
(`relative_path`, `valid_time`, ...); extra top-level key
`observed_header_minutes` and extra entry key `provenance` are ignored by
construction. No other production reader of `state_checkpoints.json` exists
(`git grep state_checkpoints.json` → state_cli + tests only).
