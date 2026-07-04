# scheduler-pass-timing-instrumentation Specification

## Purpose

Every production scheduler pass on node-22 emits durable, structured timing evidence separating python-side planning cost from Slurm queue/compute wait, at pass / stage / candidate layers, so downstream optimisation decisions and long-term regression detection can rest on data rather than intuition.

## ADDED Requirements

### Requirement: Pass-layer timing is always emitted

The scheduler SHALL emit a pass-layer timing record for every `run_once` invocation regardless of `NHMS_SCHEDULER_TIMING_LEVEL`.

The record SHALL include: `schema_version` (fixed string `"nhms.scheduler_pass_timing.v1"`), `pass_id`, `pass_started_at` (UTC ISO 8601), `pass_finished_at` (UTC ISO 8601), `status` (mirroring `SchedulerPassResult.status`), `total_wall_ms` (from `time.monotonic()` deltas), `total_cpu_ms` (from `time.process_time()` deltas at pass boundary), `python_time_ms`, `slurm_wait_ms`.

The `SchedulerPassTiming` object SHALL be constructed as the **first statement** of `run_once` — before `root_preflight`, before `db_free_runtime_preflight`, before `NHMS_SCHEDULER_TIMING_LEVEL` validation, before every other side effect — so `timing.pass` is always populated even for the earliest-exit branches.

The invariant `python_time_ms + slurm_wait_ms == total_wall_ms` (± 5 ms tolerance for measurement noise) SHALL hold.

The `slurm_wait_ms` value SHALL be the **union of intervals** of every direct-measured Slurm wait span within the pass (per-stage `submit_job` + `poll_until_terminal` spans plus the `restart_reconcile` `sacct`-poll span), so the invariant remains correct whether stages execute serially or concurrently (`NHMS_SCHEDULER_CONCURRENT_SUBMIT_BOUND > 1`).

#### Scenario: Successful pass emits pass-layer timing

- **WHEN** a production `run_once` completes with `status="submitted"`
- **THEN** the pass evidence JSON contains a `timing.pass` object with all required fields including `schema_version="nhms.scheduler_pass_timing.v1"` and `total_cpu_ms >= 0`
- **AND** `python_time_ms + slurm_wait_ms` equals `total_wall_ms` within ±5 ms
- **AND** a single-line JSON record with `level="pass"` and `phase="finished"` is written to stdout with the same `schema_version` field.

#### Scenario: Every `SchedulerPassResult.status` value populates `timing.pass`

- **WHEN** `run_once` returns any of the enumerated `SchedulerPassResult.status` values — `submitted`, `preflight_blocked`, `lock_contended`, `lease_lost`, `resource_limit_blocked`, `slurm_status_synced`, `slurm_status_sync_failed`, `restart_reconciled`, `restart_reconcile_unknown`, `planned`, or any blocked-pass derivative status enumerated by `SchedulerPassResult`
- **THEN** the pass evidence JSON contains a `timing.pass` object with all required fields including `status` equal to the returned `SchedulerPassResult.status`
- **AND** the invariant `python_time_ms + slurm_wait_ms == total_wall_ms` (± 5 ms) holds for that status
- **AND** for statuses in which no Slurm dispatch occurred (`preflight_blocked`, `lock_contended`, `lease_lost`, `resource_limit_blocked`, `planned`, and blocked-pass derivatives) `slurm_wait_ms` is `0`.

#### Scenario: Blocked pass still emits pass-layer timing

- **WHEN** a pass returns early with `status="preflight_blocked"` or `status="lock_contended"`
- **THEN** the evidence JSON still contains a `timing.pass` object
- **AND** `slurm_wait_ms` is `0` because no Slurm dispatch occurred
- **AND** `python_time_ms` equals `total_wall_ms` within ±5 ms.

#### Scenario: Very-early preflight exit still emits `timing.pass`

- **WHEN** `run_once` short-circuits at `root_preflight` (before `db_free_runtime_preflight`, before any `NHMS_SCHEDULER_TIMING_LEVEL` handling)
- **THEN** the evidence JSON still contains a `timing.pass` object
- **AND** `total_wall_ms` is the small (typically <1 ms) time consumed by `root_preflight`
- **AND** `python_time_ms` equals `total_wall_ms` within ±5 ms and `slurm_wait_ms` is `0`.

### Requirement: Stage-layer timing is emitted at `NHMS_SCHEDULER_TIMING_LEVEL` ≥ `stage`

