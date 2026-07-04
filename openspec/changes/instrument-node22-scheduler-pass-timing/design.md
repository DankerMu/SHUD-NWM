## Context

Node-22 hosts a db-free production scheduler that runs `python -m services.orchestrator.cli plan-production --submit --continuous --max-passes 1` under a systemd timer. One pass = one `run_once()` call inside `scheduler_runtime.py`. Empirically (2026-07-04 evening baseline) one pass burns 1h04–2h43 wall-clock and 1h02–1h58 CPU while submitting 48–104 Slurm jobs across the five pipeline stages — `convert / forcing / forecast / parse / state_save_qc` (per `services/orchestrator/chain_repository_state.py:17` `_FORECAST_STAGE_ORDER` and `services/orchestrator/scheduler.py:325` / `services/orchestrator/scheduler_preflight.py:22` `SLURM_ARRAY_STAGE_NAMES`) — for 13 basin models × 2 forecast sources.

The pass architecture chains stages **inline**: `_submit_and_wait_cycle_stage` → `_submit_and_wait` → `_poll_until_terminal` blocks python-side for the duration of a Slurm stage before dispatching the next. This means a large fraction of every pass's wall-clock is spent on Slurm queue/compute (dominated by SHUD forecast, which is out of scope for this project) and only a bounded slice is addressable python-side overhead.

Grill outcomes established:

- SHUD runtime is out of scope (separate project).
- Slurm compute capacity is the wall-clock hard ceiling; parallelising `gfs` and `ifs` source cohorts yields ~zero net gain because SHUD queues at the capacity wall.
- Python-side addressable budget is ~25–45 min per pass (rough estimate from reverse-engineered breakdown of P3 = 2h43).
- The current planner has **zero timing instrumentation**; all attribution is inferred from `sacct` submit timestamps and `systemctl` boundaries, which cannot separate python-time from slurm-wait.

