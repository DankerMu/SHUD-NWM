# Capability Spec: dependency-chain-automation

## Context

M1 使用 LAZY 提交（前一步成功后再提交下一步）实现了 5 阶段线性链。M3 升级为 7 阶段（增加 frequency_array 和 publish），继续沿用 lazy submit 模式：Orchestrator 提交一个阶段，通过 sacct 轮询等待其完成，聚合结果（检查部分成功），然后有条件地提交下一阶段。阶段不会预先全部提交——Orchestrator 每次仅提交一个阶段。

七阶段（upstream §2 job_type）：`download_source_cycle` → `convert_canonical` → `produce_forcing_array` → `run_shud_forecast_array` → `parse_output_array` → `compute_frequency_array` → `publish_tiles`

Orchestrator 按 (source, cycle_time) 粒度编排，可同时运行多个 cycle（如 GFS 00Z 和 GFS 06Z）。每步完成后的 job_id 记录在 `ops.pipeline_job` 表中，crash recovery 可从最后完成的阶段恢复，继续提交下一个未提交的阶段。

---

## ADDED Requirements

### Requirement: Seven-stage lazy submit orchestration

The orchestrator SHALL submit stages one at a time, polling sacct for completion before deciding whether to submit the next stage. Stages are NOT submitted upfront.

#### Scenario: All seven stages are submitted lazily in sequence

- **WHEN** the orchestrator triggers a forecast cycle
- **THEN** stage 1 (`download_source_cycle`) MUST be submitted first
- **THEN** the orchestrator MUST poll sacct until stage 1 reaches a terminal state
- **THEN** if stage 1 succeeds, stage 2 (`convert_canonical`) MUST be submitted
- **THEN** this pattern MUST repeat for all 7 stages in order: `download_source_cycle` → `convert_canonical` → `produce_forcing_array` → `run_shud_forecast_array` → `parse_output_array` → `compute_frequency_array` → `publish_tiles`
- **THEN** each stage's `pipeline_job` record MUST be created at the time of submission, not upfront

#### Scenario: Orchestrator polls sacct for stage completion

- **WHEN** stage N has been submitted and is running
- **THEN** the orchestrator MUST poll sacct at a configurable interval (default 30s) to check the job status
- **THEN** for array job stages, the orchestrator MUST aggregate all task results to determine overall stage outcome
- **THEN** the orchestrator MUST evaluate: all tasks succeeded → submit next stage normally; partial success → submit next stage with reduced basin manifest; all tasks failed → do not submit next stage

#### Scenario: Array job completion triggers aggregation before next submit

- **WHEN** stage 3 (`produce_forcing_array`) is submitted as a job array and receives master job_id `12345`
- **THEN** the orchestrator MUST poll sacct for job `12345` until all array tasks reach terminal states
- **THEN** the orchestrator MUST aggregate task-level results (succeeded/failed/cancelled counts)
- **THEN** only after aggregation MUST the orchestrator decide whether to submit stage 4

---

### Requirement: Cycle-level orchestration

Each orchestration instance SHALL manage one (source, cycle_time) combination. The orchestrator SHALL support simultaneous orchestration of multiple cycles.

#### Scenario: One orchestration per (source, cycle_time)

- **WHEN** the orchestrator receives a trigger for `(source="GFS", cycle_time="2026050700")`
- **THEN** it MUST create one orchestration context for that specific (source, cycle_time) pair
- **THEN** all 7 stages MUST be associated with this single orchestration context
- **THEN** the context MUST include: `source`, `cycle_time`, `basins` (list), and `current_stage` (the last completed or active stage)

#### Scenario: Multiple cycles orchestrated simultaneously

- **WHEN** triggers arrive for `(GFS, 2026050700)` and `(GFS, 2026050706)` within the same time window
- **THEN** each MUST be orchestrated independently with separate stage submissions
- **THEN** jobs from different cycles MUST NOT share dependency chains
- **THEN** each cycle's stages MUST be tracked in separate `ops.pipeline_job` records

#### Scenario: Duplicate orchestration for same (source, cycle_time) is rejected

- **WHEN** an orchestration is already active (non-terminal) for `(GFS, 2026050700)`
- **THEN** a new trigger for the same (source, cycle_time) MUST be rejected
- **THEN** the rejection MUST return an error identifying the existing active orchestration

---

### Requirement: Stage tracking in pipeline_job table

Each stage's pipeline_job record SHALL be created at the time of submission (lazy), not upfront. This enables monitoring and crash recovery.

#### Scenario: pipeline_job record is created when a stage is submitted

- **WHEN** the orchestrator submits stage N to Slurm
- **THEN** a new `ops.pipeline_job` record MUST be created for that stage at submission time
- **THEN** each record MUST include: `slurm_job_id`, `job_type` (one of the 7 upstream stage names), `status`
- **THEN** `submitted_at` MUST be set to the current UTC timestamp