At level `stage` or `candidate`, the scheduler SHALL emit one stage-layer record per (source_id, cycle_id, stage_name) tuple executed within the pass. The `stage_name` domain is exactly the five entries of `services/orchestrator/chain_repository_state.py:17 _FORECAST_STAGE_ORDER`: `convert`, `forcing`, `forecast`, `parse`, `state_save_qc`.

Each stage record SHALL include: `source_id`, `cycle_id`, `stage_name`, `stage_started_ms_from_pass_entry`, `stage_finished_ms_from_pass_entry`, `build_candidates_ms`, `dispatch_ms`, `slurm_wait_ms`, `python_time_ms`, `total_wall_ms`, `basin_count`, `submitted_count`, `failed_count`.

`dispatch_ms` measures python-only work inside `services/orchestrator/scheduler_execution.py execute_candidate_cohort` up to the moment control leaves the cohort loop; `slurm_wait_ms` measures the union of the direct-measured `slurm_client.submit_job` and `_poll_until_terminal` spans inside `services/orchestrator/chain_forecast_execution.py:489-599 _submit_and_wait` (both call sites SHALL be wrapped so the already-terminal-on-submit fast path at L568-572 attributes correctly) and NO other work; `python_time_ms` = `build_candidates_ms + dispatch_ms`; `total_wall_ms` = `python_time_ms + slurm_wait_ms`.

The stage-record invariant `stage.python_time_ms + stage.slurm_wait_ms == stage.total_wall_ms` (± 5 ms) SHALL hold.

The pass-level invariant `union_ms(intervals=[(stage.stage_started_ms_from_pass_entry + stage.dispatch_ms, stage.stage_finished_ms_from_pass_entry) for stage in timing.stages] ∪ restart_reconcile_span) == pass.slurm_wait_ms` (± 5 ms cumulative) SHALL hold, where the union collapses overlapping intervals when `NHMS_SCHEDULER_CONCURRENT_SUBMIT_BOUND > 1`. When `NHMS_SCHEDULER_CONCURRENT_SUBMIT_BOUND == 1` (the default `DEFAULT_CONCURRENT_SUBMIT_BOUND` value in `scheduler.py:314` is `4`, but end-to-end serial execution keeps overlaps at zero anyway if the deployment sets the env to `1`) the union equals `sum(stage.slurm_wait_ms) + restart_reconcile.slurm_wait_ms` and the invariant reduces to a simple sum.

#### Scenario: Default level emits stage records

- **WHEN** `NHMS_SCHEDULER_TIMING_LEVEL` is unset (default `stage`) and a pass executes all five stages across 2 sources
- **THEN** the evidence JSON `timing.stages` array contains 10 records (5 stages × 2 sources)
- **AND** each record has non-negative `build_candidates_ms`, `dispatch_ms`, `slurm_wait_ms`, `python_time_ms`, `total_wall_ms`
- **AND** each record satisfies `stage.python_time_ms + stage.slurm_wait_ms == stage.total_wall_ms` within ±5 ms
- **AND** the pass-level union-of-intervals invariant holds within cumulative ±5 ms.

#### Scenario: Pass-only level suppresses stage records

- **WHEN** `NHMS_SCHEDULER_TIMING_LEVEL=pass` and a pass executes stages
- **THEN** the evidence JSON `timing.stages` is absent or an empty array
- **AND** no stdout log line with `level="stage"` is emitted
- **AND** the `timing.pass` object is still fully populated.

### Requirement: `restart_reconcile` is a first-class pseudo-record

At levels `stage` and `candidate`, the scheduler SHALL emit exactly one `timing.restart_reconcile` block per pass covering the `services/orchestrator/scheduler_runtime.py:543 self._run_restart_reconcile()` invocation.

The block SHALL include: `python_time_ms` (time spent in the python side of `_run_restart_reconcile` outside the `sacct` subprocess wait), `slurm_wait_ms` (time spent inside the `sacct` `run_restart_reconcile` subprocess call, direct-measured by wrapping the subprocess call in a `slurm_wait` sub-span), `total_wall_ms` = sum.

`timing.restart_reconcile.slurm_wait_ms` SHALL be included in the pass-level union-of-intervals when computing `pass.slurm_wait_ms` (see Requirement "Stage-layer timing"), so no Slurm-side wall-clock silently leaks into `python_time_ms`.

#### Scenario: restart_reconcile time is attributed to Slurm-wait side

