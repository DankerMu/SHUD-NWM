# Tasks: fix-shud-checkpoint-capture-race

Fixture level: expanded · Repair intensity: high · Project profile: NHMS

## Change surface

- `workers/shud_runtime/runtime.py`: `run_shud` miss-handling branch + shared
  solver deadline, new `_recover_missing_state_checkpoints` (incl. scratch-root
  fresh-scoping, per-hour exception containment, per-hour outcome recording),
  `_clear_recovery_scratch_root` (two-pass, returns a refusal reason),
  `_StateCheckpointTracker` (`observed_header_minutes`, `recovery_outcomes`,
  `install_recovered`, `write_manifest` payload).
- `tests/test_shud_runtime.py`: `_FAST_SOLVER_STUB`, `_PROJECT_FAST_SOLVER_STUB`,
  `_STUCK_HEADER_SOLVER_STUB`, `_IC_DERIVED_SOLVER_STUB`,
  `_FAILING_RECOVERY_SOLVER_STUB`, `_HANGING_RECOVERY_SOLVER_STUB`, recovery
  scenario tests (fast-miss recovery in both command styles, multi-hour,
  IC-derived body oracle, 100-repeat determinism, gate/diagnostics, slow-leg
  speed independence, stale scratch lane, unclean scratch refusal, per-hour
  cfg-write failure, rc≠0 lane, shared timeout budget; round-3 complement
  closures: success-path manifest deliverable, no-hours manifest guard,
  cfg-read/install-write lanes, the four scratch-clear refusals + the
  vanished-entry tolerance, refusal-log best-effort).
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
- Failure paths/rollback/stale state: recovery timeout → kill+continue; gate failure → unlink candidate; cfg restored in `finally` on every path (restore failure recorded, never masks the original error); **any** per-hour recovery failure — cfg-write, log-open, spawn/wait, **and install-time IO** (`OSError`, `SHUDRuntimeError`, or `SafeFilesystemError` from the no-follow/safe-fs helpers; the per-hour containment boundary is ONE try around the whole hour body including `install_recovered`) — skips that hour only — remaining hours still attempted, `write_manifest` still runs, error code stays `STATE_CHECKPOINTS_MISSING`; the manifest write itself is best-effort — a diagnostics-write failure must never replace `STATE_CHECKPOINTS_MISSING` as the run's error code; scratch root `state_checkpoint_recovery/f{hour:03d}/` must be fresh-scoped — a stale `<proj>.cfg.ic.update` left by an earlier attempt (reused run workspace) must never be installable without a successful same-invocation rerun, and a refusal to clear it is recorded as that hour's outcome, never half-clears the dir.
- Evidence/audit/readiness: `observed_header_minutes` in manifest + error message — the manifest is written whenever the checkpoint lane is reached after a successful main solve (including the total-miss case; main-solve `SHUD_TIMEOUT`/`SHUD_EXIT_<rc>` raise before the checkpoint lane and are out of scope for this row); per-hour recovery outcomes (`recovered` / `skipped_scratch_unclean` / `spawn_failed` / `wait_failed` / `log_open_failed` / `timeout` / `exit_<rc>` / `gate_rejected` with the rerun's header minute / `budget_exhausted` / `cfg_write_failed` / `cfg_read_failed` / `scratch_dir_failed` / `install_failed` / `install_write_failed` / `cfg_restore_failed` appended with `+`) recorded in manifest + failure message; per-hour recovery logs in `log_dir`.
- Resource bound: total solver wall time of `run_shud` (main solve + all recovery reruns) is bounded by ONE `timeout_seconds` budget via a shared monotonic deadline; recovery hours with no remaining budget are skipped with outcome `budget_exhausted`.

Regression rows:
- Fast solve, watcher misses f012, recovery rerun succeeds → checkpoint installed with header at target minute, `provenance=post_run_recovery`, run exits 0.
- Recovery rerun produces wrong-header state (stuck stub) → candidate unlinked, `STATE_CHECKPOINTS_MISSING` raised with observed-header trail, no checkpoint file left behind.
- Recovery rerun produces a header-matching but structurally incomplete state (truncated body, one river row short) → structure gate rejects, candidate unlinked, hour stays missing (e13ae809 semantics on the recovery producer).
- Normal-speed run, watcher captures all hours → recovery never invoked; entries identical to pre-change (unchanged sibling behavior).
- `state_cli` reads a manifest with `observed_header_minutes` + `provenance` → checkpoints parsed exactly as before (consumer compatibility).
- Scratch root pre-seeded with a stale gate-valid `<proj>.cfg.ic.update` and the recovery rerun exits 0 without writing a state (or fails rc≠0/timeout) → stale candidate is NOT installed, hour stays missing, hard failure raised.
- Total miss (nothing captured, recovery fails) → `state_checkpoints.json` still written with empty `checkpoints`, the `observed_header_minutes` trail, and per-hour recovery outcomes.
- Recovery rerun whose output provably depends on the staged IC content → recovered checkpoint body reflects the staged IC (deterministic-derivation oracle, not header-only).

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
- [x] AC8: `uv run pytest -q tests/test_shud_runtime.py tests/test_state_manager.py tests/test_warm_start_chaining.py` (317 passed after the round-3 complement-audit pass; 307 after round-2) + `uv run ruff check .` clean.
- [x] Consumer tolerance (Schema pack): `test_state_checkpoint_manifest_reader_ignores_runtime_diagnostic_keys` — `_load_state_checkpoint_manifest` fed the same manifest with and without the full runtime diagnostic shape (top-level `observed_header_minutes` **and** `recovery_outcomes`, entry-level `provenance`) → identical `StateCheckpoint` list (valid_times, lead_hours, filenames, referenced bytes, output-relative paths). (Re-tick after r2-test-06 payload sync.)
- [x] Stale scratch lane (File IO pack): `test_run_shud_recovery_never_installs_stale_scratch_state` — `state_checkpoint_recovery/f012/demo.cfg.ic.update` pre-seeded gate-valid, rerun exits 0 without writing state → stale file cleared and NOT installed, `STATE_CHECKPOINTS_MISSING` raised. Red vs pre-fix `runtime.py`: `Failed: DID NOT RAISE` (stale state was installed as the checkpoint).
- [ ] AC5/AC6 node-22 live acceptance (rollout, tracked in #1164 watch): xinanjiang rerun (gfs 2026072500, dg_0a50ecb0…) exits 0 with `*.f012.cfg.ic.update` header 720; one full 17-basin cycle with complete f012 coverage including the fastest basin.

Round-1 verified-finding closures (Phase 5/6 fix pass):

- [x] cand-01: total-miss run → `state_checkpoints.json` exists with `checkpoints: []` + `observed_header_minutes` — asserted in `test_run_shud_recovery_keeps_partial_gates_and_reports_observed_headers`. Red vs pre-fix `runtime.py`: `FileNotFoundError: .../state_checkpoints/state_checkpoints.json`.
- [x] cand-07: per-hour recovery outcome trail (`recovery_outcomes` in manifest + `; recovery outcomes: f012=…` in the failure message), incl. the rerun's own header minute on gate rejection — `test_run_shud_recovery_keeps_partial_gates_and_reports_observed_headers` (`gate_rejected(header=1440)`), `test_run_shud_recovery_never_installs_stale_scratch_state` (`gate_rejected(no_state)`), `test_run_shud_recovery_reports_non_zero_exit_lane` (`exit_1`). Scratch-refusal lane additionally writes its reason into `log_dir/state_checkpoint_recovery_f{hour:03d}.err.log`.
- [x] cand-05: forced cfg-write `SHUDRuntimeError` on one hour of `[6,12]` → that hour skipped, other hour still recovered, `STATE_CHECKPOINTS_MISSING` raised, manifest written, cfg restored — `test_run_shud_recovery_skips_only_the_hour_whose_cfg_write_fails`. Red vs pre-fix: `assert 'WORKSPACE_WRITE_FAILED' == 'STATE_CHECKPOINTS_MISSING'`.
- [x] cand-06: scratch root with a non-regular entry → hour skipped with recorded outcome `skipped_scratch_unclean`, dir NOT half-cleared (two-pass stat-then-unlink), reason in the per-hour err log — `test_run_shud_recovery_refuses_unclean_scratch_without_half_clearing_it`. Red vs pre-fix: `FileNotFoundError: .../state_checkpoint_recovery/f012/aaa.txt` (the pre-fix single pass had already unlinked it before refusing).
- [x] cand-04: main solve consuming most of a small `timeout_seconds` + hanging recovery stub → total `run_shud` wall time bounded by one shared monotonic deadline; hour with no budget left is skipped with `budget_exhausted` — `test_run_shud_main_solve_and_recovery_share_one_timeout_budget`. Red vs pre-fix: `assert 10.54 < (1.5 * 6)`.
- [x] cand-02/cand-13: `test_run_shud_recovers_watcher_missed_checkpoint_via_deterministic_rerun[cfg|shud_project]` (project leg rewrites tab-separated `END` in days, cfg byte-identical after) and `test_run_shud_recovers_every_missing_hour_with_scoped_scratch_and_logs` (`[6,12]` → headers 360/720, scratch dirs `f006`/`f012`, both log pairs, ordered `checkpoints`).
- [x] cand-11: `test_run_shud_recovery_reports_non_zero_exit_lane` (rc≠0 → outcome `exit_1`, no checkpoint file, solver stderr preserved) and `test_run_shud_main_solve_and_recovery_share_one_timeout_budget` (sleeping recovery leg → bounded wall time, outcome `timeout`/`budget_exhausted`).
- [x] cand-12: `cfg_path.read_bytes()` equality before/after `run_shud` in the fast (both command styles), multi-hour, cfg-write-failure, stuck-header, and stale-scratch tests.
- [x] cand-08 (oracle half): `test_run_shud_recovered_checkpoint_body_derives_from_the_staged_ic` — stub carries the staged `demo.cfg.ic` body forward, recovered f012 body equals it (green pre-fix by design: this strengthens the oracle, it does not change runtime behavior); runtime input-checksum guard deferred to AC5/AC6 rollout receipt.
- [x] cand-14 (ride-along): `test_run_shud_recovers_watcher_missed_checkpoint_via_deterministic_rerun` asserts `FakeHydroRunRepository.created`/`statuses`/`failures` all stay empty across a recovery `run_shud`. (Superseded by round-2 r2-test-05: assertion tautological at `run_shud` level; real oracle is the `execute()`-level closure below.)

Round-2 verified-finding closures (Phase 5/6 fix pass, round-2 cross-review):

- [x] CAND-1 (P1, install containment): the per-hour containment boundary is ONE `try/except (OSError, SHUDRuntimeError, SafeFilesystemError)` around the whole hour body INCLUDING `install_recovered`; an install-time failure records a per-hour outcome (`install_failed`), remaining hours still attempted, manifest written, error code `STATE_CHECKPOINTS_MISSING`. Test: hours `[6,12]`, injected read failure during hour-6 install → hour 12 recovered, manifest exists, both hours in `recovery_outcomes`. Red vs 6a99c72b required.
- [x] CAND-2 (P2, manifest masking): `write_manifest()` call is best-effort — a forced manifest-write failure on a total-miss run leaves error code `STATE_CHECKPOINTS_MISSING` (never `WORKSPACE_WRITE_FAILED`). Red vs 6a99c72b required.
- [x] r2-test-01 (P2, structure gate): recovery stub writing header-720 but truncated body (one river row short) → structure gate rejects, target unlinked, hour missing. Mutation-killing: this test must fail if the `state_ic_structure_complete` clause is removed from `install_recovered`.
- [x] r2-test-03 (P3, budget lane): hours `[6,12]` + hanging recovery stub → deterministic `{"6": "timeout", "12": "budget_exhausted"}` (no disjunctive assertion for the second hour).
- [x] r2-test-04 (P3, restore lane): forced restore-write failure (seam: `_write_text_no_follow` failing when `content == original_cfg`) → `cfg_restore_failed` recorded (appended with `+` per its lane position), original error not masked.
- [x] r2-test-05 (P3, cand-14 real oracle): `execute()`-level test with fast recovery stub → repository sees exactly one created run with normal status progression; no extra runs from recovery.
- [x] r2-test-06 (P3, consumer payload): consumer-tolerance test payload includes top-level `recovery_outcomes`; docstring synced.
- [x] Hygiene rider (from DISCARDed r2-corr-02): recovery cfg write re-terminates (`content.rstrip() + "\n"`), matching `generate_cfg_para`.

Round-3 verified-finding closures (Phase 5/6 fix pass, round-3 cross-review):

- [x] r3-test-01 (CONFIRMED, success-path manifest re-raise): with every requested hour captured, `state_checkpoints.json` is the DELIVERABLE warm-start chaining reads, so its write failure stays a hard failure with the workspace error code — `test_run_shud_manifest_write_failure_fails_the_run_when_nothing_is_missing` (slow-leg watcher capture + `_write_text_no_follow` failing only for `state_checkpoints.json`) asserts `WORKSPACE_WRITE_FAILED`, the f012 file present, the index absent, no recovery scratch root. Red vs `runtime.py:616-620` deleted: `Failed: DID NOT RAISE`.
- [x] r3-corr-01 (PLAUSIBLE, budget-test timing margin — supersedes the round-2 r2-test-07 deferral): `test_run_shud_main_solve_and_recovery_share_one_timeout_budget` budget 6 → 10s with the stub's 4.5s main solve unchanged, so f006 keeps ~5.4s of shared budget (4.4s of runner slack before it would flip to `budget_exhausted`). Exact-equality outcome map and the no-spawn assertion kept, no disjunction introduced. Discrimination re-measured against per-rerun-timeout semantics: `assert 24.56 < (1.5 * 10)` FAILED.

Round-3 complement audit (closes the recurring class: a fix adds a conditional and ships one half untested). Every conditional half the PR introduces now has a named killing test, or a recorded reason. Added closures:

- [x] `write_manifest` requested-hours guard, TAKEN half: `test_run_shud_writes_no_checkpoint_manifest_when_no_hours_are_requested` — a run with no `state_checkpoint_hours` still publishes no `state_checkpoints/`. Red vs `if not self.targets` removed: the directory exists.
- [x] `capture_available` observed-header dedup: `..._solve_speed_independent_slow_leg` now asserts `observed_header_minutes == [720.0, 4320.0]`. Red vs dedup removed: 38 samples.
- [x] Empty observed-header rendering: `test_run_shud_fails_when_requested_state_checkpoints_are_missing` now asserts `header minutes: none`. Red vs the `else "none"` half removed: `minutes: )`.
- [x] Manifest-note rendering, complement half: `..._keeps_partial_gates_and_reports_observed_headers` now asserts the failure message carries NO `state_checkpoints.json write failed` note when that write succeeded. Red vs the ternary's `else ""` removed: `...write failed: None`.
- [x] Recovery cfg-read failure lane: `test_run_shud_recovery_records_cfg_read_failure_for_every_missing_hour` — every hour recorded `cfg_read_failed`, no scratch dir, code stays `STATE_CHECKPOINTS_MISSING`. Red: `WORKSPACE_READ_FAILED == STATE_CHECKPOINTS_MISSING`.
- [x] `install_recovered` publish-write lane: `test_run_shud_recovery_records_install_write_failure_distinctly` — `install_write_failed` is distinguishable from the outer `install_failed`. Red: outcome collapses to `install_failed`.
- [x] `_clear_recovery_scratch_root` refusal halves, one test each — symlink entry (`..._refuses_a_symlinked_scratch_entry_without_following_it`, link and its outside target both survive), unlinkable entry (`..._refuses_when_a_scratch_entry_cannot_be_removed`), entry-count bound (`..._refuses_a_scratch_dir_above_the_entry_bound`), unlistable dir (`..._refuses_an_unreadable_scratch_dir`), and the fail-OPEN half — an entry that vanishes between listing and stat is skipped, not refused (`..._tolerates_a_scratch_entry_that_vanishes_mid_scan`, run still recovers). Red for the four refusals: outcome degrades to `scratch_dir_failed` with no refusal log (bound lane: `DID NOT RAISE`); red for the tolerance lane: hard failure.
- [x] Refusal-log best-effort write (same class as CAND-2 manifest masking): `test_run_shud_recovery_refusal_log_failure_does_not_alter_the_hour_outcome` — outcome stays exactly `skipped_scratch_unclean`. Red: `skipped_scratch_unclean+scratch_dir_failed`.

Round-3 audit halves left uncovered (reason recorded, no test added):

- `run_shud` `if checkpoint_tracker.missing_hours():` complement — skipping the recovery call when nothing is missing is unobservable (the callee loops over an empty `missing_hours()`); full-file run stays green with the guard forced true. Redundancy, not behavior.
- `install_recovered` `if target_info is None or hour in self.captured` — dead from the only call site (invoked strictly for hours in `missing_hours()`); full-file run green with the guard forced false. Removal is a behavior-neutral simplification, deliberately NOT taken in a test-closure pass.
- `recovery_outcome_summary` empty-outcomes early return — unreachable through `run_shud`: every lane records an outcome for every hour it visits, so the map is non-empty at the raise site; full-file run green with the guard forced false.
- Post-`kill()` `process.wait(timeout=5)` `TimeoutExpired: pass` — needs an unkillable (uninterruptible-sleep) child; not constructible from a Python stub.
- `install_recovered` `captured_header_minute is None` sub-clause — needs a target readable by the writer but not by the reader in the same call; its sibling clauses (header mismatch, truncated body) already pin the reject-and-unlink behavior of the whole gate.
- `failure_outcome` sentinel assignments (`scratch_dir_failed` / `log_open_failed` / `spawn_failed` / `wait_failed`) — straight-line label precision, not conditionals; the containment behavior they annotate is pinned by the `cfg_write_failed` and `install_failed` tests.
- `SafeFilesystemError` member of the `write_manifest` catch tuple — unreachable in practice (`_write_text_no_follow`/`_ensure_directory` convert safe-fs failures to `SHUDRuntimeError`); kept as a defensive widening.

Round-2 deferrals (recorded): r2-test-07 (PLAUSIBLE P3, budget-test wall-clock margin on slow CI runners) — **superseded by r3-corr-01 above**: the margin was widened (budget 6 → 10s) once the discriminative bound was re-measured at the larger budget, so the trade-off that motivated the deferral no longer applies.

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
