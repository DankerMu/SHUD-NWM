# Capability Spec: partial-success-handling

## Context

全国化场景下，一个 array job 包含多个流域的并行 task。部分流域可能因数据缺失、模型发散等原因失败，但其它流域正常完成。M3 需要支持 partial success 状态：成功的流域可继续下游处理，不因个别流域失败而阻断整个 cycle。

状态机扩展：`forcing_ready_partial`、`parsed_partial` 已在设计文档 §5.1 定义。Orchestrator 在 array job 完成后统计 task 级结果，决定 cycle 进入正常、partial 还是 failed 状态。

---

## ADDED Requirements

### Requirement: Array job result aggregation

The orchestrator SHALL aggregate individual array task results after an array job completes, counting succeeded, failed, and cancelled tasks.

#### Scenario: All tasks succeed

- **WHEN** an array job with 10 tasks completes and sacct reports all 10 tasks as `COMPLETED`
- **THEN** the aggregated result MUST be: `succeeded=10, failed=0, cancelled=0`
- **THEN** the cycle MUST transition to the normal next state (e.g., `forcing_ready` after forcing_array)

#### Scenario: Some tasks fail, some succeed

- **WHEN** an array job with 10 tasks completes with 7 tasks `COMPLETED` and 3 tasks `FAILED`
- **THEN** the aggregated result MUST be: `succeeded=7, failed=3, cancelled=0`
- **THEN** the cycle MUST transition to the partial state (e.g., `forcing_ready_partial` after forcing_array)

#### Scenario: All tasks fail

- **WHEN** an array job with 10 tasks completes with all 10 tasks `FAILED`
- **THEN** the aggregated result MUST be: `succeeded=0, failed=10, cancelled=0`
- **THEN** the cycle MUST transition to the corresponding `failed_*` state (e.g., `failed_forcing`)

#### Scenario: sacct query for array task results

- **WHEN** the orchestrator queries results for array job `12345` with 10 tasks
- **THEN** the system MUST invoke `sacct --parsable2 --noheader --format=JobID,State,ExitCode --jobs=12345`
- **THEN** the system MUST parse lines matching `12345_[0-9]+` (array task lines) and ignore `12345.batch` / `12345.extern` lines
- **THEN** each task's State MUST be mapped to `succeeded`, `failed`, or `cancelled`

---

### Requirement: Partial state transitions

The cycle status machine SHALL support partial states that indicate mixed success across basins within an array stage.

#### Scenario: forcing_array partial success → forcing_ready_partial

- **WHEN** the forcing_array stage completes with at least one succeeded task and at least one failed task
- **THEN** the `met.forecast_cycle` status MUST transition to `forcing_ready_partial`
- **THEN** the next stage (shud_forecast_array) SHALL be submitted for the succeeded basins only

#### Scenario: parse_array partial success → parsed_partial

- **WHEN** the parse_array stage completes with mixed results
- **THEN** the `met.forecast_cycle` status MUST transition to `parsed_partial`
- **THEN** downstream stages SHALL only process the basins that succeeded in parse_array

#### Scenario: Partial state after already-partial input

- **WHEN** stage N received a partial manifest (e.g., 7 out of 10 basins) and 5 out of 7 succeed
- **THEN** the new partial count MUST reflect cumulative success: 5 basins remain
- **THEN** the cycle status MUST remain in a `_partial` state

---

### Requirement: Successful basins continue to next stage

When a stage completes with partial success, the next stage's manifest index SHALL only include basins that succeeded in the previous stage.

#### Scenario: Next stage manifest excludes failed basins

- **WHEN** forcing_array completes with tasks 0,1,2,4,5,7,8,9 succeeded and tasks 3,6 failed
- **THEN** the shud_forecast_array manifest index MUST contain only 8 entries (for succeeded basins)
- **THEN** the array submission MUST use `--array=0-7%{max_concurrent}` (re-indexed to 0-based)
- **THEN** the new manifest index MUST map task_id 0-7 to the 8 succeeded basins' original parameters

#### Scenario: Mapping from original basin to current task_id is preserved

- **WHEN** the next stage's manifest index is generated for partial-success basins
- **THEN** each entry MUST include `original_task_id` (the task_id from the previous stage) in addition to `task_id` (the new 0-based index)
- **THEN** this mapping SHALL enable tracing a basin's journey across stages

#### Scenario: Stage with only one surviving basin