- **WHEN** a pass runs at level `stage` and `_run_restart_reconcile` calls `sacct` for 300 ms
- **THEN** the evidence JSON `timing.restart_reconcile.slurm_wait_ms` is within [280, 320] ms
- **AND** `pass.slurm_wait_ms` includes those ~300 ms as part of the union of intervals
- **AND** none of those ~300 ms are attributed to `pass.python_time_ms`.

### Requirement: Candidate-layer timing is emitted only at `NHMS_SCHEDULER_TIMING_LEVEL=candidate`

At level `candidate`, the scheduler SHALL emit one candidate-layer record per (basin_model_id, source_id, stage_name) submitted within the pass. Including `source_id` in the key is deliberate: the same basin_model_id runs against both `gfs` and `IFS` sources in a single pass, and their sub-phase timings must not collapse into one record.

Each candidate record SHALL include the following fields corresponding to concrete code regions:

- Keys: `model_id`, `basin`, `source_id`, `stage_name`
- Sub-phases inside `services/orchestrator/scheduler_execution.py execute_candidate_cohort` (L233-410): `output_uri_lookup_ms` (L248-273), `basin_manifest_build_ms` (L276-283), `slurm_env_check_ms` (L289-303), `secret_manifest_scan_ms` (L305-308), `resource_profile_check_ms` (L309-322), `stage_raw_input_ms` (L329), `orchestrator_dispatch_ms` (L358 `orchestrator.orchestrate_cycle`) — **the first five (`output_uri_lookup_ms`, `basin_manifest_build_ms`, `slurm_env_check_ms`, `secret_manifest_scan_ms`, `resource_profile_check_ms`) are per-basin, opened inside the per-basin inner loop**; **`stage_raw_input_ms` (L329) and `orchestrator_dispatch_ms` (L358) are per-cohort, measured once at cohort scope and attributed per basin as an equal share for accounting simplicity**. This partition — 5 per-basin + 2 per-cohort — is canonical across `spec.md`, `tasks.md` §2.4, and issue #862.
- Sub-phases inside `services/orchestrator/chain_forecast_execution.py _submit_and_wait` (L489-599, invoked by `orchestrate_cycle` per stage per model): `build_stage_manifest_ms` (L498), `submit_sbatch_ms` (L505, direct-measured Slurm wait), `poll_until_terminal_ms` (L574-582 when the initial status is non-terminal; `0` and the `submit_sbatch_ms` span covers the wait when the fast path at L568-572 fires), `post_stage_hook_ms` (L590-594 `_after_stage_success` / `_after_stage_failure`).

Candidate-layer records SHALL NOT be written to stdout under any level to prevent operator log flood.

#### Scenario: Candidate level enables candidate records in evidence only

- **WHEN** `NHMS_SCHEDULER_TIMING_LEVEL=candidate` and a pass runs 13 basin models × 2 sources × 5 stages (the canonical node-22 workload)
- **THEN** the evidence JSON `timing.candidates` array contains 130 records (13 × 2 × 5), one per `(basin_model_id, source_id, stage_name)` tuple
- **AND** each record has non-negative values for every included sub-phase duration
- **AND** the operator's `journalctl` output has zero lines with `level="candidate"`.

#### Scenario: Stage level omits candidate records

- **WHEN** `NHMS_SCHEDULER_TIMING_LEVEL=stage` (default)
- **THEN** the evidence JSON `timing.candidates` is absent or an empty array.

### Requirement: `NHMS_SCHEDULER_TIMING_LEVEL` validation is fail-closed at pass entry

The scheduler SHALL validate `NHMS_SCHEDULER_TIMING_LEVEL` inside `run_once` — after `pass_id` mint and `SchedulerPassTiming` construction, before `root_preflight` — and accept only one of `pass`, `stage`, `candidate` (case-insensitive).

An unrecognised value SHALL cause `run_once` to return `SchedulerPassResult(status="preflight_blocked", ...)` with a `timing.pass` block populated (per Requirement "Pass-layer timing is always emitted") and evidence `reason="scheduler_timing_level_unrecognised"`. Validation SHALL NOT be performed at config load / daemon startup because doing so would raise before `pass_id` is minted and violate the "always emit `timing.pass`" invariant.

#### Scenario: Unknown level blocks the pass and still emits `timing.pass`