Downstream optimisation change (change #2) needs evidence to rank interventions (`restart_reconcile` fast-path, per-pass shared journal cache, per-cohort array batching). Without instrumentation, that ranking is intuition, and intuition already failed once this session (I over-claimed a 4× speedup that was actually a first-submission sampling artefact).

**Constraints:**

- `.claude/CLAUDE.md`: 一律用 `uv run`；不引入非 stdlib 依赖。
- Node-22 does not connect to any live database; timing must not require an outbound network sink.
- Scheduler pass evidence JSON is a spec contract of `compute-scheduler-operationalization`; additions must be additive.
- Systemd-journald already rotates stdout for `nhms-compute-scheduler.service`; do not add a second log rotation surface.
- Instrumentation overhead must be undetectable (< 0.1 %) so it never becomes the reason a future pass slows down.

## Goals

- Every production pass emits durable `timing:` evidence covering pass / stage / candidate layers with strict `python_time_ms` vs `slurm_wait_ms` split.
- One structured JSON line per pass or stage boundary reaches stdout so an operator running `journalctl --user -u nhms-compute-scheduler.service -f` sees live phase transitions without tailing NFS artefacts.
- `NHMS_SCHEDULER_TIMING_LEVEL` env variable controls candidate-layer verbosity; pass and stage layers are always on.
- Node-22 scheduler evidence directory is bounded by a retention timer analogous to `nhms-node27-raw-retention.timer`.
- Single production pass (~2h wall) is sufficient to rank downstream optimisation candidates (B/C/D from grill) with a Top-3 time-consumer ranking.

## Non-Goals

- Any change to scheduler pass shape, `--continuous` semantic, or wall-clock bounds. Deferred to change #2 based on data.
- Any change to `%N` array throttle, per-basin `sbatch` batching, or `_build_candidates` cross-candidate cache. Deferred to change #2.
- Prometheus, OpenTelemetry, or any external metrics surface. Deferred to a separate governance change if ever needed.
- Node-27 side observability (autopipe, display API, ingest). Node-27 already has its own retention and observability chain.
- SHUD forecast runtime tuning (declared out of project scope by user).

## Decisions

### D1. Timing collector = per-pass `SchedulerPassTiming` object, not a global registry

**Chosen:** A `SchedulerPassTiming` instance is constructed at `run_once` entry, threaded via `SchedulerExecutionContext`, and finalised at pass exit. It exposes `pass_span()`, `stage_span()`, `candidate_span()` context managers that record `time.monotonic()` deltas into nested dicts. On pass exit the collector serialises to JSON, is attached to the evidence artefact and to a single-line stdout log record.

**Alternatives considered:**

- A module-global registry (like `logging`) — rejected because concurrent stage cohorts (dispatched via `run_concurrent_submissions` with `max_workers=context.config.concurrent_submit_bound`, default `DEFAULT_CONCURRENT_SUBMIT_BOUND=4` per `services/orchestrator/scheduler.py:314`, env-tunable via `NHMS_SCHEDULER_CONCURRENT_SUBMIT_BOUND`) would need thread-local isolation to avoid cross-worker span interleaving, adding complexity for zero benefit; per-pass instance is naturally scoped.
- Reusing OpenTelemetry / a tracer library — rejected: adds a runtime dependency, violates "no non-stdlib deps" rule, and its output surfaces (OTLP, Jaeger) require an external collector that node-22 explicitly does not run.

**Rationale:** the collector's lifetime is the pass; a per-pass object is the simplest correct thing.

### D2. `python_time_ms` vs `slurm_wait_ms` split, with restart_reconcile as a first-class pseudo-stage, honoring concurrency

**Chosen:** Two instrumentation strategies compose:

1. **Slurm wait is direct-measured, always.** Every place the scheduler blocks on Slurm gets an explicit `slurm_wait` sub-span. That includes both branches inside `chain_forecast_execution._submit_and_wait` (`services/orchestrator/chain_forecast_execution.py:505,568-582`): the `slurm_client.submit_job(payload)` call (needed for the already-terminal-on-submit fast path at L568-572 where `_poll_until_terminal` is never called) AND the `_poll_until_terminal` call at L574-582 (used when the initial status is non-terminal). Both wraps are attributed to `stage.slurm_wait_ms` so the split is a direct measurement at every real Slurm boundary, not an inference. Additionally, `restart_reconcile` (`services/orchestrator/scheduler_runtime.py:543` `self._run_restart_reconcile()`) polls `sacct` via `_scheduler.run_restart_reconcile` and consumes real wall-clock outside any stage; it is instrumented as a first-class pseudo-record `timing.restart_reconcile` with its own `python_time_ms` and `slurm_wait_ms` fields.

2. **Stage-level python vs slurm split honors concurrency.** For each stage record we capture `stage_started_at` and `stage_finished_at` (monotonic ns since pass entry), `dispatch_ms` (python-only work), and `slurm_wait_ms` (sum of direct `submit_job` + `_poll_until_terminal` spans inside the stage). At pass finalisation `pass.slurm_wait_ms` is the **union of intervals** of every stage's `[stage_started_at + dispatch_ms, stage_finished_at]` window **plus** `timing.restart_reconcile.slurm_wait_ms`, computed as `union_ms(intervals)` where `intervals` is the merged sorted list. When `concurrent_submit_bound == 1` this equals `sum(stage.slurm_wait_ms) + restart_reconcile.slurm_wait_ms`; when `> 1` the union is strictly smaller than the naive sum because overlapping stage waits collapse. `pass.python_time_ms = pass.total_wall_ms − pass.slurm_wait_ms`.

3. **`total_cpu_ms` comes from `time.process_time()`.** At the very first statement of `run_once` (before any preflight check, before `pass_id` minting is even echoed) we snapshot `_cpu_start = time.process_time()`; at `finalize_evidence()` we compute `total_cpu_ms = int((time.process_time() − _cpu_start) * 1000)`. This is stdlib-only and honors the "no non-stdlib deps" rule. CPU accounting is pass-level only (Requirement 1); it deliberately does not attribute to stages because subprocess/subprocess-poll CPU is not attributable to the calling python thread with per-stage granularity.

**Alternatives considered:**

- Sampling `getrusage()` to separate CPU vs wall at stage granularity — rejected because the pass mostly waits on subprocess (`sbatch`, `sacct`) and `_poll_until_terminal` uses `time.sleep`, so getrusage during Slurm waits underreports subprocess CPU and misclassifies sbatch subprocess time. Pass-level `process_time()` is unambiguous and cheap.
- Instrumenting inside `_poll_until_terminal` — rejected because that function is called from several sites; wrapping at the call sites via `stage_span("slurm_wait")` keeps the poll implementation untouched.
- Naive `sum(stage.slurm_wait_ms) == pass.slurm_wait_ms` invariant — rejected because concurrent stage cohorts break it (see D1); the union-of-intervals formulation is the correct semantics under any `concurrent_submit_bound`.

**Rationale:** the split has to be trustworthy or the whole change is worthless. Wrapping the exact submit + poll calls at each dispatch site is a direct measurement of "waiting on Slurm"; interval-union at pass level is the only formulation that reduces to the intuitive sum when serial and stays correct when parallel. `process_time()` gives us CPU without pulling in a second clock strategy.

### D3. Output surface = evidence JSON `timing:` block + one stdout JSON line per pass/stage boundary

**Chosen:** Evidence JSON gains a top-level `timing:` block with the full three-layer breakdown. Stdout receives one JSON line per pass entry, per stage entry, per stage exit, and per pass exit; candidate-layer events are not written to stdout. Journald captures stdout automatically.

**Alternatives considered:**

- Only evidence JSON — rejected: no live progress. Operator running `journalctl -f` during a 2h43 pass would still be blind, which is the grill Q3 exact concern.
- Only stdout — rejected: no durable cross-pass comparison. `journalctl` output is line-oriented and awkward to aggregate; the retention window is set by journald not by us.
- New sidecar artefact under evidence root (`pass_id.timing.json`) — rejected: an extra file per pass doubles the artefact count and complicates retention. Embedding in the existing artefact is cheaper.

**Rationale:** two surfaces map to two distinct users — evidence JSON for post-mortem + regression detection, stdout for live progress. Both are free reuse of existing infrastructure.

### D4. `NHMS_SCHEDULER_TIMING_LEVEL` env: `pass|stage|candidate`, default `stage`, validated inside `run_once` (fail-closed with timing.pass evidence)

**Chosen:** Env var is read on config load into `scheduler_config.py` for downstream use, but **validation** happens as the first act of `run_once` after `pass_id` is minted (and after the `SchedulerPassTiming` object is constructed — the very first statement of `run_once`). Rationale for validation timing: the spec's "pass-layer timing is always emitted" invariant (Requirement 1) requires `timing.pass` to exist for every `run_once` invocation, including the unknown-level case. Validating at config load would raise `ValueError` before `pass_id` and `timing.pass` exist, forcing the daemon to crash at startup with no evidence artefact — that would break the "always emitted" contract. Validating at pass entry lets us emit a normal `timing.pass` block with `status="preflight_blocked"` and `reason="scheduler_timing_level_unrecognised"`, keeping every downstream analyzer's invariants intact.

Level `pass` records only pass-boundary spans. Level `stage` records pass + stage spans. Level `candidate` records everything including per-basin sub-phases keyed on `(basin_model_id, source_id, stage_name)` — the concrete sub-phases inside `services/orchestrator/scheduler_execution.py:233-410 execute_candidate_cohort` are `output_uri_lookup`, `basin_manifest_build`, `slurm_env_check`, `secret_manifest_scan`, `resource_profile_check`, `stage_raw_input`, `orchestrator_dispatch`; the sub-phases inside `services/orchestrator/chain_forecast_execution.py:489-599 _submit_and_wait` are `build_stage_manifest` (L498), `submit_sbatch` (L505, direct-measured Slurm wait as per D2), `poll_until_terminal` (L574-582, direct-measured Slurm wait as per D2), `post_stage_hook` (L590-594). Default `stage` gives complete observable behaviour without candidate-layer volume.

**Alternatives considered:**

- Always-on candidate layer — rejected: 13 basins × 2 sources × 5 stages × up to ~11 sub-phases ≈ ~1430 timing entries per pass in the evidence JSON at the theoretical ceiling (real numbers are lower because each stage triggers only its own sub-phase set); either way volume dominates when nothing pathological is happening.
- Validating at config parse — rejected: violates Requirement 1 "pass-layer timing is always emitted" (see rationale above). The daemon would exit before `timing.pass` is written; downstream regression tooling has no artefact to compare against.
- Boolean on/off — rejected: gives up the ability to keep the cheap pass+stage layer permanently on while opting into candidate layer only during optimisation windows or postmortems.
- Per-layer independent switches — rejected as overkill; the three levels are naturally ordered, one env var stays memorable.

**Rationale:** grill Q4 concluded permanence is a health-monitoring feature (regression detection), so pass + stage stay on always; candidate is diagnostic and belongs behind a switch. Fail-closed at pass entry (not config load) preserves the "always emit timing.pass" contract without swallowing the misconfiguration.

### D5. Evidence retention = systemd user timer + retention script; same architecture as `nhms-node27-raw-retention.timer`

**Chosen:** New `nhms-scheduler-evidence-retention.{service,timer}` under `~/.config/systemd/user/` invoking `scripts/node22_scheduler_evidence_retention.py`. Default policy: keep artefacts younger than `NHMS_SCHEDULER_EVIDENCE_RETENTION_DAYS` (default 90) up to `NHMS_SCHEDULER_EVIDENCE_MAX_MB` (default 512); delete oldest first when over cap. Fires every 24 hours. Script emits a retention receipt JSON alongside its own deletions.

**Alternatives considered:**

- Retention inline at pass exit — rejected: entangles scheduler pass with disk maintenance; a bug in retention would kill scheduling.
- `find … -delete` cron one-liner — rejected: no receipt of what was deleted; no cap-based policy, only age-based; not consistent with the project's existing retention architecture.
- Skip retention this change, do it separately — rejected: current growth is ~8 MB/day → 240 MB/month; the retention gap is real and the fix is 30 lines of Python.

**Rationale:** existing `nhms-node27-raw-retention` proves the pattern works. Reuse it, don't invent.

### D6. Timing collector uses `time.monotonic()` deltas only; no wall-clock, no `time.time()`

**Chosen:** All durations are computed as `monotonic()` deltas. Timestamps in the timing block use `_now(self.config)` (which is already the scheduler's UTC clock source) for `pass_started_at` and `pass_finished_at`, but never for measuring durations.

**Alternatives considered:** `time.perf_counter()` — equivalent precision but the codebase already uses `time.monotonic()` in `_poll_until_terminal`; sticking with `monotonic` keeps one clock source across the module.

**Rationale:** monotonic is immune to NTP jumps and is the standard practice for measured durations.

### D7. Stdout JSON line uses one line per phase, no multi-line output, versioned by schema

**Chosen:** Every stdout line is a complete, self-delimited JSON object containing at minimum `schema_version` (`"nhms.scheduler_pass_timing.v1"`), `ts` (UTC ISO 8601), `pass_id`, `level`, `phase`. journald parses this cleanly and downstream tools (`jq`, `grep`, `awk`) can filter by `level` / `phase`. No pretty-printing, no continuation lines. The same `schema_version` string is also written to the top-level of `timing.pass` in the evidence JSON so evidence and stdout consumers can version-gate together.

**Rationale:** journald is line-oriented; multi-line JSON fragments render as separate log records and break tooling. The `schema_version` follows the same convention already used by `scheduler_evidence.py` (see e.g. `"nhms.production_scheduler.pre_execution_evidence_reservation.v1"`), so future timing schema changes can be safely rolled out with parallel consumer support.

### D8. `infra/env/compute.scheduler-dbfree.env.example` is born in this change

**Chosen:** Node-22 currently runs its db-free scheduler off `/scratch/frd_muziyao/NWM/infra/env/compute.scheduler-dbfree.env` (per `receipts/2026-06-28-node22-dbfree-scheduler-live-proof.md` line 15 for the path and line 51 for the systemd `EnvironmentFile=` binding), but no `.example` template has ever been committed to git. This change creates that template — `infra/env/compute.scheduler-dbfree.env.example` — alongside adding the new `NHMS_SCHEDULER_TIMING_LEVEL` entry, so operators bringing up a fresh node-22 or a lab peer have a diffable canonical starting point.

The template contents SHALL enumerate the on-node canonical keys observed in the receipt: `NHMS_SCHEDULER_DB_FREE_REQUIRED=true`, `NHMS_SCHEDULER_LOCK_ROOT`, `NHMS_SCHEDULER_STATE_BACKEND=file`, `NHMS_SCHEDULER_LOCK_BACKEND=file`, `NHMS_SCHEDULER_REGISTRY_BACKEND=file`, `NHMS_SCHEDULER_REGISTRY_MANIFEST`, `NHMS_SCHEDULER_CANONICAL_READINESS_BACKEND=file`, `NHMS_SCHEDULER_CANONICAL_READINESS_INDEX`, `NHMS_SCHEDULER_JOURNAL_BACKEND=file`, `NHMS_SCHEDULER_JOURNAL_ROOT`, `NHMS_SCHEDULER_STATE_INDEX_BACKEND=file`, `NHMS_SCHEDULER_STATE_INDEX`, `NHMS_SCHEDULER_EVIDENCE_ROOT`, `NHMS_SCHEDULER_RUNTIME_ROOT`, `NHMS_SCHEDULER_TEMP_ROOT`, `NHMS_SCHEDULER_ALLOWED_ROOTS`, `NHMS_SCHEDULER_SOURCES`, `NHMS_SCHEDULER_ALLOWED_CYCLE_HOURS_UTC`, plus the new `NHMS_SCHEDULER_TIMING_LEVEL`, `NHMS_SCHEDULER_EVIDENCE_RETENTION_DAYS`, and `NHMS_SCHEDULER_EVIDENCE_MAX_MB`.

**Alternatives considered:**

- Reuse `infra/env/compute.example` and just add the new key there — rejected because `compute.example` already has role scope overlap with the broader compose env and lacks a "scheduler-dbfree" gate; grafting timing knobs onto it would blur the on-node file boundary that the receipt documents.
- Skip the template, only document the new key in the runbook — rejected because a spec-diff reader coming to this change 6 months from now would need to reverse-engineer the on-node env from receipts; the template gives them a single-file source of truth.

**Rationale:** the template is a one-shot cost that unblocks fresh bring-up and standardises the diff surface. The change is the natural moment to introduce it because we already have to touch the same file for the new env var.

## Risks / Trade-offs

**[Risk 1] Timing collector adds overhead to hot path** → **Mitigation:** Only ~108 `time.monotonic()` calls + dict updates per pass at candidate level (fewer at lower levels). Total instrumentation overhead measured as < 1 ms on a 2h43 pass = 1e-5 fraction. Add a unit test that records elapsed time of an empty pass with and without instrumentation and asserts the delta is bounded.

**[Risk 2] `slurm_wait_ms` split is wrong at a mis-wrapped call site** → **Mitigation:** every dispatch site is grepped for `_poll_until_terminal` and wrapped in `stage_span("slurm_wait")`; a unit test with a stub gateway that returns after `time.sleep(0.1)` verifies the split assigns those 100 ms to `slurm_wait_ms` and not `python_time_ms`.

**[Risk 3] Evidence JSON grows past what downstream consumers parse** → **Mitigation:** even at candidate level, timing block is bounded by `13 basins × 2 sources × 5 stages × up to ~11 sub-phase keys ≈ ~1430` numeric entries plus keys, ~55 KB max at the theoretical ceiling (real numbers land lower because each stage only exercises its own sub-phase set — e.g. sub-phases inside `_submit_and_wait` do not apply to stages that never reach `_submit_and_wait`). Existing evidence artefacts are already 100 KB+, so relative growth stays below ~55 %. Retention timer bounds long-term disk (default 512 MB cap, see D5 and D8).

**[Risk 4] Retention script deletes an in-flight `pre_execution.json`** → **Mitigation:** retention script matches files older than `NHMS_SCHEDULER_EVIDENCE_RETENTION_DAYS` (default 90) only; in-flight pre-execution artefacts are always fresh. Additionally skip files whose sibling `.tmp` or `.lock` exists.

**[Risk 5] `NHMS_SCHEDULER_TIMING_LEVEL` set to an unknown value silently defaults to `stage`** → **Mitigation:** validate inside `run_once` (per D4) after `pass_id` mint and `SchedulerPassTiming` construction: if the resolved level is not one of `pass|stage|candidate`, the pass returns `SchedulerPassResult(status="preflight_blocked", ...)` with `reason="scheduler_timing_level_unrecognised"` and a `timing.pass` block populated with `total_wall_ms == python_time_ms` (no Slurm dispatch happened) and the enumerated valid values in the error message. This keeps Requirement 1's "always emit timing.pass" invariant intact rather than pretending everything is fine and rather than crashing the daemon at startup.

**[Risk 6] Sampling one pass to rank optimisation candidates could still lead to bad decisions** → **Mitigation:** grill Q6 acknowledged single-pass suffices only for initial ranking; change #2 must reference 3–5 passes' worth of stage-layer data before locking a target. This design does not commit change #2 to a decision.

## Migration Plan

1. **Local**: implement `services/orchestrator/scheduler_timing.py`, wire into `scheduler_runtime.run_once`, add unit tests, `uv run ruff check .`, `uv run pytest -q tests/test_production_scheduler.py tests/test_file_orchestration_journal.py` full-green gate.
2. **Local**: write retention script + systemd units, add a unit test that fakes an evidence dir and verifies cap-based deletion.
3. **Push to master, node-22 `git pull --ff-only`, redeploy systemd units** (`systemctl --user daemon-reload && systemctl --user enable --now nhms-scheduler-evidence-retention.timer`).
4. **Live-verify**: run one production pass, confirm `timing:` block present in evidence JSON, confirm `journalctl --user -u nhms-compute-scheduler.service` shows structured stage lines, confirm python-time / slurm-wait split adds up to total wall.
5. **Post-verify**: collect 3–5 pass timing blocks from live evidence, produce a Top-3 python-time-consumer ranking, and pass that to change #2 as its Stage-1 input.

**Rollback:** since instrumentation is additive and env-controlled, rollback is either (a) `git revert` the change or (b) `NHMS_SCHEDULER_TIMING_LEVEL=pass` to silence stage / candidate output while keeping pass-level regression detection. Retention timer is independently `systemctl --user disable --now`-able.

## Verification Approach

- Unit tests exercise the collector with fake spans and assert (a) python-time + slurm-wait = total wall, (b) candidate-level output disappears at `NHMS_SCHEDULER_TIMING_LEVEL=stage`, (c) unknown env value raises.
- Contract test asserts the evidence JSON schema still validates after the `timing:` block is added.
- Live receipt: one production pass on node-22 with `NHMS_SCHEDULER_TIMING_LEVEL=candidate`, evidence JSON attached, structured journald excerpt attached, and a `python_time_ms + slurm_wait_ms ≈ total_wall_ms` assertion on real data.
- Retention live receipt: point retention script at a synthetic dir with mixed-age files, confirm expected deletions + retention receipt.