- **WHEN** a partial result leaves only 1 basin succeeding
- **THEN** the next stage MUST submit a non-array job (single basin)
- **THEN** the manifest index MUST contain exactly 1 entry

---

### Requirement: Cycle status reflects worst-case across basins

The overall cycle status SHALL reflect the worst outcome across all basins, providing an at-a-glance summary of the cycle's health.

#### Scenario: All basins succeed through all stages → complete

- **WHEN** all basins complete all 7 stages successfully
- **THEN** the `met.forecast_cycle` status MUST transition to `complete`

#### Scenario: Some basins fail at any stage → remains at last _partial state

- **WHEN** some basins fail at any stage but at least one basin completes all stages
- **THEN** the `met.forecast_cycle` status MUST remain at the last `_partial` state (e.g., `parsed_partial`)
- **THEN** the cycle SHALL NOT transition to `complete` until ALL basins finish all stages or manual acknowledgment occurs
- **THEN** publish proceeds for the successful basins' products, but does not change the cycle status

#### Scenario: All basins fail at any stage → failed state

- **WHEN** all basins fail at any single stage
- **THEN** the `met.forecast_cycle` status MUST transition to the corresponding `failed_*` state
- **THEN** no downstream stages SHALL be submitted

---

### Requirement: API response includes per-basin status breakdown

The pipeline status API SHALL return per-basin status for each stage, enabling operators to identify which basins succeeded or failed.

#### Scenario: Per-basin breakdown in cycle status response

- **WHEN** a client queries `GET /api/v1/pipeline/status?source={source}&cycle_time={cycle_time}` (matching upstream §7)
- **THEN** the response MUST include a `stages` array with one entry per stage
- **THEN** each stage entry MUST include `stage_name`, `slurm_job_id`, `status`, and `basin_results`
- **THEN** `basin_results` MUST be an array of objects with: `basin_id`, `task_id`, `status` (succeeded/failed/cancelled), `exit_code`

#### Scenario: Partial stage shows per-basin detail

- **WHEN** forcing_array completed with 7 succeeded and 3 failed basins
- **THEN** the `forcing_array` stage entry MUST list all 10 basins in `basin_results`
- **THEN** the 7 succeeded basins MUST have `status="succeeded"` and `exit_code=0`
- **THEN** the 3 failed basins MUST have `status="failed"` and their respective `exit_code` values

#### Scenario: Non-array stages have single basin_result entry

- **WHEN** a non-array stage (e.g., `download`, `canonical`, `publish`) completes
- **THEN** `basin_results` MUST contain a single entry with `basin_id=null` (stage is not basin-specific)
- **THEN** the entry MUST include `status` and `exit_code`

---

### Requirement: Publish proceeds for successful basins; cycle status reflects partial state

The publish stage SHALL proceed for basins that have successfully completed all preceding stages. When partial success occurs at the publish stage, the cycle stays at `parsed_partial` — it does NOT transition to a `published` or `published_partial` state. The cycle transitions to `complete` only when ALL basins finish all stages; otherwise it stays at the last `_partial` state until manual acknowledgment or all basins complete.

#### Scenario: Publish proceeds for successful basins in partial cycle

- **WHEN** the publish stage runs after a partial cycle
- **THEN** the publish manifest MUST only include basins that succeeded in ALL of: `produce_forcing_array`, `run_shud_forecast_array`, `parse_output_array`, `compute_frequency_array`
- **THEN** the published product metadata MUST list which basins are included and which are excluded
- **THEN** the cycle status MUST remain at `parsed_partial` (NOT transition to `complete` or any published state)

#### Scenario: Published basin count matches fully-completed count

- **WHEN** 10 basins started the cycle, 3 failed at `produce_forcing_array`, 1 failed at `parse_output_array`
- **THEN** the publish stage MUST process exactly 6 basins
- **THEN** the published product MUST contain data for exactly 6 basins
- **THEN** the cycle metadata MUST record: `total_basins=10, published_basins=6, excluded_basins=[list of 4 basin_ids with failure stage]`
- **THEN** the cycle status MUST remain at `parsed_partial`

#### Scenario: No basins survive to publish

- **WHEN** all basins have failed at various stages and no basin completed `compute_frequency_array`
- **THEN** the publish stage MUST NOT be submitted
- **THEN** the cycle status MUST be set to a `failed_*` state corresponding to the latest stage with failures
- **THEN** an `ops.pipeline_event` MUST be recorded with message `"No basins available for publish; cycle failed"`