- **WHEN** `NHMS_SCHEDULER_TIMING_LEVEL=verbose` (invalid) is set and a pass starts
- **THEN** the pass returns `status="preflight_blocked"`
- **AND** the evidence JSON contains a `timing.pass` block with `status="preflight_blocked"` and non-null `total_wall_ms`, `total_cpu_ms`, `python_time_ms`, `slurm_wait_ms=0`
- **AND** the evidence records reason `scheduler_timing_level_unrecognised`
- **AND** the error message enumerates the accepted values `pass|stage|candidate`.

#### Scenario: Case-insensitive accepted values

- **WHEN** `NHMS_SCHEDULER_TIMING_LEVEL=STAGE` is set
- **THEN** the pass runs normally at stage level.

### Requirement: Stdout emission uses one JSON line per event, versioned

At levels `stage` and `candidate`, the scheduler SHALL emit one complete JSON line to stdout per pass entry, stage entry, stage exit, and pass exit; candidate-layer events SHALL NOT reach stdout.

Every stdout line SHALL be a self-delimited JSON object terminated with `\n`, containing at minimum `schema_version` (fixed string `"nhms.scheduler_pass_timing.v1"`), `ts` (UTC ISO 8601), `pass_id`, `level`, `phase`.

#### Scenario: journald captures live stage transitions

- **WHEN** an operator runs `journalctl --user -u nhms-compute-scheduler.service -f` during an active pass
- **THEN** each stage entry is visible within one flush interval as a single-line JSON record containing `schema_version="nhms.scheduler_pass_timing.v1"`, `ts`, `pass_id`, `level`, and `phase`
- **AND** each line is independently parseable by `jq`.

#### Scenario: No multi-line output

- **WHEN** the timing collector emits any stdout line
- **THEN** the line contains exactly one `\n` at its end
- **AND** contains no embedded newlines inside the JSON body.

### Requirement: Instrumentation overhead is bounded

The instrumentation SHALL add less than 1 % overhead to a nominal pass wall-clock at `NHMS_SCHEDULER_TIMING_LEVEL=candidate` (the noisiest setting).

#### Scenario: Overhead check on a synthetic pass

- **WHEN** a unit test runs a `run_once` variant with a mock gateway that skips Slurm dispatch, first with instrumentation disabled and then at level `candidate`
- **THEN** the wall-clock delta between the two runs is below 1 % of the baseline
- **OR** below 50 ms absolute, whichever is larger.

### Requirement: python-time and slurm-wait attribution is direct-measured, never inferred

`slurm_wait_ms` at every layer SHALL be measured by wrapping each concrete Slurm-boundary call in a timing span: inside `chain_forecast_execution._submit_and_wait` both `slurm_client.submit_job(payload)` (L505 — covers the already-terminal-on-submit fast path at L568-572) and `self._poll_until_terminal(...)` (L574-582 — covers the non-terminal-then-poll path); inside `scheduler_runtime._run_restart_reconcile` the `sacct` `run_restart_reconcile` subprocess call. `slurm_wait_ms` SHALL NOT be derived by subtracting other durations from `total_wall_ms`.

#### Scenario: Slurm-wait split is trustworthy under stubbed gateway (poll branch)

- **WHEN** a unit test injects a gateway stub whose `submit_job` returns a non-terminal status and whose `_poll_until_terminal` sleeps 100 ms before returning
- **THEN** the emitted `slurm_wait_ms` for that stage is within [90, 150] ms
- **AND** the emitted `dispatch_ms` for the same stage is under 50 ms.

#### Scenario: Slurm-wait split is trustworthy under stubbed gateway (already-terminal fast path)

- **WHEN** a unit test injects a gateway stub whose `submit_job` sleeps 100 ms before returning a **terminal** status (so `_poll_until_terminal` is never called and control follows L568-572)
- **THEN** the emitted `slurm_wait_ms` for that stage is within [90, 150] ms (attributed to the `submit_sbatch` sub-span)
- **AND** none of those 100 ms are attributed to `dispatch_ms` or `python_time_ms`.

### Requirement: Timing evidence is additive to existing scheduler evidence schema

The `timing:` block SHALL be a top-level key on the existing `SchedulerPassResult.evidence` JSON structure; no existing key SHALL be renamed, removed, or its semantics changed.

#### Scenario: Existing consumers still parse evidence

- **WHEN** a consumer that predates this change reads a post-change evidence artefact
- **THEN** it locates all previously-required keys unchanged
- **AND** ignores the unknown `timing:` block without error.
