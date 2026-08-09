# Tasks: fix-shud-checkpoint-capture-race

Fixture level: expanded · Repair intensity: high · Project profile: NHMS

## Change surface

- `workers/shud_runtime/runtime.py`: `run_shud` miss-handling branch, new
  `_recover_missing_state_checkpoints` (incl. scratch-root fresh-scoping),
  `_StateCheckpointTracker` (`observed_header_minutes`, `install_recovered`,
  `write_manifest` payload).
- `tests/test_shud_runtime.py`: `_FAST_SOLVER_STUB`, `_STUCK_HEADER_SOLVER_STUB`,
  recovery scenario tests (fast-miss recovery, 100-repeat determinism,
  gate/diagnostics, slow-leg speed independence, stale scratch lane).
- `tests/test_state_manager.py`: consumer-tolerance test for
  `packages/common/state_cli.py::_load_state_checkpoint_manifest` (new-shape
  manifest keys ignored).

## Must preserve

- Watcher-captured checkpoints: same filenames, same acceptance gates, same
  entry fields; a normal-speed run's behavior is byte-identical.
- `STATE_CHECKPOINTS_MISSING` remains a hard failure when no valid checkpoint
  can be derived (error code unchanged; message extended only).
- e13ae809 partial-discard semantics: a gate-failing candidate is discarded,
  never installed.
- `state_cli.py` manifest consumption: `checkpoints` list shape per entry
  unchanged; extra keys ignored.
- Published workspace cfg is byte-identical after recovery (restore in
  `finally`).

## Must add/change

- Post-run deterministic recovery rerun per missing hour, scratch-dir scoped,
  timeout-bounded, mode-aware (project `END` / cfg `END_TIME`).
- Recovery scratch root is fresh-scoped per invocation: content from any
  earlier attempt is removed/ignored before the rerun; a stale candidate is
  never installable without a successful same-invocation rerun.
- Recovered entries carry `provenance: post_run_recovery`.
- `observed_header_minutes` trail in `state_checkpoints.json` and in the
  failure message.

## Seams under test

- `run_shud` public entry with a stubbed solver executable (the established
  seam used by all existing runtime tests) — highest seam that exercises
  watcher, recovery, gates, manifest, and error path together.

## Risk packs considered (core)

- Public API / CLI / script entry: not selected — no entrypoint/signature change; `run_shud` contract unchanged.
- Config / project setup: not selected — no new config keys; timeout reuses `config.timeout_seconds`.
- File IO / path safety / overwrite: selected — scratch dir creation, cfg in-place rewrite+restore, staged-bytes copy; all through existing no-follow/containment helpers.
- Schema / columns / units / field names: selected — `state_checkpoints.json` gains a top-level key and an entry key; consumer tolerance verified (design.md).
- Auth / permissions / secrets: not selected — no auth surface.
- Concurrency / shared state / ordering: selected — the root cause is a sampling race; recovery must be race-free (post-run, final-state based).
- Resource limits / large input / discovery: selected — rerun bounded by `timeout_seconds`; kill+continue on expiry; per-hour scratch dirs scoped under workspace.
- Legacy compatibility / examples: not selected — no legacy example/format change; both command styles (project/cfg) covered as change surface, not legacy.
- Error handling / rollback / partial outputs: selected — hard-failure-after-recovery semantics, gate-failure unlink, cfg restore on every path.
- Release / packaging / dependency compatibility: not selected — no dependency change.
- Documentation / migration notes: not selected — behavior self-documented in error message/manifest; no operator-facing config.

## Domain packs (NHMS profile)

- SHUD numerical runtime / conservation / NaN: selected — recovered state must equal the watcher-target state (same IC/forcing, END at target); structure gate enforces river-count completeness.
- Run manifest / QC provenance: selected — provenance field distinguishes recovery-derived checkpoints; observed-header trail is diagnostic evidence.
- Hydro-met time series / forcing windows: not selected — forcing inputs untouched.
- Geospatial / CRS: not selected — no geometry surface.
- PostGIS / TimescaleDB: not selected — no DB surface.
- Slurm production lifecycle: not selected — recovery is in-process within the same Slurm task; no scheduler/sbatch change.
- External providers / snapshot reproducibility: not selected — no provider surface.
- Published NHMS artifacts / display identity: not selected — display plane untouched.

## Invariant Matrix

Governing invariant: a checkpoint file published under
`output/state_checkpoints/` ALWAYS has a header minute matching the requested
hour and a structurally complete body, regardless of whether the watcher or
the post-run recovery produced it; a run fails hard iff no such file could be
derived for a requested hour.

Source-of-truth identity/contract: `state_checkpoints.json` entry
(`lead_hours`, `valid_time`, `checksum`, `relative_path`) bound to the file it
describes.

