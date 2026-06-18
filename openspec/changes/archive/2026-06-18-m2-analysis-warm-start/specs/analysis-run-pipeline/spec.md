## ADDED Requirements

### Requirement: Analysis Run Creation
系统 SHALL 为每个 active model 创建 analysis run（run_type='analysis', scenario_id='analysis_true_field'），使用真实场/再分析 forcing 驱动 SHUD。

#### Scenario: Create analysis run from ERA5 forcing
- **WHEN** ERA5 forcing production 完成（forcing_version 记录已写入 met.forcing_version，forcing_package_uri 和 checksum 就绪）且 model 为 active
- **THEN** 系统创建 hydro_run（run_type='analysis', scenario_id='analysis_true_field', model_id, forcing_version_id, status='created'），start_time/end_time 对应 forcing 覆盖的时间范围

#### Scenario: Analysis run with init state
- **WHEN** 存在可用 StateSnapshot（usable_flag=true）且 valid_time <= start_time
- **THEN** hydro_run.init_state_id 填入最近可用 state_id，manifest 中 initial_state.ic_file_uri 指向 state_uri，runtime.init_mode=3

#### Scenario: Analysis run cold-start
- **WHEN** 无可用 StateSnapshot（首次运行或所有 state 不可用）
- **THEN** hydro_run.init_state_id=NULL，runtime.init_mode=1（cold-start），run_manifest 中记录 initial_state.state_id=null

#### Scenario: Duplicate analysis run prevention
- **WHEN** 同一 model_id + 同一 date_range 已有 active analysis run（status NOT IN ('failed','cancelled','superseded')）
- **THEN** 系统拒绝创建新 run，返回错误

### Requirement: Analysis Forcing Production
系统 SHALL 为 analysis run 生成 forcing，复用 M1 forcing producer，source_id='ERA5'。

#### Scenario: ERA5 forcing production
- **WHEN** ERA5 canonical 产品可用（forecast_cycle.status='canonical_ready'）
- **THEN** 复用 M1 forcing producer 生成 .tsd.forc + CSV，写入 met.forcing_version（source_id='ERA5', model_id, cycle_time, forcing_package_uri, checksum），关联 forcing_version_component 血缘

#### Scenario: ERA5 latency fallback forcing
- **WHEN** 目标时段 ERA5 canonical 不可用（约 5 天迟滞内），但同时段 GFS analysis/short forecast canonical 可用
- **THEN** 使用 GFS canonical 产品生成 forcing，met.forcing_version 中 source_id='GFS'，lineage_json 中记录 fallback_reason='era5_latency'

### Requirement: Analysis Forcing Selection
系统 SHALL 选择可用的真实场/再分析 forcing 用于 analysis run。

#### Scenario: ERA5 available
- **WHEN** 目标时段 ERA5 canonical 产品可用
- **THEN** 使用 ERA5 forcing，source_id='ERA5'

#### Scenario: ERA5 latency fallback
- **WHEN** 目标时段 ERA5 不可用（约 5 天迟滞内）
- **THEN** 使用 GFS analysis/short forecast forcing 补位，lineage_json 记录实际数据源和 fallback_reason

### Requirement: Analysis SHUD Execution
系统 SHALL 通过 Slurm 提交 SHUD analysis 作业，执行方式与 forecast 相同但 run_type 不同。

#### Scenario: Successful analysis run
- **WHEN** SHUD analysis 作业完成（exit code 0）且 .rivqdown 和 .cfg.ic 文件完整
- **THEN** hydro_run.status 从 running → succeeded，输出文件上传到 output_uri

#### Scenario: Analysis run failure
- **WHEN** SHUD analysis 作业失败（非零 exit code）
- **THEN** hydro_run.status='failed'，error_code/error_message 记录，不生成 StateSnapshot

#### Scenario: Analysis run Slurm timeout
- **WHEN** SHUD analysis 作业超过 Slurm 时间限制（TIMEOUT signal）
- **THEN** hydro_run.status='failed'，error_code='SLURM_TIMEOUT'，pipeline 不提交后续 stage

### Requirement: Analysis Output Parsing
系统 SHALL 解析 analysis run 的 .rivqdown 输出并入库。

#### Scenario: Analysis river_timeseries ingestion
- **WHEN** analysis run 输出解析完成
- **THEN** river_timeseries 写入 analysis run 的结果（lead_time_hours=NULL，因为是真实场非预报）；scenario_id 通过 hydro_run.scenario_id='analysis_true_field' 关联，不直接存储在 river_timeseries 中

#### Scenario: Analysis parse updates run status
- **WHEN** .rivqdown 解析和入库成功
- **THEN** hydro_run.status 从 succeeded → parsed

#### Scenario: Analysis parse failure
- **WHEN** .rivqdown 文件缺失或列数与 river_segment_count 不匹配
- **THEN** hydro_run.status='failed'，error_code='OUTPUT_INCOMPLETE'，不生成 StateSnapshot

### Requirement: StateSnapshot Generation
系统 SHALL 在 analysis run 解析成功后从 SHUD 输出中提取 `.cfg.ic` 状态文件并保存。

#### Scenario: Generate state snapshot
- **WHEN** analysis run status='parsed' 且 `.cfg.ic` 文件存在于输出目录
- **THEN** 系统上传 `.cfg.ic` 到 states/{model_id}/{valid_time}/，写入 hydro.state_snapshot（state_id='state_{model_id}_{valid_time}', model_id, run_id, valid_time=run.end_time, state_uri, checksum, usable_flag=false），随后执行 QC 检查，通过后设 usable_flag=true

#### Scenario: State snapshot QC integrated in pipeline
- **WHEN** state snapshot 存储完成
- **THEN** pipeline 自动执行 QC（文件存在、大小>0、checksum 匹配），通过后 usable_flag=true，确保后续 forecast 可使用

### Requirement: Analysis Pipeline Job Chain
系统 SHALL 实现 analysis 专用的 Slurm 作业链。

#### Scenario: Analysis pipeline stages
- **WHEN** 触发 analysis pipeline
- **THEN** 按顺序执行：ERA5 download → canonical convert → forcing produce → SHUD analysis → output parse → state snapshot save + QC，每个 stage 写入 ops.pipeline_job，采用 lazy submission（前一 stage 成功后才提交下一 stage）

#### Scenario: Analysis pipeline stage failure stops chain
- **WHEN** 某个 stage 失败
- **THEN** 后续 stage 不提交，pipeline 标记为对应失败状态，pipeline_event 记录状态流转

#### Scenario: Analysis pipeline trigger
- **WHEN** 执行 `nhms-pipeline trigger-analysis --model-id --date-range`
- **THEN** 系统创建 analysis pipeline，防重复（同 model + 重叠 date_range 已有 active pipeline 时拒绝）