#### Scenario: Stage status is updated from sacct polling

- **WHEN** the status poller queries sacct for all active job_ids in a cycle
- **THEN** each `ops.pipeline_job` record MUST be updated with the current Slurm state
- **THEN** `started_at` MUST be set when status transitions to `RUNNING`
- **THEN** `finished_at` MUST be set when status reaches a terminal state

#### Scenario: Pipeline event is emitted for each stage transition

- **WHEN** a stage's status changes (e.g., `pending` → `running`)
- **THEN** an `ops.pipeline_event` record MUST be inserted with `entity_type='pipeline_job'`, `entity_id`, `event_type='status_change'`, `status_from`, `status_to`

---

### Requirement: Stage failure handling

If any stage fails entirely (all tasks fail), the orchestrator SHALL NOT submit subsequent stages. The orchestrator detects failure via sacct polling and updates the cycle status accordingly.

#### Scenario: Stage failure prevents downstream submission

- **WHEN** stage 3 (`produce_forcing_array`) transitions to `FAILED` (all tasks failed)
- **THEN** the orchestrator MUST NOT submit stages 4-7
- **THEN** no `pipeline_job` records SHALL be created for stages 4-7 (since they are never submitted)
- **THEN** the orchestrator MUST update the `met.forecast_cycle` status to the corresponding `failed_*` state (e.g., `failed_forcing`)

#### Scenario: Failure state mapping per stage

- **WHEN** a stage fails, the cycle status MUST be set according to:
  - stage 1 (`download_source_cycle`) fails → `failed_download`
  - stage 2 (`convert_canonical`) fails → `failed_convert`
  - stage 3 (`produce_forcing_array`) fails → `failed_forcing`
  - stage 4 (`run_shud_forecast_array`) fails → `failed_run`
  - stage 5 (`parse_output_array`) fails → `failed_parse`
  - stage 6 (`compute_frequency_array`) fails → `failed_parse` (frequency is a parse sub-step)
  - stage 7 (`publish_tiles`) fails → `failed_publish`

#### Scenario: Failure event includes diagnostic detail

- **WHEN** a stage transitions to `FAILED`
- **THEN** the `ops.pipeline_event` record MUST include the sacct exit code and Slurm state string in `message`
- **THEN** if available, stderr from `fetch_logs` MUST be attached to the event

---

### Requirement: Crash recovery from persisted state

The orchestrator SHALL persist its orchestration state such that after a crash or restart, it can resume from the next unsubmitted stage without re-submitting completed stages.

#### Scenario: Orchestrator restarts and resumes from next unsubmitted stage

- **WHEN** the orchestrator process crashes after stage 4 (`run_shud_forecast_array`) has completed successfully
- **THEN** on restart, it MUST query `ops.pipeline_job` for the cycle's completed stages
- **THEN** it MUST identify that the last completed stage is stage 4
- **THEN** it MUST resume by submitting stage 5 (`parse_output_array`), continuing the lazy submit sequence

#### Scenario: Orchestrator restarts while a stage is still running

- **WHEN** the orchestrator crashes while stage 3 is still `running` (has a `slurm_job_id` but no terminal status)
- **THEN** on restart, it MUST resume sacct polling for the in-flight stage 3
- **THEN** it MUST NOT re-submit stage 3
- **THEN** once stage 3 reaches a terminal state, it MUST proceed with the lazy submit logic for stage 4

#### Scenario: All stages already terminal on restart

- **WHEN** the orchestrator restarts and finds all submitted stages in terminal states
- **THEN** it MUST NOT submit any new jobs
- **THEN** it MUST evaluate the final cycle status based on stage outcomes

#### Scenario: No stages submitted yet on restart

- **WHEN** the orchestrator restarts and finds no `pipeline_job` records for a cycle that was triggered but never started
- **THEN** it MUST begin the lazy submit sequence from stage 1 (`download_source_cycle`)

---

### Requirement: Concurrency across cycles

Multiple cycles SHALL be orchestrated simultaneously without interference.

#### Scenario: GFS 00Z and 06Z cycles run in parallel

- **WHEN** the orchestrator is managing cycles for `(GFS, 2026050700)` and `(GFS, 2026050706)`
- **THEN** each cycle MUST have its own independent lazy submit sequence
- **THEN** sacct polling MUST query both cycles' active job_ids
- **THEN** a failure in one cycle MUST NOT affect the other cycle's stages

#### Scenario: Different sources run independently

- **WHEN** cycles for `(GFS, 2026050700)` and `(ERA5, 2026-05-06)` are both active
- **THEN** each MUST be orchestrated with independent submission sequences
- **THEN** stage tracking MUST distinguish cycles by `source` and `cycle_time`