Surfaces:
- Producers: `_StateCheckpointTracker._capture` (watcher), `install_recovered` (recovery); both behind the same header/body gates.
- Validators/preflight: `_header_minute_matches_checkpoint`, `state_ic_structure_complete` (packages/common/state_qc.py).
- Storage/cache/query: `write_manifest` → `state_checkpoints.json` (containment-rooted writes).
- Public routes/entrypoints: `run_shud` (only caller-visible surface; raises `STATE_CHECKPOINTS_MISSING` on residual miss).
- Frontend/downstream consumers: `packages/common/state_cli.py::_load_state_checkpoint_manifest` (tolerant reader, unchanged).
- Failure paths/rollback/stale state: recovery timeout → kill+continue; gate failure → unlink candidate; cfg restored in `finally` on every path; OSError on cfg write → skip hour; scratch root `state_checkpoint_recovery/f{hour:03d}/` must be fresh-scoped — a stale `<proj>.cfg.ic.update` left by an earlier attempt (reused run workspace) must never be installable without a successful same-invocation rerun.
- Evidence/audit/readiness: `observed_header_minutes` in manifest + error message; per-hour recovery logs in `log_dir`.

Regression rows:
- Fast solve, watcher misses f012, recovery rerun succeeds → checkpoint installed with header at target minute, `provenance=post_run_recovery`, run exits 0.
- Recovery rerun produces wrong-header/incomplete state (stuck stub) → candidate unlinked, `STATE_CHECKPOINTS_MISSING` raised with observed-header trail, no checkpoint file left behind.
- Normal-speed run, watcher captures all hours → recovery never invoked; entries identical to pre-change (unchanged sibling behavior).
- `state_cli` reads a manifest with `observed_header_minutes` + `provenance` → checkpoints parsed exactly as before (consumer compatibility).
- Scratch root pre-seeded with a stale gate-valid `<proj>.cfg.ic.update` and the recovery rerun fails (rc≠0/timeout) → stale candidate is NOT installed, hour stays missing, hard failure raised.

## Required evidence

Mapped to issue #1315 acceptance boxes (AC1 repro+100 repeats · AC2 speed
independence 1s/60s · AC3 e13ae809 gates preserved · AC4 miss semantics at the
raise site · AC5/AC6 node-22 live · AC7 diagnostic trail · AC8 pytest+ruff).

- [x] AC1 (repro leg): `test_run_shud_recovers_watcher_missed_checkpoint_via_deterministic_rerun` — `_FAST_SOLVER_STUB` writes ONLY the final header (deterministic race-loss limit; red vs pre-change source, green post-fix) → recovered f012 header 720, provenance `post_run_recovery`, cfg restored (`END_TIME = 2026-05-04T00:00:00Z`).
- [x] AC1 (repeat leg): `test_run_shud_recovery_repeats_deterministically` raised 5 → 100 repeats → 100/100 recoveries, 0 misses, each with header 720.
- [x] AC2: explicit speed-independence pair — fast leg `test_run_shud_recovers_watcher_missed_checkpoint_via_deterministic_rerun` (`_FAST_SOLVER_STUB`, recovery-derived) and slow leg `test_run_shud_checkpoint_capture_is_solve_speed_independent_slow_leg` (`_SLOW_SOLVER_STUB`, header alive 25× the poll interval → watcher capture, no `provenance`, no `state_checkpoint_recovery/` dir) both end with f012 present.
- [x] AC3: `test_run_shud_recovery_keeps_partial_gates_and_reports_observed_headers` — `_STUCK_HEADER_SOLVER_STUB` (header stuck at 1440) → candidate rejected by gates, no checkpoint file installed.
- [x] AC4: hard-failure-only-after-recovery semantics implemented with comment at the `run_shud` raise site (commit e1d3b611).
- [x] AC7: same test asserts raise message contains `observed cfg.ic.update header minutes: 1440`; `state_checkpoints.json` carries `observed_header_minutes`.
- [x] AC8: `uv run pytest -q tests/test_shud_runtime.py tests/test_state_manager.py tests/test_warm_start_chaining.py` (294 passed) + `uv run ruff check .` clean.
- [x] Consumer tolerance (Schema pack): `test_state_checkpoint_manifest_reader_ignores_runtime_diagnostic_keys` — `_load_state_checkpoint_manifest` fed the same manifest with and without top-level `observed_header_minutes` / entry-level `provenance` → identical `StateCheckpoint` list (valid_times, lead_hours, filenames, referenced bytes, output-relative paths).
- [x] Stale scratch lane (File IO pack): `test_run_shud_recovery_never_installs_stale_scratch_state` — `state_checkpoint_recovery/f012/demo.cfg.ic.update` pre-seeded gate-valid, rerun exits 0 without writing state → stale file cleared and NOT installed, `STATE_CHECKPOINTS_MISSING` raised. Red vs pre-fix `runtime.py`: `Failed: DID NOT RAISE` (stale state was installed as the checkpoint).
- [ ] AC5/AC6 node-22 live acceptance (rollout, tracked in #1164 watch): xinanjiang rerun (gfs 2026072500, dg_0a50ecb0…) exits 0 with `*.f012.cfg.ic.update` header 720; one full 17-basin cycle with complete f012 coverage including the fastest basin.

## Non-goals

- Watcher redesign / poll-interval tuning.
- SHUD solver changes.
- Scheduler retry-chain behavior (#1203).

## Review focus

- Recovery determinism claim: same staged IC/forcing, END-only rewrite — is any input mutated between original run and rerun?
- cfg restore coverage: every exit path (timeout, OSError, gate failure, success) restores the original cfg.
- Gate parity: `install_recovered` gates are provably identical to watcher `_capture` gates.
- Timeout/resource bounding of the extra rerun in production (Slurm task wall-time interaction).
- Manifest consumer tolerance (`state_cli.py`) for the new keys.
